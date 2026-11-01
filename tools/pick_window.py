"""从一堆同名窗口里，钉死要播的那一个。

场景：微信开了两个（主窗 + 聊天窗，或者多开两个号），标题都带「微信」，
合成器只能靠"面积最大 / 离配置位置最近"去猜 —— 猜错了就播错窗口。

这个工具把猜测变成确定：列出所有候选，你选一个，它把识别条件写进 layout.json。

    python tools/pick_window.py --key wechat            # 看候选
    python tools/pick_window.py --key wechat --choose 2 # 选第 2 个，写进配置

写进去的条件是 `pin_pid`（进程）+ `pin_hwnd`（窗口句柄）。
hwnd 重启就变，所以两者都写：pid 活着的窗口里再按 hwnd 精确命中；
窗口重建后 hwnd 对不上，就退回 pid 那一层。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import windows_layout as wl  # noqa: E402

LAYOUT = ROOT / "layout.json"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--key", required=True, help="layout.json 里的槽位名，如 wechat / code")
    ap.add_argument("--choose", type=int, default=0, help="选第几个（从 1 开始），不填只看")
    ap.add_argument("--pick", default="", help="多命中时的策略：nearest/largest/smallest")
    args = ap.parse_args()

    cfg = json.loads(LAYOUT.read_text(encoding="utf-8"))
    meta = cfg.get(args.key) or {}
    match = meta.get("match") or ""

    wins = wl.list_windows()
    cands = wl.candidates(match, wins, meta)

    print(f"槽位 {args.key}　match={match!r}　当前配置："
          f"pin_pid={meta.get('pin_pid')} pin_hwnd={meta.get('pin_hwnd')} "
          f"exclude={meta.get('exclude')} pick={meta.get('pick')}")
    print()
    if not cands:
        print("没有匹配的窗口。先把目标窗口打开，或者把 match 改宽一点。")
        return

    print(f"{'#':<4}{'标题':<28}{'pid':<8}{'hwnd':<12}{'位置/尺寸':<22}选中")
    print("-" * 86)
    chosen = wl.find_window(match, wins, dict(meta, target=meta.get("rect")))
    for i, w in enumerate(cands, 1):
        x, y, ww, hh = w["rect"]
        mark = "←" if chosen and w["hwnd"] == chosen["hwnd"] else ""
        print(f"{i:<4}{w['title'][:26]:<28}{w.get('pid', 0):<8}{w['hwnd']:<12}"
              f"{f'{x},{y} {ww}×{hh}':<22}{mark}")

    if not args.choose and not args.pick:
        print("\n要钉死哪一个：加 --choose N（例如 --choose 2）")
        return

    if args.pick:
        meta["pick"] = args.pick
        cfg[args.key] = meta
        LAYOUT.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n[√] 已设置 pick={args.pick}")
        return

    if not (1 <= args.choose <= len(cands)):
        print(f"\n[!] --choose 超出范围（1..{len(cands)}）")
        return

    w = cands[args.choose - 1]
    meta["pin_pid"] = int(w.get("pid") or 0)
    meta["pin_hwnd"] = int(w["hwnd"])
    meta.pop("exclude", None)          # 已经精确到窗口了，黑名单就不用了
    cfg[args.key] = meta
    LAYOUT.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n[√] 已钉死：{w['title']}  pid={meta['pin_pid']} hwnd={meta['pin_hwnd']}")
    print("    重启合成器生效：python run.py --mode auto")
    print("    注意：窗口关掉重开 hwnd 会变，但 pid 一般还在；"
          "整个微信重开就再跑一次这个工具。")


if __name__ == "__main__":
    main()
