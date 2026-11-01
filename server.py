"""直播舞台服务 —— 零依赖（Python 标准库），给 OBS / 直播伴侣 提供透明覆盖层。

用法:
    python server.py            # http://127.0.0.1:8790
    python server.py --port 8791

接口（全部只监听 127.0.0.1）:
    POST /api/say      {"text": "要说的话"}           -> 上字幕 + 进入说话状态
    POST /api/danmaku  {"uname": "水友", "text": "hi"}  -> 弹幕飘过
    POST /api/title    {"title": "在做的事", "sub": "副标题"}
    POST /api/clear
    GET  /api/state?since=N                            -> 长轮询增量状态
"""

from __future__ import annotations

import argparse
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import tts

import env_loader  # noqa: F401  读取同目录 .env

ROOT = Path(__file__).resolve().parent
STATE_LOCK = threading.Lock()

# 取景框对应的三个窗口（键名与 layout.json 一致）
WINDOW_KEYS = ("code", "web", "wechat")


def window_rects() -> list[dict]:
    """三个目标窗口的实时位置（逻辑像素），供舞台画取景框用

    窗口随便拖，取景框跟着走 —— 不锁你的桌面。
    """
    out = [{"key": k, "rect": None, "title": ""} for k in WINDOW_KEYS]
    try:
        import windows_layout
    except Exception:
        return out
    try:
        cfg = json.loads(windows_layout.LAYOUT_FILE.read_text(encoding="utf-8"))
    except Exception:
        return out
    try:
        wins = windows_layout.list_windows()
    except Exception:
        return out
    for item in out:
        meta = cfg.get(item["key"]) or {}
        match = meta.get("match", "")
        if not match:
            continue
        w = windows_layout.find_window(match, wins, meta)
        if not w:
            continue
        x, y, ww, hh = w["rect"]
        if x < -2000 or y < -2000 or ww < 50 or hh < 50:
            continue          # 最小化的窗口（幽灵坐标），当没开
        item["rect"] = [int(x), int(y), int(ww), int(hh)]
        item["title"] = w["title"]
    return out

# 中文语速约 5.5 字/秒，用来估一条字幕该停留多久
CHARS_PER_SEC = 5.5
MIN_SPEAK_S = 2.0
MAX_SPEAK_S = 25.0

_state: dict = {
    "v": 0,
    "seq": 0,          # 台词代次：异步 TTS 回来时用它判断这段音频还算不算数
    "title": "还在改 bug",
    "sub": "工资 0/6000 · 摸鱼会被抓",
    "line1": "",       # 底部标题卡第一行（黄字）
    "line2": "",       # 底部标题卡第二行（白字）
    "caption": "",
    "audio": "",
    "mode": "talk",
    "speaking_until": 0.0,
    "danmaku": [],
    "work": {},        # 打工面板：工资/专注/摸鱼/手速
    "live": {},        # 状态机 + 余额经济（live_state）
    "topic": "",       # 当前在聊的话题（topic_engine）
}
_danmaku: list[dict] = []  # 最近 12 条
_work_snap: dict = {}      # work_monitor 的最新观测，喂给 live_state


def _bump() -> None:
    _state["v"] += 1


def _tts_worker(seq: int, text: str, voice: str | None, style: str | None) -> None:
    """后台合成语音。字幕已经先上屏了，这里只负责把声音补上。

    合成期间如果又有人说了新台词（seq 变了），这段音频直接作废 ——
    否则慢返回的旧音频会把新台词顶掉，听起来像答非所问。
    """
    try:
        info = tts.synthesize(text, voice=voice, style=style)
    except Exception:
        return
    if not info.get("ok"):
        return
    hold = max(MIN_SPEAK_S, float(info.get("seconds") or 0) + 0.8)
    with STATE_LOCK:
        if _state.get("seq") != seq:
            return
        _state["audio"] = info["url"]
        _state["speaking_until"] = time.time() + hold
        _bump()
    # 由服务端直接播到虚拟声卡：直播伴侣只采 CABLE Output，
    # 观众就只听到她的声音，不会被微信提示音之类的系统声音串进去。
    import os
    if os.environ.get("STAGE_NO_AUDIO") == "1":
        return
    try:
        import audio_out
        audio_out.player().play(ROOT / info["url"].lstrip("/"))
    except Exception as e:
        print(f"[音频] 播放失败: {type(e).__name__}: {e}", flush=True)


