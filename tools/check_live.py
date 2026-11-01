"""上线体检 —— 一次把「该看的都看了」，重启后跑这一条就够。

查八件事：
    1. 进程 / 端口        服务在不在
    2. 接口               /api/state 有没有 live / topic 字段
    3. 静态资源           stage.html/css/js + 12 张立绘，有没有 404
    4. 悬浮窗             在不在、**是不是置顶**、鼠标穿透、尺寸
    5. 悬浮窗出画         抓一帧看像素——DOM 正常不等于出画，全黑直播伴侣就采黑屏
    6. 合成画面           bg.jpg 能不能取到、画面是不是空的
    7. 画面比例           窗口比例 vs 槽位比例、裁掉多少、放大几倍
    8. 日志               最近有没有 [x] / [!] 报错

用法：
    python tools/check_live.py
    python tools/check_live.py --stage http://127.0.0.1:8790
"""

from __future__ import annotations

import argparse
import ctypes
import json
import re
import sys
import time
import urllib.request
from ctypes import wintypes as wt
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

user32 = ctypes.windll.user32

GWL_EXSTYLE = -20
WS_EX_TOPMOST = 0x00000008
WS_EX_TRANSPARENT = 0x00000020
WS_EX_LAYERED = 0x00080000
WS_EX_TOOLWINDOW = 0x00000080

AVATARS = ["base", "blink", "talk", "sing", "happy", "panic",
           "smug", "bored", "wow", "sad", "away", "sleep"]

OK, BAD, WARN = "✓", "✗", "!"


def http_code(url: str, timeout: float = 4.0) -> tuple[int, int]:
    """返回 (状态码, 字节数)。连不上返回 (0, 0)。"""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            data = r.read()
            return r.status, len(data)
    except Exception:
        return 0, 0


def find_stage_window(title: str = "大肥鱼舞台"):
    """找悬浮窗，返回 (hwnd, rect, exstyle)。"""
    import windows_layout as wl
    w = wl.find_window(title, wl.list_windows())
    if not w:
        return None, None, 0
    hwnd = w["hwnd"]
    ex = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    return hwnd, w["rect"], ex


