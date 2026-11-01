"""自动旁白引擎 —— 让直播自己说话，不需要人盯着。

做的事和那个便携包一样，只是把"刷 B 站视频"换成"看你的真实屏幕"：

    截屏 → 视觉模型看懂当前画面 → 生成一句人话 → POST /api/say（自动上字幕 + TTS 出声）

用法:
    set MIMO_API_KEY=sk-xxx
    python narrator.py                  # 全自动，每 90 秒看一眼屏幕
    python narrator.py --mock           # 不调模型，用模板句验证链路
    python narrator.py --region 0,0,1700,900     # 只盯代码区，忽略旁边的窗口
    python narrator.py --interval 60 --min-gap 40

参数:
    --interval   多久看一次屏幕（秒，默认 90）
    --min-gap    两次开口的最小间隔（秒，默认 45）
    --region     只看屏幕的某块区域 x,y,w,h
    --persona    人设名（默认「十四」）
    --dry        只看不推，把生成的句子打印出来
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
import random
import re
import threading
import time
import urllib.error
import urllib.request

import tts  # 复用同目录的 key / 模型配置

import env_loader  # noqa: F401  读取同目录 .env

# Agnes 免费模型并发上限极低（1~2 路），所有调用串行化，避免自己撞自己
_CALL_LOCK = threading.Lock()

# 各端点的响应速度记忆（指数平滑，秒）。越慢排越后，失败直接降权。
_SPEED: dict[str, float] = {}
_SPEED_LOCK = threading.Lock()


SLOW_LINE = float(os.environ.get("VISION_SLOW_S") or 40.0)


def _rank(provs):
    """保持 .env 里的顺序（DeepSeek 优先），只把实测慢于阈值的踢到末尾。

    这样主力端点固定，不会被一次抖动换掉；同时慢到不可用的会被自动跳过。
    """
    with _SPEED_LOCK:
        return sorted(provs, key=lambda p: 1 if _SPEED.get(p[0], 0.0) >= SLOW_LINE else 0)


def _record(name: str, secs: float, failed: bool = False) -> None:
    val = 60.0 if failed else max(0.5, min(120.0, secs))
    with _SPEED_LOCK:
        old = _SPEED.get(name)
        _SPEED[name] = val if old is None else round(old * 0.6 + val * 0.4, 2)

API_URL = "https://api.xiaomimimo.com/v1/chat/completions"

# 识图端点：谁提供 vision 就填谁。以下三项任一为空则回落到 Agnes 默认值。
VISION_BASE_URL = os.environ.get("VISION_BASE_URL") or "https://apihub.agnes-ai.com/v1/chat/completions"
VISION_MODEL = os.environ.get("VISION_MODEL") or "agnes-3.0-flash"
STAGE = os.environ.get("STAGE_URL") or "http://127.0.0.1:8790"

# 直播发声用的固定风格（口语化、不匀速）。tts.py 里也有同名默认值。
LIVE_STYLE = (
    "一个年轻女生在跟朋友随口讲话。语速偏快，有快有慢，重点词之前顿一下。"
    "句尾轻轻收掉，不要拖腔。不要新闻播报感，不要讲师腔，不要匀速朗读。"
)

PROMPT = """你是「大肥鱼」，这是你的直播间。屏幕上是观众看到的画面，也就是你此刻在做的事。

判断这块画面值不值得开口，然后决定说不说：

【不该说】锁屏、登录页、报错页、空白窗口、纯桌面壁纸、几乎没变化的地方 → 不要说
【该说】真正在操作的内容：网页、编辑器、聊天窗口、终端、文档 → 说一句

要说的话，对着屏幕随口讲给观众听，要求：
- 20~35 个字，一句话说完
- 口语，可以带"诶""啊""嗯"这种语气词，可以有半句话的停顿（用……表示）
- 不要书面语，不要用"当前""正在""进行""功能""界面"这类词
- 具体说你在干什么、看到什么，不要评价屏幕"很漂亮""很复杂"
- 不要提到"屏幕""画面""截图"
- 不要重复最近说过的话：{recent}

