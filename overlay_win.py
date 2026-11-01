"""透明悬浮舞台：把 stage.html 以无边框、置顶、可点击穿透的窗口盖在桌面上

用途：OBS 采集在这台机器（Y7000P 混合显卡）上黑屏，改走「真实桌面 + 悬浮层」
架构——悬浮窗直接叠加在 ZCode/网站/微信之上，任何屏幕采集工具（抖音直播伴侣
的显示器采集，或 ffmpeg 直推）都能把窗口 + 舞台一起采进去。

    python overlay_win.py                默认舞台地址
    python overlay_win.py --url http://127.0.0.1:8790/stage.html
"""

from __future__ import annotations

import argparse
import os
import ctypes
import threading
import time

import webview

from ctypes import wintypes as wt

user32 = ctypes.windll.user32
# 64 位下必须声明参数类型，否则 HWND_TOPMOST(-1) 会被截断导致 SetWindowPos 失败
user32.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int,
                                 ctypes.c_int, ctypes.c_int, wt.UINT]
user32.SetWindowPos.restype = wt.BOOL

GWLP_EXSTYLE = -20
WS_EX_TRANSPARENT = 0x20
WS_EX_LAYERED = 0x80000
WS_EX_TOOLWINDOW = 0x80
WS_EX_TOPMOST = 0x8
WS_EX_APPWINDOW = 0x40000
WS_POPUP = 0x80000000
HWND_BOTTOM = 1
HWND_TOPMOST = -1
SWP_NOMOVE = 0x0002
SWP_NOSIZE = 0x0001
SWP_NOACTIVATE = 0x0010
SWP_NOZORDER = 0x0004

TITLE = "大肥鱼舞台"


def _get_ex(hwnd: int) -> int:
    return user32.GetWindowLongW(hwnd, GWLP_EXSTYLE)


def _set_ex(hwnd: int, val: int) -> int:
    return user32.SetWindowLongW(hwnd, GWLP_EXSTYLE, val)


def kill_stale(title: str) -> None:
    """关掉同名的旧窗口（多次重启会堆好几个，叠在一起会很乱）"""
    victims = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
    def cb(h, _):
        n = user32.GetWindowTextLengthW(h)
        if n:
            b = ctypes.create_unicode_buffer(256)
            user32.GetWindowTextW(h, b, 256)
            if b.value.strip() == title:
                victims.append(int(h))
        return True

    user32.EnumWindows(cb, 0)
    for h in victims:
        user32.PostMessageW(h, 0x0010, 0, 0)   # WM_CLOSE
    if victims:
        time.sleep(1.0)
        print(f"[√] 关掉了 {len(victims)} 个旧的舞台窗口")


def setup_window(title: str, poll: float = 1.0, on_top: bool = False) -> None:
    """找到舞台窗口 → 鼠标穿透 + 铺满屏幕（可选置顶）

    默认**不置顶**：置顶会一直压在别的窗口上面，视觉上很挡。
    但要注意：不置顶时如果被全屏窗口（比如最大化的工作区）盖住，直播伴侣
    采这个窗口就采不到舞台了。需要直播时始终可见，就加 --on-top。
    """
    for _ in range(120):
        hwnd = user32.FindWindowW(None, title)
        if hwnd:
            ex = _get_ex(hwnd)
            flags = ex | WS_EX_LAYERED | WS_EX_TRANSPARENT
            if on_top:
                flags |= WS_EX_TOPMOST
            else:
                flags &= ~WS_EX_TOPMOST
            _set_ex(hwnd, flags & ~WS_EX_TOOLWINDOW)
            # 竖屏 9:16：高度铺满屏幕，宽度按比例算
            h = user32.GetSystemMetrics(1)
            w = int(h * 9 / 16)
            insert = wt.HWND(HWND_TOPMOST) if on_top else wt.HWND(0)
            user32.SetWindowPos(hwnd, insert, 0, 0, w, h, SWP_NOACTIVATE)
            tag = "置顶" if on_top else "不置顶"
            print(f"[√] 舞台窗口已铺满 {w}×{h}（16:9），鼠标穿透 + {tag} hwnd={hwnd}")
            return
        time.sleep(poll)
    print("[!] 没找到舞台窗口")