def grab_stage_frame(hwnd: int, wait: float = 1.2):
    """抓悬浮窗自己一帧，返回 (平均亮度, 近黑占比)；抓不到返回 None。

    为什么要抓：悬浮窗是**分层透明**窗口。WebView2 偶尔只跑 DOM 不出画
    （evaluate_js 一切正常、bg 的 naturalWidth 也是 1080），窗口本身却是透明的，
    直播伴侣按「窗口捕获」采过去就是**整屏黑**。
    DOM 正常 ≠ 出画，所以这里必须看像素。
    """
    try:
        import numpy as np
        from windows_capture import WindowsCapture
    except Exception:
        return None

    frames: list = []

    # 注意：windows_capture 是按**函数名**认事件处理器的，
    # 必须叫 on_frame_arrived / on_closed，写成 on_frame 会直接 ValueError
    def on_closed():
        pass

    def on_frame_arrived(f, _c):
        if len(frames) < 2:
            frames.append(np.asarray(f.frame_buffer))

    try:
        cap = WindowsCapture(window_hwnd=hwnd, cursor_capture=False)
        cap.event(on_frame_arrived)
        cap.event(on_closed)
        ctrl = cap.start_free_threaded()
        time.sleep(wait)
        try:
            ctrl.stop()
        except Exception:
            pass
    except Exception:
        return None
    if not frames:
        return None
    a = frames[-1][:, :, :3].astype("float32").mean(axis=2)
    return float(a.mean()), float((a < 18).mean())


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", default="http://127.0.0.1:8790")
    ap.add_argument("--comp", default="http://127.0.0.1:8791")
    args = ap.parse_args()

    rows: list[tuple[str, str, str]] = []

    def add(ok: str, name: str, detail: str = "") -> None:
        rows.append((ok, name, detail))

    # ---------- 1. 接口 ----------
    try:
        raw = urllib.request.urlopen(f"{args.stage}/api/state", timeout=4).read()
        st = json.loads(raw)
        lv = st.get("live") or {}
        add(OK if lv else WARN, "舞台接口",
            f"{lv.get('status_text','—')} 余额 {lv.get('balance',0)} token "
            f"约能聊 {lv.get('minutes_left',0)} 分钟 话题「{st.get('topic') or '—'}」"
            if lv else "返回里没有 live 字段（后端还是旧版？）")
    except Exception as e:
        add(BAD, "舞台接口", f"{type(e).__name__}")

    # ---------- 2. 静态资源 ----------
    for path in ("/stage.html", "/stage.css", "/stage.js"):
        c, n = http_code(f"{args.stage}{path}")
        add(OK if c == 200 else BAD, path, f"{c} {n}B")

    missing = []
    for name in AVATARS:
        c, _ = http_code(f"{args.stage}/avatar/sit/{name}.webp")
        if c != 200:
            missing.append(name)
    add(OK if not missing else WARN, "立绘 12 张",
        "全部就位" if not missing else f"缺 {missing}")

    # ---------- 3. 悬浮窗 ----------
    hwnd, rect, ex = find_stage_window()
    if not hwnd:
        add(BAD, "悬浮窗", "没找到「大肥鱼舞台」")
    else:
        topmost = bool(ex & WS_EX_TOPMOST)
        clickthru = bool(ex & WS_EX_TRANSPARENT)
        layered = bool(ex & WS_EX_LAYERED)
        add(OK if not topmost else WARN, "悬浮窗",
            f"hwnd={hwnd} {rect[2]}×{rect[3]}  "
            f"置顶={'是' if topmost else '否'}  "
            f"穿透={'是' if clickthru else '否'}  分层={'是' if layered else '否'}")
        if topmost:
            add(WARN, "置顶", "现在是置顶的——会挡住你操作；要关掉见 overlay_win.py --on-top")

        # ---------- 3.5 悬浮窗到底有没有在出画 ----------
        st = grab_stage_frame(hwnd)
        if st is None:
            add(WARN, "悬浮窗出画", "抓不到帧（WGC 失败，可能窗口刚起来）")
        else:
            mean, blk = st
            if mean < 12 or blk > 0.97:
                add(BAD, "悬浮窗出画",
                    f"整屏黑（亮度 {mean:.0f}）—— 直播伴侣采到就是黑屏，重启悬浮层")
            elif mean > 250:
                add(WARN, "悬浮窗出画",
                    f"整屏白（亮度 {mean:.0f}）—— WebView2 首帧没画出来")
            else:
                add(OK, "悬浮窗出画",
                    f"亮度 {mean:.0f}，黑占比 {blk * 100:.1f}%")

    # ---------- 4. 合成画面 ----------
    c, n = http_code(f"{args.comp}/bg.jpg", timeout=6)
    if c != 200:
        add(BAD, "合成画面", f"{args.comp}/bg.jpg 取不到（{c}）")
    else:
        # 体积太小基本是空画面
        add(OK if n > 20000 else WARN, "合成画面",
            f"{n // 1024} KB" + ("" if n > 20000 else "（偏小，可能窗口都最小化了）"))

    # ---------- 5. 画面比例 ----------
    try:
        import check_frame as cf
        cw, ch, slots = cf.load_slots()
        wins = json.loads(urllib.request.urlopen(
            f"{args.stage}/api/windows", timeout=4).read())["windows"]
        for w in wins:
            key, rect = w.get("key"), w.get("rect")
            if key not in slots:
                continue
            sx, sy, sw, sh = slots[key]
            if not rect:
                add(WARN, f"画面 {key}", "窗口没找到（最小化了？）")
                continue
            # /api/windows 给的是逻辑像素，合成器抓的是物理像素，要乘 DPI
            d = cf.dpi_scale()
            ww, hh = int(round(rect[2] * d)), int(round(rect[3] * d))
            m = cf.cover_math(ww, hh, sw, sh)
            err = abs(ww / hh - sw / sh) / (sw / sh) * 100
            if err > 3:
                axis = "左右" if m["cut_x_pct"] >= m["cut_y_pct"] else "上下"
                cut = max(m["cut_x_pct"], m["cut_y_pct"])
                add(BAD, f"画面 {key}",
                    f"{ww}×{hh} 比例 {ww/hh:.3f} ≠ 槽位 {sw/sh:.3f}，"
                    f"{axis}裁 {cut}%，放大 {m['scale']}×")
            elif m["scale"] > 1.3:
                add(WARN, f"画面 {key}",
                    f"{ww}×{hh} 比例对，但放大 {m['scale']}×（会糊）")
            else:
                add(OK, f"画面 {key}",
                    f"{ww}×{hh} 比例对，放大 {m['scale']}×")
    except Exception as e:
        add(WARN, "画面比例", f"算不出来：{type(e).__name__}")

    # ---------- 6. 日志 ----------
    logs = sorted((ROOT / "logs").glob("run-*.log"), key=lambda p: p.stat().st_mtime)
    if logs:
        lines = logs[-1].read_text(encoding="utf-8", errors="ignore").splitlines()[-400:]
        errs = [x for x in lines if ("[!]" in x or "[x]" in x)]
        add(OK if not errs else WARN, "日志",
            "最近 400 行没有报错" if not errs else f"{len(errs)} 条：" + errs[-1][:70])

    # ---------- 输出 ----------
    print()
    print(f"{'':<2}{'项目':<14}说明")
    print("-" * 78)
    for ok, name, detail in rows:
        print(f"{ok} {name:<14}{detail}")
    bad = sum(1 for r in rows if r[0] == BAD)
    warn = sum(1 for r in rows if r[0] == WARN)
    print("-" * 78)
    print(f"{len(rows)} 项检查：{len(rows)-bad-warn} 通过，{warn} 警告，{bad} 失败")
    print()


if __name__ == "__main__":
    main()