只输出一行 JSON，不要任何解释、不要代码块：
{{"say": "那句话"}}
不该说时输出：
{{"say": ""}}"""


# ---------------------------------------------------------------- 截屏
def grab(region: tuple[int, int, int, int] | None) -> bytes:
    from PIL import Image, ImageGrab

    box = region if region else None
    im = ImageGrab.grab(bbox=box) if box else ImageGrab.grab()
    w = 1280
    if im.width > w:
        im = im.resize((w, int(im.height * w / im.width)), Image.LANCZOS)
    buf = io.BytesIO()
    im.convert("RGB").save(buf, format="JPEG", quality=72, optimize=True)
    return buf.getvalue()


def phash(jpeg: bytes) -> str:
    """粗粒度感知哈希，用来判断画面有没有明显变化。"""
    from PIL import Image

    im = Image.open(io.BytesIO(jpeg)).convert("L").resize((16, 16), Image.LANCZOS)
    px = list(im.tobytes())
    avg = sum(px) / len(px)
    bits = "".join("1" if p > avg else "0" for p in px)
    return hashlib.sha1(bits.encode()).hexdigest()[:16]


# ---------------------------------------------------------------- 调用模型
def _agnes_models(env_key: str, default: tuple[str, ...]) -> tuple[str, ...]:
    """Agnes 的模型按顺序降级：3.0-flash 会限流，退到 2.5-flash。"""
    one = (os.environ.get(env_key) or "").strip()
    return (one,) if one else default


def _providers() -> list[tuple[str, str, str, str, str]]:
    """识图候选链：(名字, url, model, header, value)。Agnes 快，做主；DeepSeek 兜底。"""
    out: list[tuple[str, str, str, str, str]] = []
    # DeepSeek 端点定为主力（稳定 ~13s，不抢并发）
    vkey = (os.environ.get("VISION_API_KEY") or "").strip()
    if vkey and "agnes" not in VISION_BASE_URL:
        out.append((VISION_MODEL, VISION_BASE_URL, VISION_MODEL, "Authorization", f"Bearer {vkey}"))
    akey = (os.environ.get("AGNES_API_KEY") or "").strip()
    if akey:
        for m in _agnes_models("AGNES_VISION_MODEL", ("agnes-3.0-flash", "agnes-2.5-flash")):
            out.append((m, "https://apihub.agnes-ai.com/v1/chat/completions",
                        m, "Authorization", f"Bearer {akey}"))
    mkey = tts.api_key()
    if mkey:
        out.append(("mimo", API_URL, os.environ.get("MIMO_VISION_MODEL") or "mimo-v2.6-flash",
                    "api-key", mkey))
    return out


def _text_providers() -> list[tuple[str, str, str, str, str]]:
    """写台词的候选链：DeepSeek 会思考、文案更耐读，放第一；Agnes 兜底。"""
    out: list[tuple[str, str, str, str, str]] = []
    vkey = (os.environ.get("VISION_API_KEY") or "").strip()
    if vkey:
        out.append((VISION_MODEL, VISION_BASE_URL, VISION_MODEL, "Authorization", f"Bearer {vkey}"))
    akey = (os.environ.get("AGNES_API_KEY") or "").strip()
    if akey:
        for m in _agnes_models("AGNES_TEXT_MODEL", ("agnes-3.0-flash", "agnes-2.5-flash")):
            out.append((m, "https://apihub.agnes-ai.com/v1/chat/completions",
                        m, "Authorization", f"Bearer {akey}"))
    mkey = tts.api_key()
    if mkey:
        out.append(("mimo", API_URL, os.environ.get("MIMO_TEXT_MODEL") or "mimo-v2.6-flash",
                    "api-key", mkey))
    return out


DESCRIBE_PROMPT = """看这张屏幕截图，用不超过 20 个字客观说明：画面里是什么界面、在做什么。
只描述事实，不要评价，不要加语气词。直接输出描述。"""

WRITE_PROMPT = """你是「大肥鱼」，你自己在开直播、自己写代码。屏幕上的就是你正在做的事——不是别人在做，是你在做。

