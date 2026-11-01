"""把 ZCode 窗口摆成"合适且完整"的尺寸 —— 一条命令搞定。

它做的事（就三件）：
    1. 找到 ZCode 窗口（最小化了就先还原）
    2. 按 9:16（= 合成器槽位 1080×1920 的比例）算出屏幕放得下的最大尺寸
    3. SetWindowPos 摆过去，再回读一次确认

为什么必须 9:16：
    compositor 用的是 fit_cover —— 缩放到刚好盖满槽位，居中裁掉溢出。
    窗口比例 = 槽位比例时，裁掉的是 0；一旦不是，它会**静默裁**。
    实测有一次窗口被拖成 1721×1033（1.666），结果只剩中间 33.8%，
    还被放大 1.86× —— 画面上看不出来，但内容已经废了。

用法：
    python tools/fix_frame.py           # 直接摆好
    python tools/fix_frame.py --dry     # 只算不摆
"""

from __future__ import annotations

import argparse
import ctypes
import json
import sys
from ctypes import wintypes as wt
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import windows_layout as wl  # noqa: E402

user32 = ctypes.windll.user32
SW_RESTORE = 9
MINIMIZED_X = -2000


def screen_size() -> tuple[int, int]:
    user32.SetProcessDPIAware()
    return user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)


def canvas_size() -> tuple[int, int]:
    import re
    src = (ROOT / "compositor.py").read_text(encoding="utf-8")
    m = re.search(r"CANVAS_W\s*,\s*CANVAS_H\s*=\s*(\d+)\s*,\s*(\d+)", src)
    return (int(m.group(1)), int(m.group(2))) if m else (1080, 1920)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="只算不摆")
    ap.add_argument("--key", default="code", help="layout.json 里的槽位名，默认 code")
    args = ap.parse_args()

    cfg = json.loads((ROOT / "layout.json").read_text(encoding="utf-8"))
    item = cfg.get(args.key) or {}
    match = item.get("match") or "ZCode"
    cur = item.get("rect") or [0, 0, 900, 1600]

    cw, ch = canvas_size()
    sw, sh = screen_size()
    # 屏幕放得下的最大 9:16（偶数，避免半像素）
    k = min(sw / cw, sh / ch, 1.0)
    bw, bh = int(cw * k) // 2 * 2, int(ch * k) // 2 * 2
    target = [cur[0], cur[1], bw, bh]

    print(f"屏幕 {sw}×{sh}　画布 {cw}×{ch}")
    print(f"目标窗口 {match}: {bw}×{bh}（比例 {bw / bh:.4f}，槽位 {cw / ch:.4f}）")

    wins = wl.list_windows()
    w = wl.find_window(match, wins)
    if not w:
        print(f"[!] 没找到标题含「{match}」的窗口。先把 ZCode 打开再跑。")
        return

    x, y, ww, hh = w["rect"]
    minimized = x < MINIMIZED_X or y < MINIMIZED_X
    print(f"当前 {ww}×{hh}（比例 {ww / hh:.4f}）"
          f"{'　[已最小化]' if minimized else ''}")
    if ww and hh and abs(ww / hh - cw / ch) / (cw / ch) < 0.03 and not minimized:
        print("[√] 比例已经对了，不用动")
        if (ww, hh) != (bw, bh):
            print(f"    只是尺寸偏小（{ww}×{hh} < {bw}×{bh}），放大一点更清晰")
        else:
            return

    if args.dry:
        print(f"[dry] 会摆成 {target}")
        return

    if minimized:
        user32.ShowWindow(w["hwnd"], SW_RESTORE)
        import time
        time.sleep(0.4)

    if not wl.move(w["hwnd"], target):
        print("[!] SetWindowPos 失败")
        return

    import time
    time.sleep(0.3)
    w2 = wl.find_window(match, wl.list_windows())
    if not w2:
        print("[?] 摆完找不到窗口了，自己看一眼")
        return
    _, _, nw, nh = w2["rect"]
    print(f"[√] 现在是 {nw}×{nh}（比例 {nw / nh:.4f}）")
    print("    下一步：重启合成器让新尺寸生效（python run.py --mode auto）")
    print("    体检：python tools/check_frame.py")


if __name__ == "__main__":
    main()