def _fix_zoom(win, info_json: str, done: list) -> None:
    """补 DPI 缩放。

    踩过的坑：窗口是 900×1600 物理像素，但 WebView2 把页面按 1 CSS px = 1 物理 px
    画在左上角 —— 页面自己以为只有 600×1067，于是整个舞台只占窗口的 2/3，
    右下角留一大片白。表现出来就是"字特别小、排版像坏的"。
    这里量一下实际窗口宽度，差多少补多少。
    """
    if done:
        return
    try:
        import json as _json
        d = _json.loads(info_json)
        iw = int(d.get("w") or 0)
        if iw <= 0:
            return
        hwnd = user32.FindWindowW(None, TITLE)
        if not hwnd:
            return
        user32.SetProcessDPIAware()
        r = wt.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(r))
        pw = r.right - r.left
        ratio = round(pw / iw, 3)
        print(f"[i] 窗口 {pw}px / 页面 {iw}px = ×{ratio}"
              f"{'（页面没铺满，需要补缩放）' if ratio > 1.04 else '（正常）'}")
        # 默认只报数不动手：WebView2 有的版本自己会按 DPI 缩放，
        # 盲目再补一次会放大成两倍、直接糊出画面。
        # 确认页面确实只占了窗口左上角时，再开 OVERLAY_ZOOM=1。
        if ratio > 1.04 and os.environ.get("OVERLAY_ZOOM") == "1":
            win.evaluate_js(f"document.body.style.zoom='{ratio}'")
            print(f"[√] 已补缩放 ×{ratio}")
        done.append(True)
    except Exception as e:
        print(f"[!] 补缩放失败：{type(e).__name__}: {e}")
        done.append(True)


def health_check(win, every: float = 5.0) -> None:
    """定期检查页面是否真的加载出来了（WebView2 偶发白屏/加载失败）"""
    import traceback
    time.sleep(6)
    _zoomed: list = []
    while True:
        try:
            info = win.evaluate_js(
                "JSON.stringify({t:document.title,"
                "avatar:!!document.querySelector('#avatar'),"
                "bgW:(document.getElementById('bg')||{}).naturalWidth||0,"
                "bgDisp:getComputedStyle(document.getElementById('bg')).display,"
                "w:innerWidth,h:innerHeight})")
            if not info:
                print("[!] 页面还没加载出来（evaluate_js 返回空）")
            else:
                print(f"[√] 舞台页面正常 {info}")
                _fix_zoom(win, info, _zoomed)
        except Exception as e:
            print(f"[!] 舞台页面异常：{type(e).__name__}")
            try:
                traceback.print_exc()
            except Exception:
                pass
        time.sleep(every)


# ---------------------------------------------------------------- 出画自愈
_HEAL_BUDGET = 6          # 整个进程最多重载几次，防止页面真坏时死循环


def _probe_self(hwnd: int):
    """抓自己窗口一帧，返回 (平均亮度, 近黑占比)；抓不到返回 None。

    为什么要抓像素：这个窗口是**分层透明**的。WebView2 偶发「DOM 全正常、
    窗口不出画」—— evaluate_js 里 #bg 的 naturalWidth 明明是 1080，
    可窗口本身是空的/透明的，直播伴侣按「窗口捕获」采过去就是**整屏黑**。
    DOM 正常 ≠ 出画，所以体检必须看像素。
    """
    try:
        import sys as _sys
        from pathlib import Path as _Path
        _sys.path.insert(0, str(_Path(__file__).resolve().parent / "tools"))
        from check_live import grab_stage_frame
        return grab_stage_frame(hwnd)
    except Exception:
        return None