def _speak_duration(text: str) -> float:
    return max(MIN_SPEAK_S, min(MAX_SPEAK_S, len(text) / CHARS_PER_SEC))


class Handler(BaseHTTPRequestHandler):
    server_version = "LiveStage/1.0"

    def log_message(self, fmt: str, *args) -> None:  # 安静模式
        return

    # ---------------- 基础工具 ----------------
    def _send_json(self, obj: dict, code: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path) -> None:
        if not path.is_file():
            self.send_error(404, "not found")
            return
        ctype = {
            ".html": "text/html; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".svg": "image/svg+xml",
            ".woff2": "font/woff2",
        }.get(path.suffix.lower(), "application/octet-stream")
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            # 舞台页面每 500ms 轮询、切版本时浏览器会中途断开连接。
            # 这不是错误，只是对端先挂了 —— 不吞掉的话日志里全是假 Traceback，
            # 真出问题时反而看不见。
            pass

    def _body(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return {}
        if n <= 0:
            return {}
        try:
            raw = self.rfile.read(n)
            obj = json.loads(raw.decode("utf-8"))
            return obj if isinstance(obj, dict) else {}
        except Exception:
            return {}

    # ---------------- 路由 ----------------
    def do_GET(self) -> None:  # noqa: N802
        u = urlparse(self.path)
        p = u.path
        if p in ("/", "/stage.html"):
            self._send_file(ROOT / "stage.html")
            return
        if p == "/api/state":
            since = 0
            try:
                since = int(parse_qs(u.query).get("since", ["0"])[0])
            except ValueError:
                pass
            with STATE_LOCK:
                snap = dict(_state)
                if since and snap["v"] == since:
                    self._send_json({"v": since, "changed": False})
                else:
                    snap["changed"] = True
                    self._send_json(snap)
            return
        if p == "/api/tts/stats":
            self._send_json(tts.stats())
            return
        if p == "/api/audio/diag":
            # 排查"听得到音乐听不到人声"用：看每条混音流里 BGM / TTS 到底有没有挂上
            import audio_out
            pl = audio_out.player()
            self._send_json({
                "devices": pl.devices(),
                "bgm": pl.bgm_state(),
                "mixers": [{"name": m.name, "device": m.device,
                            "bgm_loaded": m._bgm is not None,
                            "bgm_len": int(len(m._bgm)) if m._bgm is not None else 0,
                            "tts_loaded": m._tts is not None} for m in pl._mixers],
            })
            return
        if p == "/api/windows":
            # 三个取景窗口的实时位置（舞台每秒取一次，跟着你拖的窗口走）
            self._send_json({"windows": window_rects()})
            return
        # 静态资源
        safe = (ROOT / p.lstrip("/")).resolve()
        if str(safe).startswith(str(ROOT)) and safe.exists():
            self._send_file(safe)
            return
        self.send_error(404, "not found")

    def do_POST(self) -> None:  # noqa: N802
        p = urlparse(self.path).path
        body = self._body()

        if p == "/api/say":
            text = str(body.get("text") or "").strip()
            if not text:
                self._send_json({"ok": False, "error": "缺少 text"}, 400)
                return
            want_voice = body.get("voice", True)
            # 字幕立刻上屏，不等 TTS。观众看到字的延迟从 2~3 秒降到 0。
            est_hold = _speak_duration(text)
            with STATE_LOCK:
                _state["seq"] += 1
                seq = _state["seq"]
                _state["caption"] = text
                _state["audio"] = ""
                _state["mode"] = "talk"
                _state["speaking_until"] = time.time() + est_hold
                _bump()
            # 开口说话 = 手离开键盘 = 摸鱼
            try:
                import live_state
                live_state.live().on_talk(est_hold)
            except Exception:
                pass
            if want_voice and tts.enabled():
                threading.Thread(
                    target=_tts_worker,
                    args=(seq, text, body.get("voice_name") or None,
                          body.get("style") or None),
                    daemon=True,
                ).start()
            self._send_json({"ok": True, "hold_s": round(est_hold, 1),
                             "audio": "", "tts": {"pending": want_voice and tts.enabled()}})
            return

        if p == "/api/sing":
            lyrics = str(body.get("lyrics") or "").strip()
            if not lyrics:
                self._send_json({"ok": False, "error": "缺少 lyrics"}, 400)
                return
            verse = lyrics[:120]
            # 同样：歌词先上屏进唱歌状态，音频后台补
            with STATE_LOCK:
                _state["seq"] += 1
                seq = _state["seq"]
                _state["caption"] = verse
                _state["audio"] = ""
                _state["mode"] = "sing"
                _state["speaking_until"] = time.time() + 8.0
                _bump()
            try:
                import live_state
                live_state.live().on_talk(8.0)
            except Exception:
                pass
            threading.Thread(
                target=_tts_worker,
                args=(seq, "(唱歌)" + verse, body.get("voice_name") or None, ""),
                daemon=True,
            ).start()
            self._send_json({"ok": True, "hold_s": 8.0, "audio": "",
                             "tts": {"pending": True}})
            return

        if p == "/api/tts/stats":
            self._send_json(tts.stats())
            return

        if p == "/api/danmaku":
            uname = str(body.get("uname") or "水友")[:16]
            text = str(body.get("text") or "").strip()[:80]
            if not text:
                self._send_json({"ok": False, "error": "缺少 text"}, 400)
                return
            item = {"uname": uname, "text": text, "ts": round(time.time(), 3),
                    "id": f"{int(time.time()*1000)}-{len(_danmaku)}"}
            with STATE_LOCK:
                _danmaku.append(item)
                del _danmaku[:-12]
                _state["danmaku"] = list(_danmaku)
                _bump()
            # 弹幕同时喂给状态机（余额 +2）和话题引擎（更新"现在在聊什么"）
            try:
                import live_state
                import topic_engine
                live_state.live().on_chat(text)
                topic_engine.engine().observe(text, uname)
                with STATE_LOCK:
                    _state["topic"] = topic_engine.engine().label()
            except Exception:
                pass
            self._send_json({"ok": True, "item": item})
            return

        if p == "/api/title":
            with STATE_LOCK:
                if body.get("title") is not None:
                    _state["title"] = str(body["title"])[:40]
                if body.get("sub") is not None:
                    _state["sub"] = str(body["sub"])[:60]
                # 底部标题卡：传 line1/line2 就显示，传空就收起
                if body.get("line1") is not None:
                    _state["line1"] = str(body["line1"])[:16]
                if body.get("line2") is not None:
                    _state["line2"] = str(body["line2"])[:20]
                _bump()
            self._send_json({"ok": True})
            return

        if p == "/api/event":
            # 直播间事件：礼物 / 点赞 / 进场。余额经济靠它驱动。
            kind = str(body.get("kind") or "chat").lower()
            try:
                import live_state
                lv = live_state.live()
                if kind == "gift":
                    lv.on_gift(int(body.get("amount") or 1), str(body.get("name") or ""))
                elif kind == "like":
                    lv.on_like(int(body.get("count") or 1))
                elif kind == "member":
                    lv.on_member()
                elif kind == "chat":
                    txt = str(body.get("text") or "")
                    # 点歌：观众发「点歌 雨声」→ 她放下活去放歌（算摸鱼）
                    if "点歌" in txt:
                        rest = txt.split("点歌", 1)[1].strip(" 　:：")
                        lv.on_song(rest, str(body.get("name") or ""))
                    lv.on_chat(txt)
                with STATE_LOCK:
                    _state["live"] = lv.snapshot()
                    _bump()
                self._send_json({"ok": True, "balance": lv.balance()})
            except Exception as e:
                self._send_json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)
            return

        if p == "/api/clear":
            with STATE_LOCK:
                _state["seq"] += 1        # 让还没回来的 TTS 作废，别清完又冒一声
                _state["caption"] = ""
                _state["audio"] = ""
                _state["mode"] = "talk"
                _state["speaking_until"] = 0.0
                _danmaku.clear()
                _state["danmaku"] = []
                _bump()
            self._send_json({"ok": True})
            return

        self.send_error(404, "not found")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8790)
    ap.add_argument("--no-audio", action="store_true",
                    help="不播 BGM、不推虚拟声卡（起第二个测试实例时用）")
    ap.add_argument("--state-dir", default="",
                    help="状态文件目录（默认 data/；测试实例指到别处，别跟线上抢余额）")
    args = ap.parse_args()

    import os
    if args.no_audio:
        os.environ["STAGE_NO_AUDIO"] = "1"
    if args.state_dir:
        os.environ["STAGE_STATE_DIR"] = args.state_dir
        import live_state as _ls
        from pathlib import Path as _P
        _ls.STORE = _P(args.state_dir) / "live_state.json"
        _ls._live = _ls.LiveState(_ls.STORE)

    # 打工面板：每 3 秒看一眼她到底在不在写代码
    try:
        import work_monitor
        mon = work_monitor.WorkMonitor()

        def _watch() -> None:
            global _work_snap
            while True:
                try:
                    snap = mon.tick()
                    _work_snap = snap
                    with STATE_LOCK:
                        _state["work"] = snap
                        _bump()
                except Exception:
                    pass
                time.sleep(3)

        threading.Thread(target=_watch, daemon=True).start()
        print("[√] 打工面板已启动（工资/专注/摸鱼/手速）")
    except Exception as e:
        print(f"[!] 打工面板启动失败：{type(e).__name__}: {e}")

    # 状态机 + 余额经济：每 0.5 秒推一次，屏幕上的数字才有"一直在动"的感觉
    try:
        import live_state
        import topic_engine
        lv = live_state.live()
        tp = topic_engine.engine()

        def _live_loop() -> None:
            last_topic_tick = 0.0
            while True:
                try:
                    with STATE_LOCK:
                        w = dict(_work_snap)
                    snap = lv.tick(w)
                    now = time.time()
                    if now - last_topic_tick > 60:
                        tp.tick()          # 话题热度衰减 + 降级
                        last_topic_tick = now
                    with STATE_LOCK:
                        _state["live"] = snap
                        _state["topic"] = tp.label()
                        _bump()
                except Exception as e:
                    print(f"[live] {type(e).__name__}: {e}", flush=True)
                time.sleep(0.5)

        threading.Thread(target=_live_loop, daemon=True).start()
        print("[√] 状态机已启动（余额 token / 摸鱼被抓 / 话题）")
    except Exception as e:
        print(f"[!] 状态机启动失败：{type(e).__name__}: {e}")

    # 背景音乐：bgm/ 目录里有歌就循环播，没有就静默跳过
    if os.environ.get("STAGE_NO_AUDIO") == "1":
        print("[BGM] 已跳过（--no-audio）")
    else:
        try:
            import audio_out
            r = audio_out.player().start_bgm()
            if not r.get("ok"):
                print(f"[BGM] 未启动（{r.get('error')}），把音频丢进 bgm/ 即可")
        except Exception as e:
            print(f"[!] BGM 启动失败：{type(e).__name__}: {e}")

    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"舞台已启动: http://127.0.0.1:{args.port}/stage.html")
    print(f"控制台:     http://127.0.0.1:{args.port}/panel.html")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    main()
