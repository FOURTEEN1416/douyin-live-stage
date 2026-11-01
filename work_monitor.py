"""打工人监测：判断大肥鱼此刻是在认真写代码、还是在摸鱼。

为什么要这个：
    直播间留不住人的根因不是内容不好，是**没有可追的剧情**。观众看一会儿
    就走，因为不知道下一秒会发生什么。对面那种「今日工资 700/6000、专注 8
    分钟、被抓 1 次」的直播间，观众留下来是想看进度涨、想看被抓现场。

    所以这里把真实行为变成可看的数据：
      - 前台窗口是不是代码窗口  → 专注 / 摸鱼
      - 键鼠空闲多久            → 挂机
      - 每分钟敲了多少次        → 手速

    全部用 Win32 原生接口，不装任何东西。

数据落在 STATE 里，由 server 每 3 秒刷一次，前端只负责显示。
"""

from __future__ import annotations

import ctypes
import time
from ctypes import wintypes as wt

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

SALARY_MAX = 6000          # 日工资上限（对标对方直播间的 6000）
PAY_PER_MIN = 80           # 专注一分钟给多少钱（12 太慢，8 小时才满 6000，观众看不出在动）
FINE_PER_SLACK = 200       # 摸鱼被抓一次扣多少（按新工资比例放大）
SLACK_GRACE = 6.0          # 离开代码窗口超过这么多秒才算摸鱼
IDLE_GRACE = 90.0          # 键鼠 90 秒没动 = 挂机


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.UINT), ("dwTime", wt.DWORD)]


def idle_seconds() -> float:
    """键鼠空闲了多少秒。"""
    info = LASTINPUTINFO()
    info.cbSize = ctypes.sizeof(info)
    if not user32.GetLastInputInfo(ctypes.byref(info)):
        return 0.0
    now = kernel32.GetTickCount64() if hasattr(kernel32, "GetTickCount64") else kernel32.GetTickCount()
    delta = (now - info.dwTime) & 0xFFFFFFFF
    return delta / 1000.0


def foreground_title() -> str:
    h = user32.GetForegroundWindow()
    if not h:
        return ""
    n = user32.GetWindowTextLengthW(h)
    if n <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(h, buf, n + 1)
    return buf.value.strip()


# 在写代码的窗口（算专注）
CODE_KEYWORDS = ("zcode", "workbuddy", "vscode", "code", "cursor", "pycharm",
                 "idea", "sublime", "terminal", "powershell", "cmd")
# 明显在玩的窗口（算摸鱼，被抓就扣钱）
SLACK_KEYWORDS = ("微信", "weixin", "bilibili", "哔哩", "youtube", "抖音", "douyin",
                  "游戏", "steam", "qqmusic", "网易云", "cloudmusic", "视频")


class WorkMonitor:
    """把「在不在写代码」翻译成工资、专注时长、摸鱼次数、手速。

    判定分三档：在代码类窗口 = 专注；在娱乐类窗口 = 摸鱼；其它（桌面、
    设置、资源管理器之类）= 中性，既不涨也不扣 —— 免得接个水就被扣钱。
    """

    def __init__(self, code_keywords: tuple[str, ...] = CODE_KEYWORDS,
                 slack_keywords: tuple[str, ...] = SLACK_KEYWORDS):
        self.code_keywords = code_keywords
        self.slack_keywords = slack_keywords
        self.salary = 0
        self.focus_sec = 0.0        # 连续专注（断了就清零）
        self.total_focus = 0.0      # 本场累计专注
        self.slack_count = 0        # 被抓次数
        self.slack_sec = 0.0        # 本次连续摸鱼
        self.speed = 1.0            # 手速倍率
        self.status = "focus"       # focus | slack | caught | idle
        self._last = time.time()
        self._last_idle = 0.0
        self._typing_events: list[float] = []
        self._started = time.time()

    def _where(self) -> str:
        t = foreground_title().lower()
        if any(k.lower() in t for k in self.code_keywords):
            return "code"
        if any(k.lower() in t for k in self.slack_keywords):
            return "slack"
        return "neutral"

    def tick(self) -> dict:
        now = time.time()
        dt = max(0.0, min(now - self._last, 10.0))
        self._last = now

        idle = idle_seconds()
        where = self._where()

        # 手速：用「输入空闲时间突然归零」的次数估打字频率
        if idle < self._last_idle - 0.05:
            self._typing_events.append(now)
        self._last_idle = idle
        self._typing_events = [t for t in self._typing_events if now - t < 10]
        # 10 秒内 20 次输入 ≈ 1.0 倍速；封顶 5 倍
        self.speed = round(min(5.0, max(0.5, len(self._typing_events) / 20.0)), 1)

        if idle >= IDLE_GRACE:
            self.status = "idle"
            self.focus_sec = 0.0
        elif where == "code":
            self.status = "focus"
            self.focus_sec += dt
            self.total_focus += dt
            self.slack_sec = 0.0
            self.salary = min(SALARY_MAX, self.salary + PAY_PER_MIN * dt / 60.0)
        elif where == "slack":
            self.slack_sec += dt
            if self.slack_sec >= SLACK_GRACE:
                if self.status not in ("slack", "caught"):
                    self.slack_count += 1        # 被抓
                    self.salary = max(0, self.salary - FINE_PER_SLACK)
                self.status = "caught" if self.slack_sec < SLACK_GRACE + 3 else "slack"
                self.focus_sec = 0.0             # 专注断了
        else:
            # 中性窗口（桌面/设置/资源管理器）：不算摸鱼，但也不涨钱
            self.status = "focus" if self.focus_sec > 0 else "idle"
            self.slack_sec = 0.0
        return self.snapshot()

    def snapshot(self) -> dict:
        return {
            "salary": int(self.salary),
            "salary_max": SALARY_MAX,
            "focus_sec": int(self.focus_sec),
            "focus_min": int(self.total_focus // 60),
            "slack_count": self.slack_count,
            "speed": self.speed,
            "status": self.status,
            "uptime_min": int((time.time() - self._started) // 60),
        }


if __name__ == "__main__":
    m = WorkMonitor()
    print("每 2 秒打一次快照，切窗口看 status 变化（Ctrl+C 停）")
    try:
        while True:
            s = m.tick()
            print(f"  工资 {s['salary']:>4}/{s['salary_max']}  专注 {s['focus_sec']:>3}s  "
                  f"累计 {s['focus_min']}min  被抓 {s['slack_count']} 次  "
                  f"手速 ×{s['speed']}  {s['status']}")
            time.sleep(2)
    except KeyboardInterrupt:
        pass
