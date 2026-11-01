"""MiMo TTS 封装（只用小米云端 API）

三个模型各管一件事：
    mimo-v2.5-tts              预置音色（冰糖/茉莉/苏打/白桦…），支持唱歌、支持流式
    mimo-v2.5-tts-voicedesign  用文字描述造一个音色，不需要样本
    mimo-v2.5-tts-voiceclone   用一段音频样本复刻音色

风格有两种给法（位置不能放错）：
    自然语言 → role:user     "像跟朋友讲话，语速稍快"
    音频标签 → role:assistant 文本开头加 (开心) 或中间插 [叹气]

环境变量：
    MIMO_API_KEY       必填
    MIMO_TTS_MODEL     默认 mimo-v2.5-tts
    MIMO_TTS_VOICE     默认 冰糖（可选 茉莉 / 苏打 / 白桦）
    MIMO_TTS_STYLE     默认一句人设语气（见下方 DEFAULT_STYLE）
    MIMO_REF_AUDIO     voiceclone 用的参考音频路径（mp3/wav，≤10MB）
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import env_loader  # noqa: F401  读取同目录 .env

API_URL = "https://api.xiaomimimo.com/v1/chat/completions"

DEFAULT_MODEL = os.environ.get("MIMO_TTS_MODEL") or "mimo-v2.5-tts"
DEFAULT_VOICE = os.environ.get("MIMO_TTS_VOICE") or "冰糖"
# 直播用风格：松弛、不发力、别表演。刻意顿挫会让咬字变重、声音发闷。
# M2「松弛」为默默 2026-10-30 试听 5 版后拍板。
DEFAULT_STYLE = os.environ.get("MIMO_TTS_STYLE") or (
    "声音松弛、不用力的年轻女生，随口说话，语气平，不要刻意咬字。"
)

CACHE_DIR = Path(__file__).resolve().parent / "audio"
_LOCK = threading.Lock()
_stats = {"seconds": 0.0, "calls": 0, "failed": 0}

MAX_CHARS = 400  # 超长截断，一次别合成太久


def _preprocess(text: str) -> str:
    """文本预处理。默认原样；MIMO_STRIP_COMMAS=1 时去掉中文逗号。

    依据（本机实测，2026-10-30）：同一句话带逗号 5.44s、去逗号 5.28s，
    只快 3%。所以默认不折腾，需要更紧的口播节奏时再开。
    """
    if os.environ.get("MIMO_STRIP_COMMAS") != "1":
        return " ".join(text.split())
    return " ".join(text.replace("，", "").replace(",", "").split())


def api_key() -> str:
    return (os.environ.get("MIMO_API_KEY") or "").strip()


def enabled() -> bool:
    return bool(api_key())


def stats() -> dict:
    with _LOCK:
        s = dict(_stats)
    s["enabled"] = enabled()
    s["model"] = DEFAULT_MODEL
    s["voice"] = DEFAULT_VOICE
    s["free"] = True  # 官方文档：限时免费
    return s


def _duration(path: Path) -> float:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(path)],
            capture_output=True, text=True, timeout=20,
            encoding="utf-8", errors="replace",
        )
        return max(0.0, float((out.stdout or "0").strip() or 0))
    except Exception:
        return 0.0


def _voice_field(voice: str, model: str) -> str:
    """只有 voiceclone 才把样本塞进 voice 字段，格式 data:audio/mpeg;base64,..."""
    if not model.endswith("voiceclone"):
        return voice
    if voice.startswith("data:"):
        return voice
    ref = os.environ.get("MIMO_REF_AUDIO") or ""
    if ref and Path(ref).is_file():
        raw = Path(ref).read_bytes()
        if len(raw) > 10 * 1024 * 1024:
            raise ValueError("参考音频超过 10MB")
        mime = "audio/wav" if ref.lower().endswith(".wav") else "audio/mpeg"
        return f"data:{mime};base64,{base64.b64encode(raw).decode()}"
    raise ValueError("voiceclone 需要 MIMO_REF_AUDIO 指向一段 mp3/wav 样本")


def synthesize(
    text: str,
    voice: str | None = None,
    style: str | None = None,
    model: str | None = None,
) -> dict:
    """合成一段语音，返回 {ok, url, seconds, cached, model, voice}。

    text 里可以直接写标签，例如 "(开心)今天搞定了" 或 "哎……[叹气]算了"。
    """
    text = (text or "").strip()
    if not text:
        return {"ok": False, "error": "空文本"}
    if not enabled():
        return {"ok": False, "error": "未配置 MIMO_API_KEY"}
    text = _preprocess(text)
    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS]

    mdl = model or DEFAULT_MODEL
    voc = voice or DEFAULT_VOICE
    sty = style if style is not None else DEFAULT_STYLE

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha1(f"{mdl}|{voc}|{sty}|{text}".encode("utf-8")).hexdigest()[:16]
    path = CACHE_DIR / f"{key}.mp3"
    if path.is_file() and path.stat().st_size > 1024:
        return {"ok": True, "url": f"/audio/{path.name}", "seconds": _duration(path),
                "cached": True, "model": mdl, "voice": voc}

    # ---- 组装请求：文本必须放 assistant，风格指令放 user ----
    messages = []
    if sty:
        messages.append({"role": "user", "content": sty})
    if mdl.endswith("voicedesign"):
        # 音色设计：user 里是"音色描述"，assistant 是样例文本
        messages = [{"role": "user", "content": sty or "年轻女生的声音"},
                    {"role": "assistant", "content": text}]
        audio_cfg: dict = {"format": "mp3", "optimize_text_preview": False}
    else:
        messages.append({"role": "assistant", "content": text})
        try:
            audio_cfg = {"format": "mp3", "voice": _voice_field(voc, mdl)}
        except ValueError as e:
            return {"ok": False, "error": str(e)}

    body = json.dumps({"model": mdl, "messages": messages, "audio": audio_cfg},
                      ensure_ascii=False).encode()

    req = urllib.request.Request(API_URL, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("api-key", api_key())

    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            payload = json.loads(r.read().decode())
        raw = base64.b64decode(payload["choices"][0]["message"]["audio"]["data"])
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        with _LOCK:
            _stats["failed"] += 1
        return {"ok": False, "error": f"HTTP {e.code}: {detail}"}
    except Exception as e:
        with _LOCK:
            _stats["failed"] += 1
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    path.write_bytes(raw)
    secs = _duration(path) or len(text) / 5.5
    with _LOCK:
        _stats["seconds"] += secs
        _stats["calls"] += 1

    return {"ok": True, "url": f"/audio/{path.name}", "seconds": round(secs, 2),
            "cached": False, "model": mdl, "voice": voc,
            "elapsed": round(time.time() - t0, 2)}


if __name__ == "__main__":
    import sys
    arg = " ".join(sys.argv[1:]) or "这是一段测试语音，确认 TTS 能用。"
    print(synthesize(arg))
    print(stats())
