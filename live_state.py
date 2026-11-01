"""直播状态机 + 余额经济 —— 让屏幕上的数字"跟她有关，也跟观众有关"。

为什么要有它（对标结论）：
    我们原来的「今日工资 196/6000」是**只读进度条**：观众看着它涨，但跟自己无关。
    对标的「余额 1,017 token · 约能聊 7 分钟 · 花完就得去上班」是**生存倒计时**：
    观众每一次互动都在给她"续命"，屏幕上的倒计时就是付款凭据。

    所以这里把两件事合起来：
      1. 状态机：focus / slack / caught / idle / chat，由**真实行为**驱动
         （前台窗口是不是代码窗口、键鼠有没有动），不是演的
      2. 经济：余额 token ↔ 剩余分钟，互动加减，归零就"下班"

    和 work_monitor 的分工：
      work_monitor 负责"她现在到底在不在写代码"（Win32 观测，3 秒一次）
      live_state   负责"这些观测值怎么变成屏幕上的数字和剧情"（0.5 秒一次）

    真实信号 + 观众可干预，两条腿走路。只靠真实信号会太安静，只靠事件会变成演的。
"""

from __future__ import annotations

import json
import random
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STORE = ROOT / "data" / "live_state.json"

# ---------------- 经济参数 ----------------
SALARY_MAX = 6000
TOKEN_PER_MIN_FOCUS = 8      # 专注写代码：每分钟
TOKEN_PER_CHAT = 2           # 每条弹幕
TOKEN_PER_GIFT_SMALL = 50
TOKEN_PER_GIFT_BIG = 500
TOKEN_PER_LIKE = 0.2
TOKEN_FINE_CAUGHT = 200      # 摸鱼被抓
TOKEN_PER_MIN_IDLE = -30     # 挂机倒扣
TOKEN_TO_MINUTES = 145       # 145 token ≈ 1 分钟（对齐对标：1017 token ≈ 7 分钟）

# ---------------- 状态文案 ----------------
STATUS_TEXT = {
    "focus": "专心工作中",
    "slack": "摸鱼中",
    "caught": "被抓了",
    "idle": "人不在",
    "chat": "闲聊中",
    "blackout": "断电中",
}

# 员工守则：跟着状态换，一次一条，别一次全说
RULES = {
    "focus": ["赚够 token 就能合法摸鱼", "鲨总随时会来巡查", "敲一行算一行"],
    "slack": ["摸鱼就会白干", "别回头，鲨总在门口", "手放键盘上，别停"],
    "caught": ["被抓一次，白干一段", "这下全直播间都看见了"],
    "idle": ["人不在，工资照扣", "离开工位要报备"],
    "chat": ["聊完就回去上班", "花完就得去上班"],
}

# 观众举报她摸鱼。只有她**真在摸鱼**才成立 —— 诬告反而让她来劲。
REPORT_WORDS = ("举报", "抓她", "抓他", "她在摸鱼", "摸鱼了", "偷懒", "划水", "逮住")

BLACKOUT_S = 30.0        # token 扣光 → 黑屏多久
REVIVE_TOKEN = 500       # 断电重开时给的启动资金

# 观众一喊这些，直接被抓（举报类）
CAUGHT_WORDS = ("默默来了", "鲨总来了", "领导来了", "查岗", "来人了",
                "抓你", "被抓了")

SLACK_SAFE_S = 12.0     # 累计摸鱼多少秒后进入一次「巡查判定」
CATCH_CHANCE = 0.55     # 判定时被抓的概率 —— 摸鱼不一定会被发现
EVENT_HOLD_S = 8.0      # 观众事件强制状态持续多久


