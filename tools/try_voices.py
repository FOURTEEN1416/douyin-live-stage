"""试音器 —— 一次生成一组「可爱向」候选，耳朵挑。

为什么要有它：
    现在 `tts.DEFAULT_STYLE` 是「松弛、不用力、语气平」——那是**耐听**调的，
    不是**可爱**调的。想变可爱，改的应该是两处：
        ① 音色（冰糖 / 茉莉 / voicedesign 自造）
        ② 风格指令（user 里那句自然语言）+ 句首标签（稚嫩/甜美/俏皮）

    光在脑子里推没用，得听。这个脚本把候选一次全跑出来。

用法：
    python tools/try_voices.py                # 生成全部候选
    python tools/try_voices.py --line "..."   # 换一句台词
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import tts  # noqa: E402

OUT = ROOT / "audio" / "试音"

# 默认试听句：取她人设里的一句，带问号/逗号/短句，能听出语气变化
LINE = "诶？你又来了。这行我改到第三遍了，键盘都快被我敲穿了。"

# 可爱向风格指令（写在 role:user，不会被念出来）
S_SWEET = ("十八九岁的女生，声音清亮、带一点点奶气，语速偏快，咬字轻巧，"
           "句尾轻轻往上翘。不要播音腔，不要压低嗓子，不要拖长音。")
S_CHEEK = ("一个爱耍小聪明的年轻女生在跟人抬杠，语速快，尾音上挑，"
           "带点不服气的小得意。不要一本正经，不要念稿。")
S_SOFT = ("刚被打断工作的年轻女生，有点小委屈又有点撒娇，声音软下来，"
          "尾音轻轻往下拖一点。不要用力，不要喊。")

# voicedesign 的音色描述（1–4 句，别堆维度）
VD_DESC = "十八九岁的女生，声音清亮偏甜，带一点奶气，语速偏快，像在跟熟人耍赖"

CASES = [
    # (文件名, 音色, 模型, 风格, 文本前缀标签)
    ("00_基线_冰糖_现用风格", "冰糖", None, None, ""),
    ("01_冰糖_甜美系", "冰糖", None, S_SWEET, ""),
    ("02_冰糖_俏皮抬杠", "冰糖", None, S_CHEEK, ""),
    ("03_冰糖_撒娇软", "冰糖", None, S_SOFT, ""),
    ("04_茉莉_甜美系", "茉莉", None, S_SWEET, ""),
    ("05_冰糖_标签稚嫩甜美", "冰糖", None, S_SWEET, "(稚嫩 甜美)"),
    ("06_voicedesign_自造甜嗓", "", "mimo-v2.5-tts-voicedesign", VD_DESC, ""),
]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--line", default=LINE)
    args = ap.parse_args()

    if not tts.enabled():
        print("[!] 没有 MIMO_API_KEY，先配 .env")
        return

    OUT.mkdir(parents=True, exist_ok=True)
    print(f"试听句：{args.line}\n")

    rows = []
    for name, voice, model, style, tag in CASES:
        text = (tag + args.line) if tag else args.line
        r = tts.synthesize(text, voice=voice or None, style=style,
                           model=model or None)
        if not r.get("ok"):
            print(f"  ✗ {name}：{r.get('error')}")
            rows.append((name, "—", "—", "失败"))
            continue
        src = ROOT / r["url"].lstrip("/")
        dst = OUT / f"{name}.mp3"
        try:
            shutil.copyfile(src, dst)
        except Exception as e:
            print(f"  ! 复制失败 {type(e).__name__}")
        secs = r.get("seconds") or 0
        cps = round(len(args.line) / secs, 2) if secs else 0
        rows.append((name, f"{secs:.2f}s", f"{cps} 字/秒", "缓存" if r.get("cached") else "新合成"))
        print(f"  ✓ {name}  {secs:.2f}s  {cps} 字/秒")

    print(f"\n输出目录：{OUT}")
    print(f"{'候选':<26}{'时长':<8}{'语速':<10}来源")
    print("-" * 60)
    for a, b, c, d in rows:
        print(f"{a:<26}{b:<8}{c:<10}{d}")
    print("\n听完告诉我编号，我把选中的写进 tts.DEFAULT_STYLE / .env。")


if __name__ == "__main__":
    main()
