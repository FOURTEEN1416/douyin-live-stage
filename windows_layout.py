"""窗口布局工具：把代码窗 / 浏览器 / 微信 自动摆到直播坐标

    python windows_layout.py --list              列出所有可见窗口
    python windows_layout.py --init              生成 layout.json 模板（含建议坐标）
    python windows_layout.py --apply             按 layout.json 摆窗口
    python windows_layout.py --save              把窗口当前位置存回 layout.json

坐标按 2560×1600 屏幕算，对应舞台 1920×1080（OBS 显示器捕获后上下各裁 60）。
换分辨率就用 --save 记一次当前布局，之后 --apply 还原。
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wt
import json
from pathlib import Path

user32 = ctypes.windll.user32
# 64 位下必须声明参数类型，否则句柄/insert-after 会被截断导致 SetWindowPos 失败
user32.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, wt.UINT]
user32.SetWindowPos.restype = wt.BOOL
user32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
user32.ShowWindow.restype = wt.BOOL

SW_RESTORE = 9
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040
SWP_NOMOVE = 0x0002
SWP_NOSIZE = 0x0001
GWL_EXSTYLE = -20
WS_EX_TOPMOST = 0x8
# Win32 常量：HWND_TOP = 0，**HWND_TOPMOST = -1**（别搞混，一置顶就锁死了）
HWND_TOP = 0
HWND_NOTOPMOST = -2

LAYOUT_FILE = Path(__file__).resolve().parent / "layout.json"

# 逻辑坐标（Windows 缩放 150% 下的 1707×1067），舞台锚定顶部 1:1 映射
# 布局：左半 = 代码；右上 = 网站；右下 = 微信（Windows 贴靠）
DEFAULT_LAYOUT = {
    "_note": "rect=[左,上,宽,高] 逻辑像素。match 是窗口标题关键字，改完 --apply。",
    "code":   {"match": "ZCode",  "rect": [0, 0, 853, 1019]},
    "web":    {"match": "唯一的你", "rect": [857, 0, 850, 509]},
    "wechat": {"match": "微信",    "rect": [857, 509, 850, 510]},
}


def list_windows() -> list[dict]:
    out: list[dict] = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
    def cb(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        if n <= 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        title = buf.value.strip()
        if not title:
            return True
        r = wt.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(r))
        w, h = r.right - r.left, r.bottom - r.top
        # 进程 id 和窗口类名：同一个应用开两个窗口（微信多开、两个聊天窗）时，
        # 光靠标题分不出来，得靠这两个字段钉死。
        pid = wt.DWORD(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        cbuf = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cbuf, 256)
        # 不按尺寸过滤：被最小化的窗口坐标是 -32000 的“幽灵”矩形，
        # move() 里的 SW_RESTORE 会把它们还原回来
        out.append({"hwnd": int(hwnd), "title": title,
                    "rect": [r.left, r.top, w, h],
                    "pid": int(pid.value), "cls": cbuf.value})
        return True

    user32.EnumWindows(cb, 0)
    return out


def candidates(match, wins: list[dict], meta: dict | None = None) -> list[dict]:
    """按 match/exclude/pin_pid/pin_hwnd 过滤出所有候选窗口。

    match 支持两种写法：
        "微信"                    → 标题包含即可（老写法）
        ["微信", "文件传输助手"]   → 命中任一关键字
    meta 里还能放：
        exclude   标题黑名单（命中就排除），比如 ["通知", "公众号"]
        pin_pid   只认这个进程的窗口 —— 微信多开时最稳
        pin_hwnd  只认这个窗口句柄（重启就失效，临时用）
    """
    meta = meta or {}
    keys = match if isinstance(match, (list, tuple)) else [match]
    keys = [str(k).lower() for k in keys if str(k).strip()]
    if not keys:
        return []

    pin_pid = meta.get("pin_pid")
    pin_hwnd = meta.get("pin_hwnd")
    excl = [str(x).lower() for x in (meta.get("exclude") or []) if str(x).strip()]

    out = []
    for w in wins:
        title = w.get("title", "").lower()
        if not any(k in title for k in keys):
            continue
        if any(e in title for e in excl):
            continue
        if pin_pid and int(w.get("pid") or 0) != int(pin_pid):
            continue
        if pin_hwnd and int(w.get("hwnd") or 0) != int(pin_hwnd):
            continue
        out.append(w)
    return out


def find_window(match, wins: list[dict], meta: dict | None = None) -> dict | None:
    """从候选里挑一个。多个命中时的挑法由 meta["pick"] 决定：

        nearest（默认，有 target 时） 离配置位置最近的那个 —— 你把要播的那个
                                      拖到直播位，它就一直赢，不管另一个多大
        largest / smallest            面积最大 / 最小
    """
    meta = meta or {}
    cands = candidates(match, wins, meta)
    if not cands:
        return None
    # 标题完全一致的优先（「微信」vs「微信 - 通知」这种）
    keys = match if isinstance(match, (list, tuple)) else [match]
    exact = [w for w in cands if w["title"].strip().lower() in
             {str(k).strip().lower() for k in keys}]
    if exact:
        cands = exact
    if len(cands) == 1:
        return cands[0]

    pick = meta.get("pick") or ("nearest" if meta.get("target") else "largest")
    if pick == "nearest" and meta.get("target"):
        tx, ty = meta["target"][0], meta["target"][1]
        return min(cands, key=lambda w: (w["rect"][0] - tx) ** 2 + (w["rect"][1] - ty) ** 2)
    if pick == "smallest":
        return min(cands, key=lambda w: w["rect"][2] * w["rect"][3])
    return max(cands, key=lambda w: w["rect"][2] * w["rect"][3])


def move(hwnd: int, rect: list[int], front: bool = False) -> bool:
    """挪位置和大小：**不动 z-order、不置顶、不抢前台**

    顺带把目标窗口的「始终置顶」标记摘掉（WS_EX_TOPMOST）。
    """
    x, y, w, h = rect
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
    # HWND_NOTOPMOST：保证窗口不是「始终置顶」；SWP_NOZORDER：不动层叠顺序
    ok = user32.SetWindowPos(hwnd, HWND_NOTOPMOST, x, y, w, h,
                             SWP_NOZORDER | SWP_NOACTIVATE | SWP_SHOWWINDOW)
    return bool(ok)


def clear_topmost() -> int:
    """把所有目标窗口的「始终置顶」标记摘掉（历史遗留 + 应用自带置顶）

    自己枚举窗口，不看可见性——有些应用会留一个隐藏的置顶窗口。
    """
    try:
        cfg = json.loads(LAYOUT_FILE.read_text(encoding="utf-8"))
    except Exception:
        return 0
    keys = [item.get("match", "") for key, item in cfg.items()
            if not key.startswith("_")]
    keys += ["微信", "Weixin"]

    targets = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, ctypes.c_long)
    def cb(h, _):
        n = user32.GetWindowTextLengthW(h)
        if n <= 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(h, buf, n + 1)
        title = buf.value.strip()
        if not title:
            return True
        low = title.lower()
        if not any(k and k.lower() in low for k in keys):
            return True
        r = wt.RECT()
        user32.GetWindowRect(h, ctypes.byref(r))
        if (r.right - r.left) < 150 or (r.bottom - r.top) < 100:
            return True          # 跳过输入法之类的小辅助窗
        targets.append((int(h), title))
        return True

    user32.EnumWindows(cb, 0)

    n = 0
    for hwnd, title in targets:
        if user32.GetWindowLongW(hwnd, GWL_EXSTYLE) & WS_EX_TOPMOST:
            # 取消置顶必须允许改 z-order（带 SWP_NOZORDER 的话 NOTOPMOST 不生效）
            user32.SetWindowPos(hwnd, HWND_NOTOPMOST, 0, 0, 0, 0,
                                SWP_NOACTIVATE | SWP_NOMOVE | SWP_NOSIZE)
            print(f"  [√] 已取消置顶: {title[:28]}")
            n += 1
    return n


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true", help="列出可见窗口")
    ap.add_argument("--init", action="store_true", help="生成 layout.json 模板")
    ap.add_argument("--apply", action="store_true", help="按 layout.json 摆窗口")
    ap.add_argument("--save", action="store_true", help="把当前位置存回 layout.json")
    ap.add_argument("--watch", type=float, default=0,
                    help="守护模式：每 N 秒检查一次，窗口偏离就摆回去")
    ap.add_argument("--unpin", action="store_true", help="取消三个窗口的『始终置顶』")
    args = ap.parse_args()

    wins = list_windows()

    if args.unpin:
        n = clear_topmost()
        print(f"已取消 {n} 个窗口的置顶")
        return

    if args.list:
        print(f"共 {len(wins)} 个可见窗口（按面积排序）\n")
        for w in sorted(wins, key=lambda w: -w["rect"][2] * w["rect"][3])[:25]:
            x, y, ww, hh = w["rect"]
            print(f"  {ww:4d}×{hh:<4d} @{x:5d},{y:<5d}  {w['title'][:56]}")
        print("\n下一步：python windows_layout.py --init 生成配置，改 match 后 --apply")
        return

    if args.init:
        if LAYOUT_FILE.is_file():
            print(f"已存在 {LAYOUT_FILE.name}，先备份再覆盖")
            LAYOUT_FILE.rename(LAYOUT_FILE.with_suffix(".json.bak"))
        LAYOUT_FILE.write_text(json.dumps(DEFAULT_LAYOUT, ensure_ascii=False, indent=2),
                               encoding="utf-8")
        print(f"已生成 {LAYOUT_FILE.name}")
        print("把每个 match 改成 --list 里对应窗口标题的一部分，再 --apply")
        return

    if not LAYOUT_FILE.is_file():
        print(f"没有 {LAYOUT_FILE.name}，先跑 --init")
        return

    cfg = json.loads(LAYOUT_FILE.read_text(encoding="utf-8"))

    if args.save:
        for key, item in cfg.items():
            if key.startswith("_"):
                continue
            w = find_window(item["match"], wins)
            if w:
                item["rect"] = w["rect"]
                print(f"  保存 {key}: {w['title'][:30]} → {w['rect']}")
            else:
                print(f"  [!] {key} 没找到 match='{item['match']}'")
        LAYOUT_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        print("已写回", LAYOUT_FILE.name)
        return

    if args.apply:
        ok = 0
        for key, item in cfg.items():
            if key.startswith("_"):
                continue
            w = find_window(item["match"], wins)
            if not w:
                print(f"  [x] {key}: 找不到 match='{item['match']}'")
                continue
            if move(w["hwnd"], item["rect"]):
                ok += 1
                print(f"  [√] {key}: {w['title'][:28]} → {item['rect']}")
            else:
                print(f"  [x] {key}: 移动失败（可能需要管理员权限）")
        print(f"\n完成 {ok}/{sum(1 for k in cfg if not k.startswith('_'))}")
        return

    if args.watch > 0:
        import time
        print(f"布局守护中：每 {args.watch:.0f}s 检查一次，Ctrl+C 停止")
        while True:
            time.sleep(args.watch)
            wins = list_windows()
            for key, item in cfg.items():
                if key.startswith("_"):
                    continue
                w = find_window(item["match"], wins)
                if not w:
                    continue
                want = item["rect"]
                got = w["rect"]
                if any(abs(a - b) > 8 for a, b in zip(want, got)):
                    move(w["hwnd"], want)
        return

    ap.print_help()


if __name__ == "__main__":
    main()
