"""画面体检 —— 回答一个问题：**现在这一帧，ZCode 窗口完整吗？比例合适吗？**

为什么要它：
    合成器用的是 `fit_cover`（缩放到刚好盖满槽位，居中裁掉溢出）。
    这招在「窗口比例 = 槽位比例」时零裁切；一旦窗口被拖成别的比例，
    它不会报错、不会留黑边，只会**默默把两边裁掉**——
    你看到的是"画面正常"，实际只剩中间三分之一。

这个脚本把这件事算给你看：窗口比例、裁掉多少、放大多少倍、文字还剩多大。

用法：
    python tools/check_frame.py            # 看当前
    python tools/check_frame.py --json     # 机器可读
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STAGE = "http://127.0.0.1:8790"

# 抖音竖屏直播：播放器按高度填满，左右各裁 108px，实际可见中间 864px
PHONE_VISIBLE_W = 864
# 手机上小于这个等效字号基本读不了（经验值，1080 宽画布）
MIN_READABLE_PX = 26


def _get(url: str, timeout: float = 3.0) -> bytes:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.read()


def load_slots() -> tuple[int, int, dict]:
    """槽位定义以 compositor.py 为准，直接从源码里抠，避免两处漂移。"""
    import re
    src = (ROOT / "compositor.py").read_text(encoding="utf-8")
    m = re.search(r"CANVAS_W\s*,\s*CANVAS_H\s*=\s*(\d+)\s*,\s*(\d+)", src)
    if m:
        w, h = int(m.group(1)), int(m.group(2))
    else:
        w = int(re.search(r"CANVAS_W\s*=\s*(\d+)", src).group(1))
        h = int(re.search(r"CANVAS_H\s*=\s*(\d+)", src).group(1))
    block = re.search(r"SLOTS\s*=\s*\{(.*?)\n\}", src, re.S).group(1)
    slots = {}
    for m in re.finditer(r'"(\w+)"\s*:\s*\(([^)]*)\)', block):
        parts = [p.strip() for p in m.group(2).split(",")]
        nums = [int(p) for p in parts[:4] if p.lstrip("-").isdigit()]
        if len(nums) == 4:
            slots[m.group(1)] = tuple(nums)
    return w, h, slots


def cover_math(win_w: int, win_h: int, slot_w: int, slot_h: int) -> dict:
    """复刻 fit_cover 的算法，算清楚裁了多少。"""
    if win_w <= 0 or win_h <= 0:
        return {}
    k = max(slot_w / win_w, slot_h / win_h)
    sw, sh = round(win_w * k), round(win_h * k)
    cut_x = max(0, sw - slot_w)
    cut_y = max(0, sh - slot_h)
    return {
        "scale": round(k, 3),
        "scaled": [sw, sh],
        "cut_x_px": cut_x,
        "cut_y_px": cut_y,
        "cut_x_pct": round(cut_x / sw * 100, 1) if sw else 0.0,
        "cut_y_pct": round(cut_y / sh * 100, 1) if sh else 0.0,
        "visible_x_pct": round(slot_w / sw * 100, 1) if sw else 0.0,
        "visible_y_pct": round(slot_h / sh * 100, 1) if sh else 0.0,
    }


def dpi_scale() -> float:
    """系统 DPI 缩放（150% → 1.5）。

    为什么必须换算：/api/windows 给的是**逻辑像素**（480×1019），
    但合成器 WGC 抓到的是**物理像素**（720×1528）。
    直接拿逻辑像素去算缩放倍数会虚高 1.5 倍 —— 之前就报错过
    「放大 2.25×」，实际只有 1.5×。
    """
    try:
        import ctypes
        u = ctypes.windll.user32
        # 必须先声明 DPI 感知，否则 GetDpiForSystem() 恒返回 96（虚报 100%）
        u.SetProcessDPIAware()
        try:
            return max(1.0, u.GetDpiForSystem() / 96.0)
        except Exception:
            hdc = u.GetDC(0)
            dpi = ctypes.windll.gdi32.GetDeviceCaps(hdc, 88)   # LOGPIXELSX
            u.ReleaseDC(0, hdc)
            return max(1.0, dpi / 96.0)
    except Exception:
        return 1.0


def screen_size() -> tuple[int, int]:
    try:
        import ctypes
        u = ctypes.windll.user32
        u.SetProcessDPIAware()
        return u.GetSystemMetrics(0), u.GetSystemMetrics(1)
    except Exception:
        return 0, 0


def best_size(slot_w: int, slot_h: int, screen_w: int, screen_h: int) -> tuple[int, int]:
    """在屏幕放得下的前提下，取最大的 9:16 尺寸（只缩小不放大）。"""
    if screen_w <= 0 or screen_h <= 0:
        return slot_w, slot_h
    k = min(screen_w / slot_w, screen_h / slot_h, 1.0)
    # 取偶数，避免缩放时半像素
    return int(slot_w * k) // 2 * 2, int(slot_h * k) // 2 * 2


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--stage", default=STAGE)
    args = ap.parse_args()

    canvas_w, canvas_h, slots = load_slots()
    try:
        wins = json.loads(_get(f"{args.stage}/api/windows"))["windows"]
    except Exception as e:
        print(f"[!] 读不到舞台窗口信息（{type(e).__name__}）。先起 run.py 或 server.py。")
        sys.exit(1)

    sw_screen, sh_screen = screen_size()
    report = {"canvas": [canvas_w, canvas_h], "screen": [sw_screen, sh_screen],
              "slots": {}, "problems": []}

    for w in wins:
        key, rect = w.get("key"), w.get("rect")
        if key not in slots:
            continue
        sx, sy, sw, sh = slots[key]
        slot_ratio = sw / sh
        item = {"title": (w.get("title") or "")[:40], "slot": [sw, sh],
                "slot_ratio": round(slot_ratio, 4)}
        if not rect:
            # 窗口没开（或最小化）时，用 layout.json 里配置的尺寸算——
            # 至少能告诉你"按现在的配置会得到什么画面"
            try:
                cfg = json.loads((ROOT / "layout.json").read_text(encoding="utf-8"))
                rect = (cfg.get(key) or {}).get("rect")
            except Exception:
                rect = None
            if not rect:
                item["status"] = "missing"
                report["problems"].append(f"{key}: 窗口没找到，layout.json 也没配 rect")
                report["slots"][key] = item
                continue
            item["assumed"] = True
        x, y, ww, hh = rect
        d = dpi_scale()
        pw, ph = int(round(ww * d)), int(round(hh * d))     # 物理像素
        item.update({"rect": [x, y, ww, hh], "dpi": d, "physical": [pw, ph],
                     "ratio": round(ww / hh, 4),
                     "ratio_err": round(abs(ww / hh - slot_ratio) / slot_ratio * 100, 1)})
        m = cover_math(pw, ph, sw, sh)
        item.update(m)
        # 等效字号：按窗口里 14px 正文算，缩放到画布后还有多大
        item["text_px"] = round(14 * m["scale"], 1)
        item["readable"] = item["text_px"] >= MIN_READABLE_PX
        # 想在手机上读得清，窗口里的字号至少要多大
        item["need_font_px"] = round(MIN_READABLE_PX / m["scale"], 0) if m["scale"] else 0
        if item["ratio_err"] > 3:
            item["status"] = "cropped"
            if m["cut_x_pct"] >= m["cut_y_pct"]:
                axis, cut, vis = "左右", m["cut_x_pct"], m["visible_x_pct"]
            else:
                axis, cut, vis = "上下", m["cut_y_pct"], m["visible_y_pct"]
            report["problems"].append(
                f"{key}: 比例 {item['ratio']} ≠ 槽位 {round(slot_ratio, 4)}，"
                f"{axis}裁掉 {cut}%（只剩 {vis}%）")
        elif m["scale"] > 1.30:
            item["status"] = "upscaled"
            report["problems"].append(
                f"{key}: 放大 {m['scale']}× 输出，文字会糊（窗口比槽位小）")
        else:
            item["status"] = "ok"
        if not item["readable"]:
            report["problems"].append(
                f"{key}: 等效正文 {item['text_px']}px < {MIN_READABLE_PX}px，手机上读不清"
                f" → 把窗口里字号调到 ≥ {int(item['need_font_px'])}px")
        report["slots"][key] = item

    bw, bh = best_size(canvas_w, canvas_h, sw_screen, sh_screen)
    up = round(canvas_w / bw, 2) if bw else 1
    why = (f"屏幕 {sw_screen}×{sh_screen}，9:16 最大只能开到 {bw}×{bh}；"
           f"比例与槽位一致 → cover 零裁切")
    if up > 1.02:
        why += f"；屏幕高度不到 {canvas_h}，合成时会放大 {up}×（轻微，可接受）"
    report["recommend"] = {
        "code_rect": [0, 0, bw, bh],
        "upscale": up,
        "why": why,
        "cmd": "python windows_layout.py --apply",
    }

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=1))
        return

    print(f"\n画布 {canvas_w}×{canvas_h}（9:16）　屏幕 {sw_screen}×{sh_screen}\n")
    print(f"{'窗口':<8}{'实际尺寸':<14}{'比例':<9}{'槽位比例':<10}{'缩放':<8}{'横向可见':<10}状态")
    print("-" * 74)
    for k, it in report["slots"].items():
        if it.get("status") == "missing":
            print(f"{k:<8}{'—':<14}{'—':<9}{round(it['slot_ratio'], 4):<10}{'—':<8}{'—':<10}未找到")
            continue
        ww, hh = it["rect"][2], it["rect"][3]
        pw, ph = it.get("physical", [ww, hh])
        print(f"{k:<8}{f'{pw}×{ph}':<14}{it['ratio']:<9}{round(it['slot_ratio'], 4):<10}"
              f"{it['scale']:<8}{str(it['visible_x_pct']) + '%':<10}{it['status']}")
        print(f"        等效正文 {it['text_px']}px（画布）　"
              f"想读清窗口里字号需 ≥ {int(it['need_font_px'])}px")

    print()
    if report["problems"]:
        print("问题：")
        for p in report["problems"]:
            print("  ✗ " + p)
    else:
        print("✓ 画面完整、比例合适、没有放大")

    r = report["recommend"]
    print(f"\n建议窗口尺寸：{r['code_rect'][2]}×{r['code_rect'][3]}")
    print(f"  {r['why']}")
    print(f"  执行：{r['cmd']}")


if __name__ == "__main__":
    main()