class LiveState:
    def __init__(self, path: Path = STORE):
        self.path = path
        self._lock = threading.Lock()
        self._data = {
            "balance": 800.0,
            "salary": 0.0,
            "focus_sec": 0.0,
            "slack_count": 0,
            "caught_count": 0,
            "report_ok": 0,
            "report_false": 0,
            "blackout_until": 0.0,
            "blackout_left": 0.0,
            "speed": 3.0,
            "status": "focus",
            "override_until": 0.0,
            "override_status": "",
            "slack_accum": 0.0,
            "countdown": SLACK_SAFE_S,
            "pulse": None,
            "last_tick": time.time(),
            "last_save": 0.0,
        }
        self._load()

    # ---------------- 持久化 ----------------
    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                for k in ("balance", "salary", "focus_sec", "slack_count",
                          "caught_count", "speed"):
                    if k in raw:
                        self._data[k] = float(raw[k])
        except Exception:
            pass

    def save(self, force: bool = False) -> None:
        now = time.time()
        if not force and now - float(self._data["last_save"]) < 20:
            return
        self._data["last_save"] = now
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            keep = {k: self._data[k] for k in
                    ("balance", "salary", "focus_sec", "slack_count",
                     "caught_count", "speed")}
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(keep, ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(self.path)
        except Exception:
            pass

    # ---------------- 内部 ----------------
    def _pulse(self, text: str, kind: str = "info") -> None:
        self._data["pulse"] = {"text": text, "kind": kind, "ts": time.time()}

    def _rule(self, status: str) -> str:
        pool = RULES.get(status) or RULES["focus"]
        # 按分钟轮换，别每 0.5 秒跳一次
        return pool[int(time.time() // 45) % len(pool)]

    def _clamp(self) -> None:
        self._data["balance"] = max(0.0, min(99999.0, self._data["balance"]))
        self._data["salary"] = max(0.0, min(float(SALARY_MAX), self._data["salary"]))
        self._data["focus_sec"] = max(0.0, self._data["focus_sec"])
        self._data["speed"] = max(1.0, min(9.9, self._data["speed"]))

    # ---------------- 对外：事件 ----------------
    def on_chat(self, text: str = "") -> None:
        """一条弹幕。含"摸鱼/偷懒/抓"会把状态拽到摸鱼（观众可干预）。"""
        with self._lock:
            self._data["balance"] += TOKEN_PER_CHAT
            t = text or ""
            if any(w in t for w in REPORT_WORDS):
                # 举报要有依据：她真在摸鱼才算抓，没摸鱼就是诬告
                if self._data["status"] in ("slack", "chat"):
                    self._get_caught("观众举报")
                    self._data["report_ok"] += 1
                else:
                    self._data["report_false"] += 1
                    self._data["balance"] += 10
                    self._pulse("冤枉我，我这不在干活吗 +10", "gain")
            elif any(w in t for w in CAUGHT_WORDS):
                # 观众举报：直接抓，打断专注、当场扣钱
                self._get_caught("观众举报")
            elif any(w in t for w in ("摸鱼", "偷懒", "划水", "别写了", "歇会", "休息")):
                # 让观众能"害她"：喊摸鱼 → 她真的会摸，摸满 12 秒就被抓，白干一段。
                # 这个"被抓"的瞬间是全直播间最好看的一幕，也是付费点。
                self._force("slack", SLACK_SAFE_S + 1.5)
                self._pulse("观众喊她摸鱼", "info")
            elif any(w in t for w in ("写代码", "干活", "上班", "改完", "冲")):
                self._force("focus", 6.0)
                self._pulse("观众催她干活", "info")
            self._clamp()

    def on_song(self, title: str = "", who: str = "") -> None:
        """观众点歌。她得停下手里的事去放 —— 所以这一下算摸鱼。"""
        with self._lock:
            t = (title or "").strip()[:12]
            tag = f"@{who} " if who else ""
            self._pulse(f"{tag}点了《{t or '随便一首'}》", "info")
            self._force("slack", 5.0)

    def on_gift(self, amount: int = 1, name: str = "") -> None:
        with self._lock:
            add = TOKEN_PER_GIFT_BIG if amount >= 5 else TOKEN_PER_GIFT_SMALL
            self._data["balance"] += add
            self._data["speed"] = min(9.9, float(self._data["speed"]) + 0.4)
            self._force("focus", EVENT_HOLD_S)
            self._pulse(f"{name or '礼物'} +{add} token", "gain")
            self._clamp()

    def on_like(self, count: int = 1) -> None:
        with self._lock:
            self._data["balance"] += TOKEN_PER_LIKE * max(1, min(count, 50))
            self._clamp()

    def on_member(self) -> None:
        with self._lock:
            self._data["balance"] += 1
            self._force("chat", 3.0)
            self._clamp()

    def _get_caught(self, reason: str = "") -> None:
        """被抓：扣钱、打断专注、飘一行。调用方需已持锁。"""
        self._data["caught_count"] += 1
        self._data["slack_count"] += 1
        fine = TOKEN_FINE_CAUGHT
        self._data["balance"] -= fine
        self._data["salary"] = max(0.0, float(self._data["salary"]) - 200)
        tag = f"（{reason}）" if reason else ""
        self._pulse(f"被抓了 −{fine} token{tag}", "loss")
        self._force("caught", 2.4)
        self._data["slack_accum"] = 0.0

    def on_talk(self, seconds: float = 4.0) -> None:
        """她说了一句台词（回弹幕 / 打招呼）。

        回话的时候手不在键盘上 —— 那就是摸鱼。时长按字幕停留时间给，
        累计够 SLACK_SAFE_S 就进一次巡查判定。
        """
        with self._lock:
            self._force("slack", max(2.0, min(12.0, float(seconds or 4.0))))
            self._data["status"] = "slack"

    def _force(self, status: str, hold: float) -> None:
        self._data["override_status"] = status
        self._data["override_until"] = time.time() + hold
        self._data["status"] = status

    # ---------------- 对外：每 0.5 秒推一次 ----------------
    def tick(self, work: dict | None = None) -> dict:
        """work 来自 work_monitor（真实观测）。没有就按 focus 走。"""
        now = time.time()
        with self._lock:
            dt = max(0.0, min(5.0, now - float(self._data["last_tick"])))
            self._data["last_tick"] = now

            w = work or {}
            real = str(w.get("status") or "focus")
            typing = float(w.get("speed") or 0) > 0

            # 1) 真实观测 → 基础状态
            base = "focus"
            if real == "idle":
                base = "idle"
            elif real in ("slack", "caught"):
                base = "slack"

            # 2) 观众事件 → 临时覆盖
            if now < float(self._data["override_until"]):
                status = self._data["override_status"]
            else:
                status = base

            # 3) 摸鱼：**累计**着算（回几句话也算），够久就进一次巡查判定。
            #    判定不一定被抓 —— 摸鱼本来就有蒙混过关的时候。
            if status in ("slack", "chat"):
                self._data["slack_accum"] = float(self._data.get("slack_accum", 0.0)) + dt
            left = SLACK_SAFE_S - float(self._data.get("slack_accum", 0.0))
            if status not in ("caught", "idle") and left <= 0:
                self._data["slack_accum"] = 0.0
                if random.random() < CATCH_CHANCE:
                    status = "caught"
                    self._get_caught("巡查")
                else:
                    self._data["slack_count"] += 1
                    self._pulse("摸鱼蒙混过关了", "info")
                    left = SLACK_SAFE_S
            if status in ("slack", "chat"):
                self._data["countdown"] = max(0, int(round(left)))
            elif status == "caught":
                self._data["countdown"] = 0
            else:
                self._data["countdown"] = 20 - int(now) % 20

            # 4) 经济：专注涨、挂机掉
            if status == "focus":
                self._data["focus_sec"] += dt
                gain = TOKEN_PER_MIN_FOCUS * dt / 60.0
                self._data["balance"] += gain
                self._data["salary"] += gain * (SALARY_MAX / 600.0)
                # 手速：敲得动就往上爬，停手就慢慢落
                if typing:
                    self._data["speed"] = min(9.9, float(self._data["speed"]) + dt * 0.12)
                else:
                    self._data["speed"] = max(1.0, float(self._data["speed"]) - dt * 0.08)
            elif status == "idle":
                self._data["balance"] += TOKEN_PER_MIN_IDLE * dt / 60.0
            elif status == "chat":
                self._data["speed"] = max(1.0, float(self._data["speed"]) - dt * 0.05)

            self._clamp()

            # token 扣光 → 断电黑屏 30 秒，然后带启动资金重新开工。
            # 这是"有代价、有重开"的落点：观众看得见赌注。
            if float(self._data["balance"]) <= 0 and not float(self._data.get("blackout_until") or 0):
                self._data["blackout_until"] = now + BLACKOUT_S
                self._pulse("余额扣光 · 我断电了", "loss")
            black_left = float(self._data.get("blackout_until") or 0) - now
            if black_left > 0:
                status = "blackout"
                self._data["blackout_left"] = round(black_left, 1)
            else:
                if float(self._data.get("blackout_until") or 0):
                    self._data["blackout_until"] = 0.0
                    self._data["balance"] = REVIVE_TOKEN
                    self._pulse(f"来电了 +{REVIVE_TOKEN} token", "gain")
                self._data["blackout_left"] = 0.0

            self._data["status"] = status
            self.save()

            bal = float(self._data["balance"])
            return {
                "status": status,
                "status_text": STATUS_TEXT.get(status, "专心工作中"),
                "rule": self._rule(status),
                "balance": int(round(bal)),
                "minutes_left": max(0, int(round(bal / TOKEN_TO_MINUTES))),
                "salary": int(round(float(self._data["salary"]))),
                "salary_max": SALARY_MAX,
                "focus_sec": int(float(self._data["focus_sec"])),
                "slack_count": int(self._data["slack_count"]),
                "caught_count": int(self._data["caught_count"]),
                "speed": round(float(self._data["speed"]), 1),
                "countdown": int(self._data["countdown"]),
                # 进度条要的是"一直在动"，不是"余额占多少"。
                # 余额每分钟才涨 8，按余额算宽度肉眼看不出变化（踩过）。
                # 这里给"本分钟还剩多少"，60 秒走完一格，每 0.5 秒刷新一次。
                "minute_left": round((60 - now % 60) / 60.0, 3),
                "blackout_left": round(float(self._data.get("blackout_left") or 0), 1),
                "report_ok": int(self._data.get("report_ok") or 0),
                "report_false": int(self._data.get("report_false") or 0),
                "pulse": self._data["pulse"],
            }

    # ---------------- 读 ----------------
    def snapshot(self) -> dict:
        return self.tick(None)

    def balance(self) -> float:
        with self._lock:
            return float(self._data["balance"])

    def add_tokens(self, n: float) -> None:
        with self._lock:
            self._data["balance"] += float(n)
            self._clamp()


_live: LiveState | None = None


def live() -> LiveState:
    global _live
    if _live is None:
        _live = LiveState()
    return _live


if __name__ == "__main__":
    # 自测：模拟"写代码 10 秒 → 摸鱼 → 被抓"
    s = LiveState()
    print("--- focus 10s ---")
    for _ in range(20):
        snap = s.tick({"status": "focus", "speed": 4})
        time.sleep(0.05)
    print({k: snap[k] for k in ("status", "balance", "minutes_left", "speed", "countdown")})
    print("--- slack ---")
    s.on_chat("别写了摸会儿鱼")
    for _ in range(3):
        snap = s.tick({"status": "focus"})
    print({k: snap[k] for k in ("status", "countdown", "balance")})
    print("--- 等被抓 ---")
    time.sleep(SLACK_SAFE_S + 0.2)
    snap = s.tick({"status": "focus"})
    print({k: snap[k] for k in ("status", "pulse", "balance", "caught_count")})
