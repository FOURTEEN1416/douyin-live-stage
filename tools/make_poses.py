"""把 ImageGen 出的姿态图处理成可用差分。

踩过的坑：ImageGen 有时会把"透明"画成**棋盘格**（灰白相间的小方块），
这种图直接 flood fill 抠不掉（相邻像素差 30+，阈值一路被挡）。
所以这里先把"中性亮色"二值化成掩膜，再从四角 flood fill 掩膜，
这样棋盘格和纯白都能一次抠干净，而白围裙因为被裙边包住、跟边角不连通，
不会被误伤。
"""
from PIL import Image, ImageDraw, ImageFilter
import numpy as np
import glob, os

SRC = "avatar/sit_raw"
DST = "avatar/sit"
BASE = "avatar/sit/base.webp"

MAP = {
    "18-40-27": "smug",
    "18-40-59": "bored",
    "18-41-17": "wow",
    "18-42-01": "sad",
    "18-42-21": "away",
    "18-42-40": "sleep",
}

FILLED = 128


def cut_bg(im: Image.Image) -> Image.Image:
    rgb = im.convert("RGB")
    a = np.asarray(rgb).astype(np.int16)
    r, g, b = a[:, :, 0], a[:, :, 1], a[:, :, 2]
    mx = a.max(axis=2)
    mn = a.min(axis=2)
    # 中性亮色 = 背景候选（纯白、浅灰、棋盘格的两种灰）
    # 亮底（纯白/棋盘格）和黑底（模型有时把透明画成纯黑）都要算背景
    neutral = ((mx - mn) <= 16) & ((mn >= 185) | (mx <= 45))
    # 注意：Image.fromarray 出来的图 floodfill 写不进去（PIL 12 实测），
    # 必须用 frombytes 建一份自己拥有内存的副本
    arr = np.where(neutral, 255, 0).astype(np.uint8)
    mask = Image.frombytes("L", (arr.shape[1], arr.shape[0]), arr.tobytes())

    w, h = mask.size
    for xy in [(0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)]:
        if mask.getpixel(xy) == 255:
            ImageDraw.floodfill(mask, xy, FILLED, thresh=0)

    m = np.array(mask, dtype=np.uint8)      # 拷贝一份，asarray 出来是只读的
    # 被轮廓围住的"洞"（比如呆毛卷里面那块背景）从边角灌不进去，
    # 单独找出来：面积小、又在画面上半部分的，一并挖掉（避开白围裙）。
    from scipy import ndimage
    holes = (m == 255)
    lab, n = ndimage.label(holes)
    if n:
        border_labels = (set(lab[0, :].tolist()) | set(lab[-1, :].tolist()) |
                         set(lab[:, 0].tolist()) | set(lab[:, -1].tolist()))
        sizes = ndimage.sum(holes, lab, range(1, n + 1))
        yy = np.repeat(np.arange(h, dtype=np.float64)[:, None], w, axis=1)
        mean_y = ndimage.mean(yy, lab, range(1, n + 1))
        luma = (a * np.array([0.299, 0.587, 0.114])).sum(2)
        # 只挖"被深色线稿围住"的洞（呆毛卷起来留下的空隙）。
        # 白围裙上的空洞边界是白的，不能挖——挖了头饰会破一个口子。
        kill = []
        for i in range(1, n + 1):
            if i in border_labels:
                continue
            if not (300 <= sizes[i - 1] <= 12000):
                continue
            if mean_y[i - 1] >= h * 0.15:
                continue
            m_i = (lab == i)
            ring = ndimage.binary_dilation(m_i, iterations=3) & ~m_i
            if ring.sum() == 0:
                continue
            if luma[ring].mean() < 95:      # 边界是深色线稿 → 是真空隙
                kill.append(i)
        if kill:
            m[np.isin(lab, kill)] = FILLED
            print(f"    挖掉 {len(kill)} 个封闭背景洞")
    alpha = np.where(m == FILLED, 0, 255).astype(np.uint8)
    alpha = np.asarray(Image.fromarray(alpha, "L").filter(ImageFilter.GaussianBlur(0.7)))
    out = rgb.convert("RGBA")
    out.putalpha(Image.fromarray(alpha, "L"))
    return out


def kill_watermark(im: Image.Image) -> Image.Image:
    """右下角那行「AI生成 WORKBUDDY」——那块本来就是空的，直接清掉。"""
    w, h = im.size
    box = (int(w * 0.56), int(h * 0.940), w, h)
    px = im.load()
    for y in range(box[1], h):
        for x in range(box[0], w):
            px[x, y] = (0, 0, 0, 0)
    return im


def normalize(im: Image.Image) -> Image.Image:
    bbox = im.getbbox()
    if bbox:
        im = im.crop(bbox)
    w, h = im.size
    scale = min(720 / w, 1080 / h)
    nw, nh = max(1, int(w * scale)), max(1, int(h * scale))
    im = im.resize((nw, nh), Image.LANCZOS)
    canvas = Image.new("RGBA", (720, 1080), (0, 0, 0, 0))
    canvas.alpha_composite(im, ((720 - nw) // 2, 1080 - nh))
    return canvas


def main() -> None:
    made = []
    for f in sorted(glob.glob(os.path.join(SRC, "*.png"))):
        key = next((n for s, n in MAP.items() if s in f), None)
        if not key:
            continue
        print(f"  {key} <- {os.path.basename(f)}")
        im = Image.open(f).convert("RGB")
        im = normalize(kill_watermark(cut_bg(im)))
        # 全透明像素的 RGB 统一刷白。
        # 不刷的话 libwebp 会把透明区压成黑色，再在 lossy 编码时把黑渗到
        # 半透明的毛边上 —— 结果就是头发边缘糊出一块黑斑（踩过）。
        arr = np.array(im)
        arr[arr[:, :, 3] == 0] = (255, 255, 255, 0)
        im = Image.fromarray(arr, "RGBA")
        out = os.path.join(DST, f"{key}.webp")
        im.save(out, "WEBP", quality=92, method=6, exact=True)
        made.append(out)

    base = Image.open(BASE).convert("RGBA")
    tiles = [("base", base)] + [(os.path.basename(p)[:-5],
                                 Image.open(p).convert("RGBA")) for p in made]
    CW, CH = 240, 360
    sheet = Image.new("RGB", (CW * len(tiles), CH + 26), (16, 22, 34))
    d = ImageDraw.Draw(sheet)
    for i, (name, im) in enumerate(tiles):
        im = im.resize((CW, CH))
        sheet.paste(im, (i * CW, 26), im)
        d.text((i * CW + 6, 6), name, fill=(255, 214, 74))
    sheet.save("avatar/sit/_preview.png")
    print("preview -> avatar/sit/_preview.png")


if __name__ == "__main__":
    main()