画面情况：{desc}

用第一人称「我」随口说一句给观众听，就像边干活边跟他们聊天。要求：
- 20~35 个字，一句话说完
- **必须用「我」**：不要说「主播」，也不要用「他/她」指自己
- 口语，可以带"诶""啊""嗯"，可以有半句话停顿（用……表示）
- 不要书面语，不要用"当前""正在""进行""功能""界面""展示"这类词
- 不要评价画面"很漂亮""很复杂"
- 不要重复最近说过的话：{recent}

只输出一行 JSON，不要解释、不要代码块：
{{"say": "那句话"}}
画面不值得说时输出：{{"say": ""}}"""


def _describe(jpeg: bytes) -> tuple[str, dict]:
    """第一步：谁识图快就用谁，拿回一句客观画面描述。"""
    b64 = base64.b64encode(jpeg).decode()
    last: dict = {}
    for name, url, model, header, auth in _rank(_providers()):
        text, meta = _chat(url, model, header, auth, DESCRIBE_PROMPT, b64, json_mode=False)
        if text:
            return text.strip()[:120], {"vision": name, "vision_model": model,
                                        "vision_elapsed": meta.get("elapsed")}
        last = meta
    return "", last


def _write_line(desc: str, recent: list[str]) -> tuple[str, dict]:
    """第二步：基于描述写台词，文案模型优先。"""
    prompt = WRITE_PROMPT.format(desc=desc,
                                 recent="、".join(recent[-3:]) if recent else "（还没有）")
    last: dict = {}
    for name, url, model, header, auth in _rank(_text_providers()):
        text, meta = _chat(url, model, header, auth, prompt, None, json_mode=True)
        if not text:
            last = meta
            continue
        say, extra = _parse_say(text)
        if say:
            return say, {"writer": name, "writer_model": model,
                         "write_elapsed": meta.get("elapsed"), **extra}
        last = {"writer": name, "skipped": True, **extra}
    return "", last


def _chat(url, model, header, auth, prompt, b64, json_mode: bool) -> tuple[str, dict]:
    """一次对话调用。b64 非空则带图。思考模型默认关思考（带图时 36s→13s）。"""
    content: any = prompt
    if b64:
        content = [{"type": "text", "text": prompt},
                   {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}]
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": content}],
        "temperature": 0.9,
        "max_tokens": 900,
        "thinking": {"type": "disabled"},
    }
    t0 = time.time()
    data = None
    last_err = ""
    for attempt in range(2):
        body = json.dumps(payload, ensure_ascii=False).encode()
        req = urllib.request.Request(url, data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header(header, auth)
        try:
            with _CALL_LOCK:
                with urllib.request.urlopen(req, timeout=60) as r:
                    data = json.loads(r.read().decode())
            break
        except urllib.error.HTTPError as e:
            last_err = f"HTTP {e.code}"
            if e.code == 400 and "thinking" in payload:   # 该端点不认这个参数，去掉重试
                payload.pop("thinking", None)
                continue
            if e.code in (429, 500, 502, 503):
                time.sleep(3 * (attempt + 1))   # 免费模型限流，退避要狠一点
                continue
            break
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            break
    if data is None:
        _record(model, time.time() - t0, failed=True)
        return "", {"error": last_err or "请求失败"}

    try:
        msg = (data["choices"][0] or {}).get("message") or {}
        text = (msg.get("content") or "").strip()
    except Exception as e:
        return "", {"error": f"响应结构异常: {e}"}
    _record(model, time.time() - t0, failed=not text)
    return text, {"elapsed": round(time.time() - t0, 2)}


def look_and_talk(jpeg: bytes, recent: list[str]) -> tuple[bool, str, dict]:
    """双模式：识图与写词分开。Agnes 看图（2s），DeepSeek 写词（会思考、文案好）。"""
    mode = (os.environ.get("NARRATOR_MODE") or "dual").lower()
    if mode != "dual":
        return _single_shot(jpeg, recent)

    desc, m1 = _describe(jpeg)
    if not desc:
        return False, "", m1
    if any(w in desc for w in _BAD_WORDS):
        return False, "", {"skipped": True, "desc": desc, **m1}

    say, m2 = _write_line(desc, recent)
    if not say:
        return False, "", {**m1, **m2}
    return True, say, {**m1, **m2, "desc": desc}


def _single_shot(jpeg: bytes, recent: list[str]) -> tuple[bool, str, dict]:
    """单次调用模式（NARRATOR_MODE=single），识图和写词一次完成。"""
    prompt = PROMPT.format(recent="、".join(recent[-3:]) if recent else "（还没有）")
    b64 = base64.b64encode(jpeg).decode()
    last: dict = {}
    for name, url, model, header, auth in _rank(_providers()):
        text, meta = _chat(url, model, header, auth, prompt, b64, json_mode=True)
        if not text:
            last = meta
            continue
        say, extra = _parse_say(text)
        if say:
            return True, say, {"provider": name, "model": model, **meta, **extra}
        last = {"provider": name, "skipped": True, **meta, **extra}
    return False, "", last
    if meta_extra.get("raw") and not say:
        pass
    if not say:
        return False, "", {"skipped": True, "elapsed": round(time.time() - t0, 2), **meta_extra}
    return True, say, {"elapsed": round(time.time() - t0, 2), "usage": usage,
                       "model": model, **meta_extra}


# 出现这些词说明画面不是要展示的内容，宁可不说
_BAD_WORDS = ("登录", "密码", "验证码", "账号", "注册", "404", "报错", "错误")


def _parse_say(raw: str) -> tuple[str, dict]:
    """从模型输出里取那句话。兼容 JSON / 纯文本 / 代码块包裹。"""
    s = (raw or "").strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\s*", "", s)
        s = re.sub(r"\s*```$", "", s).strip()

    say = ""
    if s.startswith("{"):
        try:
            obj = json.loads(s)
            say = str(obj.get("say") or obj.get("text") or "")
        except json.JSONDecodeError:
            m = re.search(r'"say"\s*:\s*"([^"]*)"', s)
            say = m.group(1) if m else ""
    elif s.upper().startswith("SKIP"):
        say = ""
    else:
        say = s

    say = say.strip().strip('"').strip("「」“”").replace("\n", " ").strip()
    if any(w in say for w in _BAD_WORDS):
        return "", {"filtered": "bad_words"}
    if len(say) < 6:
        return "", {"filtered": "too_short"}
    if len(say) > 60:
        return "", {"filtered": "too_long"}
    # 思考模型有时会把英文推理漏出来，中文占比过低一律丢掉
    cjk = sum(1 for c in say if "\u4e00" <= c <= "\u9fff")
    if cjk / max(1, len(say)) < 0.55:
        return "", {"filtered": "not_chinese"}
    return say, {}


# ---------------------------------------------------------------- 推给舞台
def push_say(text: str, voice: bool = True, dry: bool = False) -> dict:
    if dry:
        print(f"[dry] {text}")
        return {"ok": True, "dry": True}
    body = json.dumps({"text": text, "voice": voice}, ensure_ascii=False).encode()
    req = urllib.request.Request(f"{STAGE}/api/say", data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        return {"ok": False, "error": str(e)}


def stage_alive() -> bool:
    try:
        with urllib.request.urlopen(f"{STAGE}/api/state?since=0", timeout=5) as r:
            return r.status == 200
    except Exception:
        return False


# ---------------------------------------------------------------- 主循环
MOCK_LINES = [
    "我在把角色模板那页的复制逻辑改掉，点一次会生成独立人设，不动原来那条。",
    "微信这边刚发过去一句，等十四回；顺手把语音引擎切到新模型了。",
    "后台这个列表还没分页，一会儿得加，不然角色多了会卡。",
]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=float, default=55)
    ap.add_argument("--min-gap", type=float, default=28)
    ap.add_argument("--region", default="")
    ap.add_argument("--persona", default="十四")
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--mock", action="store_true")
    ap.add_argument("--voice", dest="voice", action="store_true", default=True)
    ap.add_argument("--no-voice", dest="voice", action="store_false")
    args = ap.parse_args()

    region = None
    if args.region:
        parts = [int(x) for x in args.region.replace("，", ",").split(",")]
        if len(parts) == 4:
            region = tuple(parts)  # type: ignore[assignment]

    if not stage_alive():
        print(f"[!] 舞台没起来：{STAGE}")
        print("    先运行 run.cmd 或 python server.py")
        return

    if not args.mock and not tts.enabled():
        print("[!] 未设置 MIMO_API_KEY，无法看图。用 --mock 可以先验证链路。")
        return

    print(f"自动旁白已启动 · 舞台 {STAGE} · 每 {args.interval:.0f}s 看一眼屏幕"
          f"{' · 只看区域 ' + args.region if region else ''}")
    print("Ctrl+C 停止\n")

    recent: list[str] = []
    last_hash = ""
    last_spoke = 0.0
    last_caption = ""
    mock_i = 0

    while True:
        try:
            # 冷场才说话：距上次开口不够久就等
            wait = args.min_gap - (time.time() - last_spoke)
            if wait > 0:
                time.sleep(min(wait, 5))
                continue

            jpeg = grab(region)
            h = phash(jpeg)

            # 画面没变且不是冷场太久 → 不重复说同一件事
            same = (h == last_hash)
            if same and (time.time() - last_spoke) < args.interval * 2:
                time.sleep(6)
                continue

            if args.mock:
                text, ok = MOCK_LINES[mock_i % len(MOCK_LINES)], True
                mock_i += 1
            elif same:
                # 画面长时间没变（一直盯着同一个文件改）——以前这里就静音了，
                # 观众听到的是几十秒的空白。改成从话题库里抛一句，保证不断档。
                text, ok = "", False
                try:
                    import topic_engine
                    text = topic_engine.engine().suggest(min_gap=args.min_gap)
                    ok = bool(text)
                except Exception:
                    pass
                if not ok:
                    last_hash = h
                    time.sleep(15)
                    continue
                print(f"[旁白·话题] {text}", flush=True)
            else:
                ok, text, meta = look_and_talk(jpeg, recent)
                if meta.get("error"):
                    print(f"[x] {meta['error']}")
                    time.sleep(10)
                    continue
                if not ok:
                    # 看不出在干什么，隔一会儿再看
                    last_hash = h
                    time.sleep(15)
                    continue

            # 防止和用户手动推的那句撞车
            try:
                with urllib.request.urlopen(f"{STAGE}/api/state?since=0", timeout=5) as r:
                    st = json.loads(r.read().decode())
                cur = st.get("caption") or ""
                if cur and cur != last_caption and (st.get("speaking_until") or 0) > time.time():
                    last_caption = cur
                    time.sleep(8)
                    continue
                last_caption = cur
            except Exception:
                pass

            res = push_say(text, voice=args.voice, dry=args.dry)
            if res.get("ok"):
                print(f"[说] {text}")
                recent.append(text)
                del recent[:-6]
                last_spoke = time.time()
                last_hash = h
            else:
                print(f"[x] 推送失败：{res.get('error')}")

            # 加抖动，避免机械的固定间隔
            time.sleep(args.interval * random.uniform(0.85, 1.2))

        except KeyboardInterrupt:
            print("\n已停止")
            return
        except Exception as e:
            print(f"[x] {type(e).__name__}: {e}")
            time.sleep(8)


if __name__ == "__main__":
    main()
