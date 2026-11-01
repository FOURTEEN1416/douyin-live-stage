"""抖音弹幕桥 —— 把直播间的弹幕接进舞台，并可选自动应答。

抖音没有开放直播间消息接口，所以必须借一个第三方的弹幕抓取工具，常见三个：

    skmcj/dycast                输入房间号，ws 转发      轻
    chenwng/douyin-live         ws://127.0.0.1:1088/ws/<房间号>
    DouyinBarrageGrab           系统代理抓包，最重但最全

它们吐出的 JSON 字段各不相同，这里用通用递归解析（找 nickname + content），
认不出来的结构会原样打印，方便你照着改。

用法:
    python douyin_bridge.py --ws ws://127.0.0.1:1088/ws/14214746370
    python douyin_bridge.py --ws ... --auto-reply          # 弹幕来了自己答
    python douyin_bridge.py --ws ... --list-only           # 只看原始结构，不推送
    python douyin_bridge.py --mock                          # 造一条假弹幕验证链路
"""

from __future__ import annotations

import argparse
import json
import os
import random
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from wsclient import WSError, WSClient

import env_loader  # noqa: F401  读取同目录 .env
import stage_memory

ROOT = Path(__file__).resolve().parent

STAGE = os.environ.get("STAGE_URL") or "http://127.0.0.1:8790"
API_URL = "https://api.xiaomimimo.com/v1/chat/completions"
TEXT_MODEL = os.environ.get("MIMO_TEXT_MODEL") or "mimo-v2.6-flash"

NICK_KEYS = ("nickname", "nick_name", "nickName", "username", "user_name",
             "userName", "nick", "name", "Nickname")
TEXT_KEYS = ("content", "Content", "text", "Text", "msg", "Msg", "message",
             "Message", "danmaku", "chatContent")

SKIP_WORDS = {"666", "哈哈哈", "谢谢", "支持", "来了", "1", "打卡", "已关注"}

# 触发唱歌的关键词（命中就去唱一句原创，不碰版权曲目）
SING_WORDS = ("唱歌", "唱一", "唱个", "唱首", "唱段", "唱支", "来一首", "点歌", "唱吧", "开唱")
# 原创短句兜底，模型失败时用
FALLBACK_LYRICS = "晚风吹过窗台的灯，我把今天写成一行"

LYRIC_PROMPT = """为直播间写两句歌词，中文，口语，押韵，适合清唱。
每句 8~12 个字，一共两句，不要标题不要解释。
主题：{hint}
只输出歌词本身。"""


# ---------------------------------------------------------------- 通用解析
def _find_nick(obj, depth: int):
    """在子树里找昵称（用于"内容在父层、昵称在子层"的结构）。"""
    if depth > 6:
        return None
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, str) and 0 < len(v) <= 32:
                if any(n.lower() == k.lower() for n in NICK_KEYS):
                    return v
        for v in obj.values():
            got = _find_nick(v, depth + 1)
            if got:
                return got
    elif isinstance(obj, list):
        for v in obj[:8]:
            got = _find_nick(v, depth + 1)
            if got:
                return got
    return None


def _walk(obj, depth: int, nick: str | None):
    if depth > 6:
        return None
    if isinstance(obj, dict):
        local_nick = nick
        text = None
        for k, v in obj.items():
            if not isinstance(v, str):
                continue
            kl = k.lower()
            if not local_nick and any(n.lower() == kl for n in NICK_KEYS) and 0 < len(v) <= 32:
                local_nick = v
            if text is None and any(t.lower() == kl for t in TEXT_KEYS) and 0 < len(v) <= 200:
                text = v
        if local_nick and text:
            return local_nick, text
        # 有内容但没昵称 → 到子树里捞一个
        if text and not local_nick:
            for v in obj.values():
                n = _find_nick(v, depth + 1)
                if n:
                    return n, text
        for v in obj.values():
            got = _walk(v, depth + 1, local_nick)
            if got:
                return got
    elif isinstance(obj, list):
        for v in obj[:8]:
            got = _walk(v, depth + 1, nick)
            if got:
                return got
    return None