def self_heal(win, every: float = 120.0) -> None:
    """首帧没画出来（全黑 / 全白）就自动重载页面。

    2026-10-31 09:46 那次重启就是踩的这个坑：页面 JS 一切正常、日志刷着
    「舞台页面正常」，可悬浮窗不出画，直播伴侣那边整屏黑，观众在弹幕里喊
    「咋黑屏了」。重启一次就好，但直播中没人愿意去重启。

    平时每 2 分钟探一次（只在状态变化时打日志，不刷屏），坏了才重载。
    """
    global _HEAL_BUDGET
    time.sleep(10)
    last_ok: bool | None = None
    while True:
        hwnd = user32.FindWindowW(None, TITLE)
        if not hwnd:
            time.sleep(10)
            continue
        st = _probe_self(hwnd)
        if st is None:
            print("[!] 出画自愈：抓不到帧，跳过本轮")
            time.sleep(every)
            continue
        mean, blk = st
        healthy = 12 <= mean <= 250 and blk <= 0.97
        if healthy:
            if last_ok is not True:
                print(f"[√] 悬浮窗出画正常（亮度 {mean:.0f}，黑占比 {blk * 100:.1f}%）")
            last_ok = True
            time.sleep(every)
            continue

        last_ok = False
        kind = "全黑" if (mean < 12 or blk > 0.97) else "全白"
        if _HEAL_BUDGET <= 0:
            print(f"[x] 悬浮窗{kind}（亮度 {mean:.0f}），重载次数用完了，请手动重启悬浮层")
            return
        _HEAL_BUDGET -= 1
        print(f"[!] 悬浮窗{kind}（亮度 {mean:.0f}）→ 自动重载页面"
              f"（还剩 {_HEAL_BUDGET} 次）")
        try:
            win.evaluate_js("location.reload()")
        except Exception as e:
            print(f"[!] 重载失败：{type(e).__name__}: {e}")
            return
        time.sleep(8)


def wait_for_server(url: str, timeout: float = 30.0) -> bool:
    """等舞台服务真的在监听再开窗，否则 WebView2 会打开一个连不上的页面 → 永远白屏"""
    import urllib.error
    import urllib.request
    from urllib.parse import urlparse

    u = urlparse(url)
    base = f"{u.scheme}://{u.hostname}:{u.port or 80}/"
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            with urllib.request.urlopen(base, timeout=3) as r:
                if r.status in (200, 404):      # 连上了就是好的
                    print(f"[√] 舞台服务就绪 {base}")
                    return True
        except urllib.error.HTTPError:
            print(f"[√] 舞台服务就绪 {base}")
            return True
        except Exception:
            time.sleep(1)
    print(f"[!] 等了 {timeout:.0f}s 舞台服务还没起来，先开窗（可能白屏）")
    return False


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8790/stage.html")
    ap.add_argument("--width", type=int, default=0)
    ap.add_argument("--height", type=int, default=0)
    ap.add_argument("--on-top", action="store_true",
                    help="置顶（默认不置顶；直播时需要始终可见才加）")
    args = ap.parse_args()

    wait_for_server(args.url)
    kill_stale(TITLE)

    h = args.height or user32.GetSystemMetrics(1)
    w = args.width or int(h * 9 / 16)
    print(f"舞台窗口 {w}×{h} ← {args.url}")

    threading.Thread(target=setup_window, args=(TITLE, 1.0, args.on_top),
                     daemon=True).start()

    win = webview.create_window(
        TITLE, args.url,
        width=w, height=h,
        x=0, y=0,
        frameless=True,
        on_top=args.on_top,
        transparent=True,
        easy_drag=False,
    )

    def on_loaded():
        print("[√] 页面已加载")

    try:
        win.events.loaded += on_loaded
    except Exception:
        pass

    threading.Thread(target=health_check, args=(win,), daemon=True).start()
    # 出画自愈：DOM 正常但窗口没画出来（透明窗 → 直播伴侣采到黑屏）就自动重载
    threading.Thread(target=self_heal, args=(win,), daemon=True).start()
    webview.start()


if __name__ == "__main__":
    main()
