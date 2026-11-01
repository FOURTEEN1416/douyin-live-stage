"""直播间的短时记忆：最近聊过什么 + 每个水友是谁。

为什么需要它：
    原来的应答是**单轮无上下文**——每条弹幕都从零开始，她不知道上一条说了
    什么，也不认识回头客。表现出来就是「答得对，但像个客服」。

    这里只做两件事，都刻意做得很轻：
      1. 全局对话流：最近 N 轮（谁说了什么、她回了什么）
      2. 观众档案：昵称、第一次出现、来过几次、最近说过的话

    不搬 ai-girlfriend 的三层记忆（短期/情景/长期）——那套是给一对一的长期
    关系用的，直播间是「很多人 + 短回合」，照搬会又重又慢。这里只要够她
    「接得上话」就行。

存储：data/stage_memory.json（重启不丢，纯文本可直接看/手改）
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STORE = ROOT / "data" / "stage_memory.json"

MAX_DIALOGUE = 40          # 全局对话流最多留多少条
MAX_PER_VIEWER = 4         # 每个观众最多记几句
MAX_VIEWERS = 300          # 观众档案上限，超了淘汰最久没来的


class StageMemory:
    def __init__(self, path: Path = STORE):
        self.path = path
        self._lock = threading.Lock()
        self._data: dict = {"viewers": {}, "dialogue": []}
        self._load()

    # ---------------- 持久化 ----------------
    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                self._data["viewers"] = raw.get("viewers") or {}
                self._data["dialogue"] = raw.get("dialogue") or []
        except Exception:
            pass

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=1),
                           encoding="utf-8")
            tmp.replace(self.path)
        except Exception:
            pass

    # ---------------- 写入 ----------------
    def note_danmaku(self, nick: str, text: str) -> None:
        """记一条观众弹幕。"""
        nick = (nick or "水友").strip()[:16]
        text = (text or "").strip()[:120]
        if not text:
            return
        now = time.time()
        with self._lock:
            v = self._data["viewers"].setdefault(
                nick, {"first": now, "count": 0, "recent": [], "last": now})
            v["count"] = int(v.get("count", 0)) + 1
            v["last"] = now
            recent = v.get("recent") or []
            recent.append(text)
            v["recent"] = recent[-MAX_PER_VIEWER:]
            self._data["dialogue"].append(
                {"nick": nick, "text": text, "who": "viewer", "ts": round(now, 1)})
            self._trim()

    def note_reply(self, text: str) -> None:
        """记一条她说的话。"""
        text = (text or "").strip()[:120]
        if not text:
            return
        with self._lock:
            self._data["dialogue"].append(
                {"nick": "大肥鱼", "text": text, "who": "me", "ts": round(time.time(), 1)})
            self._trim()

    def _trim(self) -> None:
        d = self._data["dialogue"]
        if len(d) > MAX_DIALOGUE:
            self._data["dialogue"] = d[-MAX_DIALOGUE:]
        vs = self._data["viewers"]
        if len(vs) > MAX_VIEWERS:
            oldest = sorted(vs.items(), key=lambda kv: kv[1].get("last", 0))
            for name, _ in oldest[: len(vs) - MAX_VIEWERS]:
                vs.pop(name, None)

    # ---------------- 读取 ----------------
    def recent_dialogue(self, n: int = 8) -> list[str]:
        with self._lock:
            rows = self._data["dialogue"][-n:]
        out = []
        for r in rows:
            who = "我" if r.get("who") == "me" else r.get("nick", "水友")
            out.append(f"{who}：{r.get('text', '')}")
        return out

    def viewer(self, nick: str) -> dict:
        with self._lock:
            return dict(self._data["viewers"].get((nick or "").strip()[:16]) or {})

    def viewer_hint(self, nick: str) -> str:
        """给模型看的「这个人是谁」一句话。没见过的返回空。"""
        v = self.viewer(nick)
        if not v:
            return ""
        count = int(v.get("count", 0))
        if count <= 1:
            return ""
        parts = [f"这人来过 {count} 次"]
        recent = [x for x in (v.get("recent") or []) if x]
        if len(recent) > 1:
            said = "；".join(recent[:-1][-2:])
            parts.append(f"之前说过：{said}")
        return "，".join(parts)

    def is_returning(self, nick: str) -> bool:
        return int(self.viewer(nick).get("count", 0)) > 1

    def stats(self) -> dict:
        with self._lock:
            return {"viewers": len(self._data["viewers"]),
                    "dialogue": len(self._data["dialogue"])}


_memory: StageMemory | None = None


def memory() -> StageMemory:
    global _memory
    if _memory is None:
        _memory = StageMemory()
    return _memory