def dig_json(obj, depth: int = 0):
    """递归找 (昵称, 内容)。昵称在父层、内容在子层也能配对。认不出返回 None。"""
    return _walk(obj, depth, None)


# ---------------------------------------------------------------- 舞台
def push_danmaku(nick: str, text: str) -> bool:
    body = json.dumps({"uname": nick, "text": text}, ensure_ascii=False).encode()
    req = urllib.request.Request(f"{STAGE}/api/danmaku", data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status == 200
    except Exception:
        return False


def push_say(text: str) -> bool:
    body = json.dumps({"text": text, "voice": True}, ensure_ascii=False).encode()
    req = urllib.request.Request(f"{STAGE}/api/say", data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status == 200
    except Exception:
        return False


def push_event(kind: str, **kw) -> bool:
    """把直播间事件推给舞台，驱动余额经济（礼物 +50 / 弹幕 +2 / 被抓 −200）。

    这条链路是"观众买命"的物理实现：没有它，余额就只是个装饰数字。
    """
    payload = {"kind": kind}
    payload.update(kw)
    body = json.dumps(payload, ensure_ascii=False).encode()
    req = urllib.request.Request(f"{STAGE}/api/event", data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            return r.status == 200
    except Exception:
        return False


# ---------------------------------------------------------------- 事件识别
# 抓取工具吐的 JSON 各不相同，但基本都带一个 type/method 字段。
# 认不出类型时退化成「有礼物字段就算礼物」这种特征判断。
TYPE_KEYS = ("type", "msgType", "msg_type", "method", "Method", "Type",
             "eventType", "event_type", "cmd", "Cmd")
EVENT_MAP = (
    ("gift", ("gift", "礼物")),
    ("member", ("member", "enter", "进入")),
    ("like", ("like", "点赞")),
    ("social", ("social", "follow", "关注")),
    ("chat", ("chat", "danmaku", "弹幕", "comment", "评论")),
)


def _find_type_tag(obj, depth: int = 0) -> str:
    if depth > 5:
        return ""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, str) and any(k.lower() == t.lower() for t in TYPE_KEYS):
                return v
        for v in obj.values():
            got = _find_type_tag(v, depth + 1)
            if got:
                return got
    elif isinstance(obj, list):
        for v in obj[:6]:
            got = _find_type_tag(v, depth + 1)
            if got:
                return got
    return ""


def detect_event(obj) -> str:
    """判定这条消息是弹幕 / 进房 / 礼物 / 点赞 / 关注 / 直播状态 / 其他。

    真实数据里大量是系统消息（在线人数、榜单、横幅、推流自适应…），
    这些**不能**当成弹幕 —— 早先版本兜底返回 chat，导致 34 条"弹幕"里
    只有 8 条是真的，其余全是系统消息在冒充。
    """
    # 直播状态通知（开播/下播），抓取服务会周期性推
    if isinstance(obj, dict) and obj.get("type") == "system" \
            and obj.get("event") == "live_status":
        return "status"

    tag = _find_type_tag(obj).lower()
    if tag:
        for kind, words in EVENT_MAP:
            if any(w.lower() in tag for w in words):
                return kind
        return "other"        # 有明确类型，但不属于我们要反应的
    # 没有类型字段（部分抓取工具就是这样）：按特征猜，猜不出当弹幕
    try:
        s = json.dumps(obj, ensure_ascii=False)[:3000]
    except Exception:
        return "other"
    if any(k in s for k in ("giftName", "gift_name", "giftId", "repeatCount")):
        return "gift"
    if any(k in s for k in ("memberCount", "enterRoom", "进入直播间")):
        return "member"
    if any(k in s for k in ("likeCount", "like_count", "点赞")):
        return "like"
    if any(k in s for k in ("关注了主播", "followed")):
        return "social"
    return "chat"


def gift_name(obj) -> str:
    """从礼物消息里捞礼物名（捞不到就返回空）。"""
    for k in ("giftName", "gift_name", "giftname"):
        v = _deep_get(obj, k)
        if isinstance(v, str) and v.strip():
            return v.strip()[:20]
    return ""


def _deep_get(obj, key: str, depth: int = 0):
    if depth > 5:
        return None
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k.lower() == key.lower():
                return v
        for v in obj.values():
            got = _deep_get(v, key, depth + 1)
            if got is not None:
                return got
    elif isinstance(obj, list):
        for v in obj[:6]:
            got = _deep_get(v, key, depth + 1)
            if got is not None:
                return got
    return None


# ---------------------------------------------------------------- 人设
_PERSONA: dict | None = None


def load_persona() -> dict:
    global _PERSONA
    if _PERSONA is not None:
        return _PERSONA
    try:
        _PERSONA = json.loads((ROOT / "persona.json").read_text(encoding="utf-8"))
    except Exception as e:
        print(f"  [!] persona.json 读不到（{type(e).__name__}），用兜底人设")
        _PERSONA = {"description": "你叫大肥鱼，是直播间里的虚拟主播，说话短、口语、不客套。"}
    return _PERSONA


def _state_hint() -> str:
    """此刻在干什么 —— 读舞台标题，读不到就空着。

    用「我」不用「主播」：她是写代码和开播的人，不是旁边看着的人。
    """
    try:
        with urllib.request.urlopen(f"{STAGE}/api/state", timeout=3) as r:
            s = json.loads(r.read().decode())
        t = (s.get("title") or "").strip()
        sub = (s.get("sub") or "").strip()
        if t:
            return f"我正在做：{t}" + (f"（{sub}）" if sub else "")
    except Exception:
        pass
    return ""


def _live_hint() -> str:
    """她自己的处境：余额、状态、现在聊什么。让她答话时"知道自己是谁、在哪"。"""
    try:
        with urllib.request.urlopen(f"{STAGE}/api/state", timeout=3) as r:
            s = json.loads(r.read().decode())
    except Exception:
        return ""
    lv = s.get("live") or {}
    bits = []
    if lv:
        bits.append(f"{lv.get('status_text','专心工作中')}，余额 {lv.get('balance',0)} token"
                    f"（约能聊 {lv.get('minutes_left',0)} 分钟）")
        if int(lv.get("caught_count") or 0):
            bits.append(f"今天被抓 {lv.get('caught_count')} 次")
    topic = (s.get("topic") or "").strip()
    if topic:
        bits.append(f"弹幕正在聊：{topic}")
    return "；".join(bits)


def build_prompt(nick: str, text: str, kind: str = "chat", extra: str = "") -> str:
    """人设 + 此刻 + 在聊什么 + 刚才聊过的 + 这个人是谁 + 这次要回什么。

    v3（2026-10-31）加了三段：
      - 【现在在聊什么】话题状态机的快照，每条都带她的既有立场
      - 【我的处境】余额/状态/被抓次数，让她能自嘲"工资掉了"
      - 【别学这些】把最近的门童腔当反例贴出来——模型会被上下文带偏，
        光说"别这样"没用，得指出来"这几条别学"
    """
    p = load_persona()
    mem = stage_memory.memory()
    blocks: list[str] = []

    desc = (p.get("description") or "").strip()
    if desc:
        blocks.append("【你是谁】\n" + desc)

    ptext = (p.get("personality_text") or "").strip()
    if ptext:
        blocks.append("【怎么说话】\n" + ptext)

    anchors = p.get("core_anchors") or []
    if anchors:
        blocks.append("【必须守住】\n" + "\n".join(f"- {a}" for a in anchors))

    world = (p.get("world") or {}).get("things") or []
    if world:
        blocks.append("【你嘴里常出现的东西（挑一个用，别全用）】\n" +
                      "\n".join(f"- {w}" for w in world))

    example = (p.get("mes_example") or "").strip()
    if example:
        blocks.append("【说话的调子（照这个来，别照抄）】\n" + example)

    hint = _state_hint()
    if hint:
        blocks.append("【此刻】\n" + hint)

    live = _live_hint()
    if live:
        blocks.append("【我的处境】\n" + live)

    # 话题状态机：她知道"现在在聊什么"，而且对每个话题早有立场
    try:
        import topic_engine
        snap = topic_engine.engine().snapshot()
        if snap:
            blocks.append("【现在在聊什么（括号里是你早就想好的立场）】\n" + snap)
    except Exception:
        pass

    recent_all = mem.recent_dialogue(10)
    bad = [x for x in recent_all if any(w in x for w in ("来了", "进来坐", "找个地儿", "欢迎"))]
    recent = [x for x in recent_all if x not in bad]
    if recent:
        blocks.append("【刚才聊过的】\n" + "\n".join(recent[-7:]))
    # 反例单独放：模型会被上文带偏，光说"别这样"没用，得把废句挑出来标红
    if bad:
        blocks.append("【别学这些（都是废话，重复了会被观众骂）】\n" +
                      "\n".join(bad[-3:]))

    vh = mem.viewer_hint(nick)
    if vh:
        blocks.append("【这个人】\n" + vh)

    if kind == "gift":
        ask = f"{nick} 送了你礼物：{extra or '一个礼物'}。谢一句，带上具体的东西，别肉麻。"
    elif kind == "member":
        ask = f"{nick} 刚进直播间。说一句带动作或物件的话，别用「来了」「进来坐」。"
    elif kind == "social":
        ask = f"{nick} 关注了你。谢一句，别客套。"
    else:
        ask = f"{nick} 说：{text}"

    blocks.append("【现在要回应的】\n" + ask)
    blocks.append(
        "只输出你要说的那一句中文。规则：\n"
        "1. 先复用对方话里的一个词（证明你听懂了）；\n"
        "2. 句中必须有一个具体物件（键盘/椅子/外卖/报错/工位/房贷/屎山）；\n"
        "3. 最后半句翻一下（先嘴硬后认怂，或反过来）；\n"
        "4. 10-24 字，一句话，不要引号、不要解释、不要表情符号、不要列点。"
    )
    return "\n\n".join(blocks)


def _fast_providers() -> list:
    """弹幕应答的候选链：**flash 模型排前面**。

    narrator 那条链是给「写台词」用的，DeepSeek 会思考、文案更耐读，所以
    放第一。但弹幕回复是即时对话，等 8~13 秒观众早走了 —— 这里把 agnes
    flash（2~3 秒）提到最前，DeepSeek 留作兜底。
    """
    try:
        import narrator
        ps = list(narrator._text_providers())
    except Exception as e:
        print(f"  [x] 引入 narrator 失败：{type(e).__name__}")
        return []

    def score(item):
        model = (item[2] or "").lower()
        # 实测（2026-10-30，同一 prompt 各跑一次）：
        #   DeepSeek-V4.1-Flash   7.8s  内容最好，接地气
        #   agnes-3.0-flash      11.5s  返回空
        #   agnes-2.5-flash       7.6s  一般
        #   mimo                  0.2s  无余额（402）
        # 结论：flash 不一定快，按实测排，别按名字猜。
        # mimo 有余额时可以提到最前面。
        if "deepseek" in model:
            return 0
        if "agnes-2" in model:
            return 1
        if "agnes-3" in model:
            return 2
        if "mimo" in model:
            return 3
        return 4

    return sorted(ps, key=score)


def _call_model(prompt: str) -> str:
    """按候选链问一次，返回干净的一句话（失败返回空串）。"""
    for name, url, model, header, auth in _fast_providers():
        try:
            import narrator
            txt, _meta = narrator._chat(url, model, header, auth, prompt, None,
                                        json_mode=False)
        except Exception as e:
            print(f"  [x] {name} 应答失败：{type(e).__name__}")
            continue
        txt = (txt or "").strip().strip('"').strip("「」“”")
        txt = "".join(txt.split())
        if 4 <= len(txt) <= 80:
            return txt
        if txt:
            print(f"  [!] {name} 返回长度异常（{len(txt)} 字）")
    return ""


def answer(nick: str, text: str, kind: str = "chat", extra: str = "") -> str | None:
    """生成一句台词，并且**必须过质检**。

    v3 改动：
      - member / social 不再问模型，直接从招呼池里挑（模型写招呼 90% 是门童腔）
      - chat / gift 问模型，但过 lines.check；不过关就带着理由重写一次
      - 两次都不过关就用兜底句（话题立场），绝不把废话推上屏
    """
    import lines

    salt = str(int(time.time() // 9))
    mem = stage_memory.memory()

    # 招呼类：池子直出。池子里每条都手写过，且不会重复。
    if kind == "member":
        return lines.greeting(nick, mem.is_returning(nick), salt)
    if kind == "social":
        return lines.social_line(nick, salt)

    recent = [r[2:] for r in mem.recent_dialogue(12) if r.startswith("我：")]
    prompt = build_prompt(nick, text, kind, extra)
    last = ""
    for attempt in range(2):
        txt = _call_model(prompt)
        if not txt:
            break
        txt = lines.sanitize(txt)
        ok, score, reasons = lines.check(txt, text, kind, recent)
        if ok:
            if attempt:
                print(f"  [√] 重写通过（{score}）", flush=True)
            return txt
        last = txt
        print(f"  [x] 打回 {score}：{'；'.join(reasons)} ← {txt}", flush=True)
        if attempt == 0:
            prompt = prompt + "\n\n" + lines.repair_hint(reasons) + \
                     f"\n你刚才那句是：「{txt}」，换个说法重写。"

    # 兜底：礼物走礼物池，其余用她当前话题的立场
    if kind == "gift":
        return lines.gift_line(nick, salt)
    try:
        import topic_engine
        s = topic_engine.engine().suggest(min_gap=0)
        if s:
            return s
    except Exception:
        pass
    return last or None


def wants_song(text: str) -> bool:
    t = text.strip()
    return any(w in t for w in SING_WORDS)


def make_lyrics(hint: str) -> str:
    """生成两句原创歌词；失败就用兜底句。"""
    hint = (hint or "深夜写代码").strip()[:20]
    try:
        import narrator  # 复用它的文本候选链
        for name, url, model, header, auth in narrator._text_providers():
            txt, _meta = narrator._chat(url, model, header, auth,
                                        LYRIC_PROMPT.format(hint=hint), None, json_mode=False)
            txt = (txt or "").strip().strip('"').strip("「」“”")
            txt = "".join(txt.split())
            if 10 <= len(txt) <= 60:
                return txt
    except Exception as e:
        print(f"  [!] 歌词生成失败：{type(e).__name__}")
    return FALLBACK_LYRICS


def push_sing(lyrics: str) -> dict:
    body = json.dumps({"lyrics": lyrics}, ensure_ascii=False).encode()
    req = urllib.request.Request(f"{STAGE}/api/sing", data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        return {"ok": False, "error": str(e)}


def should_reply(text: str) -> bool:
    t = text.strip()
    if len(t) < 3:
        return False
    if t in SKIP_WORDS:
        return False
    return True


# 同一时刻只允许一条应答在生成，但生成过程不挡住收弹幕
replying = threading.Event()

# 进房/关注这种事件会刷屏，单独限流（跟弹幕回复的间隔分开算）
PRESENCE_GAP = {"member": 20.0, "social": 30.0}
_last_presence: dict[str, float] = {"member": 0.0, "social": 0.0}


def _reply_worker(nick: str, text: str, kind: str = "chat", extra: str = "") -> None:
    try:
        ans = answer(nick, text, kind, extra)
        if ans:
            print(f"[答] {ans}", flush=True)
            push_say(ans)
            stage_memory.memory().note_reply(ans)
    except Exception as e:
        print(f"[!] 应答失败: {type(e).__name__}: {e}", flush=True)
    finally:
        replying.clear()


# 招呼和弹幕回复**分开锁**。
# 原来共用一个 replying 标志，结果是：只要有条弹幕在等模型（3~10 秒），
# 这期间进来的人全被静默丢掉 —— 观众看到的就是"进房没人理"。
_presence_busy = threading.Event()


def handle_presence(nick: str, kind: str, args, now: float) -> None:
    """有人进房 / 关注：招呼一句，但要限流，不然一句话说十遍。

    招呼语走的是手写池（不调模型），毫秒级返回，所以完全不该跟弹幕回复抢锁。
    """
    if not args.auto_reply or _presence_busy.is_set():
        return
    gap = PRESENCE_GAP.get(kind, 20.0)
    if now - _last_presence.get(kind, 0.0) < gap:
        return
    _last_presence[kind] = now
    _presence_busy.set()

    def _run() -> None:
        try:
            ans = answer(nick, "", kind, "")
            if ans:
                print(f"[迎] {nick}：{ans}", flush=True)
                push_say(ans)
                stage_memory.memory().note_reply(ans)
        except Exception as e:
            print(f"[!] 招呼失败: {type(e).__name__}: {e}", flush=True)
        finally:
            _presence_busy.clear()

    threading.Thread(target=_run, daemon=True).start()


# ---------------------------------------------------------------- 主循环
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ws", default="", help="弹幕源 ws 地址")
    ap.add_argument("--auto-reply", action="store_true", help="自动回复弹幕")
    ap.add_argument("--reply-gap", type=float, default=8.0,
                    help="两次回复最小间隔（对标节奏约 6-8 秒一条，15 秒太稀）")
    ap.add_argument("--list-only", action="store_true", help="只打印原始消息")
    ap.add_argument("--sing", dest="sing", action="store_true", default=True,
                    help="弹幕点歌就唱（默认开）")
    ap.add_argument("--no-sing", dest="sing", action="store_false")
    ap.add_argument("--sing-gap", type=float, default=120.0, help="全局两次唱歌最小间隔")
    ap.add_argument("--mock", action="store_true", help="造一条弹幕走一遍链路")
    args = ap.parse_args()

    if args.mock:
        nick, text = "小林", "这是真人还是AI"
        print(f"[mock] {nick}：{text}")
        print("  推送舞台:", push_danmaku(nick, text))
        if args.auto_reply:
            ans = answer(nick, text) or "（没有 API Key，跳过应答）"
            print("  作答:", ans)
            print("  推送到舞台:", push_say(ans))
        return

    if not args.ws:
        print("缺少 --ws。例: --ws ws://127.0.0.1:1088/ws/14214746370")
        return

    print(f"弹幕桥启动 · 源 {args.ws} · 舞台 {STAGE}"
          f"{' · 自动应答' if args.auto_reply else ''}")
    print("Ctrl+C 停止\n")

    # 记忆落盘：每 15 秒写一次，不逐条写盘
    def _persist() -> None:
        while True:
            time.sleep(15)
            stage_memory.memory().save()

    threading.Thread(target=_persist, daemon=True).start()

    last_reply = 0.0
    last_sing = 0.0
    user_sing: dict[str, float] = {}
    seen: dict[str, float] = {}

    while True:
        try:
            c = WSClient(args.ws, timeout=90)
            c.connect()
            print("[+] 已连接")
            for raw in c.messages():
                if args.list_only:
                    print(raw[:300])
                    continue
                try:
                    obj = json.loads(raw)
                except json.JSONDecodeError:
                    print("[?] 非 JSON:", raw[:160])
                    continue
                kind = detect_event(obj)
                got = dig_json(obj)
                nick = got[0] if got else (_find_nick(obj, 0) or "")
                text = got[1] if got else ""

                now = time.time()

                # 直播状态通知：开播/下播。开播前启动也能等到这一条
                if kind == "status":
                    st = str(obj.get("status") or "")
                    label = obj.get("status_text") or st
                    name = obj.get("live_name") or ""
                    if st == "online":
                        print(f"[直播] {name} 已开播 —— 开始接弹幕", flush=True)
                    else:
                        print(f"[直播] {label or st} —— 等待开播中"
                              f"（抓取服务每 15 秒自动重查，开播即接上）", flush=True)
                    continue

                # 系统消息（在线人数/榜单/横幅/推流自适应…）直接跳过
                if kind == "other":
                    continue

                # ---- 事件类：进房 / 点赞 / 关注 ----
                if kind in ("member", "like", "social"):
                    # 先进经济：进房 +1、点赞 +0.2、关注算一次互动
                    push_event(kind, count=1)
                    if nick and kind != "like":      # 点赞太密，不单独反应
                        handle_presence(nick, kind, args, now)
                    continue

                if not got:
                    print("[?] 认不出结构:", raw[:200])
                    continue

                sig = f"{nick}|{text}"
                if now - seen.get(sig, 0) < 10:   # 同一条 10 秒内只处理一次
                    continue
                seen[sig] = now
                if len(seen) > 500:
                    seen.clear()

                label = "礼物" if kind == "gift" else "弹幕"
                print(f"[{label}] {nick}：{text}")
                push_danmaku(nick, text)
                stage_memory.memory().note_danmaku(nick, text)
                # 经济：弹幕 +2，礼物 +50（连击按次数加）
                if kind == "gift":
                    try:
                        n = int(_deep_get(obj, "combo_count") or _deep_get(obj, "giftCount") or 1)
                    except Exception:
                        n = 1
                    push_event("gift", amount=max(1, min(n, 20)), name=text[:12])
                else:
                    push_event("chat", text=text)

                # 点歌优先：命中就唱，不再走文字应答
                can_sing = (args.sing and wants_song(text)
                            and (now - last_sing) >= args.sing_gap
                            and (now - user_sing.get(nick, 0)) >= 90)
                if can_sing:
                    last_sing = now
                    user_sing[nick] = now
                    hint = text
                    for w in SING_WORDS:
                        hint = hint.replace(w, "")
                    hint = hint.strip(" ：:，,") or "深夜写代码"
                    print(f"[点歌] {nick} → {hint}")
                    lyrics = make_lyrics(hint)
                    res = push_sing(lyrics)
                    print(f"[唱] {lyrics}  ({'成功' if res.get('ok') else res.get('error')})")
                    continue

                # 应答放后台线程：模型一次要 3~10 秒，同步跑会把这段时间
                # 收到的弹幕全堵在缓冲区里，看起来就是「半天不理人」。
                # 礼物一定要谢，所以不走 should_reply 的字数过滤。
                extra = gift_name(obj) if kind == "gift" else ""
                if (args.auto_reply and (kind == "gift" or should_reply(text))
                        and (now - last_reply) >= args.reply_gap
                        and not replying.is_set()):
                    last_reply = now
                    replying.set()
                    threading.Thread(target=_reply_worker,
                                     args=(nick, text, kind, extra),
                                     daemon=True).start()
        except KeyboardInterrupt:
            print("\n已停止")
            return
        except (WSError, OSError) as e:
            print(f"[!] 连接断开（{e}），5 秒后重连")
            time.sleep(5)
        except Exception as e:
            print(f"[x] {type(e).__name__}: {e}")
            time.sleep(5)


if __name__ == "__main__":
    main()
