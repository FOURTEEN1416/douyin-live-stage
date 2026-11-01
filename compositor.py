"""直播合成器 —— 自己把三个真实窗口 + 大肥鱼舞台合成一帧完整画面

为什么需要它：
  直播伴侣的「窗口捕获」不支持透明背景（那是游戏捕获的选项），
  我们的悬浮层是透明的，被它采集就变成黑底。
  但 Windows Graphics Capture（WGC）能完整保留 alpha —— 所以我们自己合成：
    三个内容窗口（WGC 抓，窗口被遮挡也能抓）
      → 缩放贴到 1920×1080 画布
      → 交给页面当背景
      → 页面上面再叠 HTML 的大肥鱼/字幕/弹幕
      → 直播伴侣只需采集这一个不透明窗口 → 就是完整直播画面

MJPEG 输出：http://127.0.0.1:8791/bg.mjpeg
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import cv2
import numpy as np

from windows_capture import WindowsCapture

import env_loader  # noqa: F401

CANVAS_W, CANVAS_H = 1080, 1920      # 竖屏 9:16（抖音手机端主流比例）
PORT = 8791
KEYS = ("code", "wechat")

# 固定分镜：竖屏三段式，从上到下 代码 → 网站 → 微信
# 顶部 240px 留给标题和打工面板，底部 120px 留给字幕，都不放窗口内容。
# (key, 槽位 x, y, w, h, 名字)
SLOTS = {
    "code":   (0, 0, 1080, 1920, "代码窗口"),
    "wechat": (556, 240, 412, 548, "微信窗口"),
}
# 播放器裁切（手机 20:9 播 9:16 视频，左右各裁约 108px）：
#   代码铺满整屏 → 边缘被裁掉的是代码边角，不影响阅读，画面还饱满
#   关键信息全部锁在中间 108~972 这条安全带里：
#     面板 238~538 / 微信 556~968 / 立绘 252~772 / 字幕 236~836
#   这样无论播放器怎么裁，观众看到的都是完整的信息
# 关键：抖音播放器会裁掉左右各 108px
#   视频是 9:16 (1080×1920)，手机是 20:9 (约 864×1920)，
#   播放器按高度填满 → 左右各裁 108，只剩中间 864 宽可见。
#   所以代码区只铺 108~972，被裁掉的正好是看不见的部分；
#   面板(238~538)、微信(556~968)、立绘(252~772) 本来就都在这个范围内。
# 分层思路（2026-10-30 改）：
#   代码 = 氛围背景，铺满整屏。它被抖音 UI 挡一部分没关系——观众不是来读代码的，
#          画面饱满比"看清每一行"重要。
#   必须看清的东西（打工面板 / 微信 / 立绘 / 字幕）才收进安全区：
#          224~968 × 258~1661（避开顶部头像栏、底部输入框、左侧观众列表、右侧礼物栏）
# 抖音竖屏直播 UI 遮挡（按 1080×1920 实测换算）：
#   顶部 0~258      头像 / 点赞数 / 在线人数 / 小时榜
#   底部 1661~1920  输入框 / 点赞 / 礼物按钮
#   左侧 0~224      在线观众列表（一列头像+昵称）
#   右侧 968~1080   礼物栏 / 连击特效
# → 真正能用的画面只有 224~968 × 258~1661，内容全收进这块。
# 抖音竖屏直播的 UI 遮挡（按 1080×1920 量）：
#   顶部 0~258    头像/点赞/在线人数/小时榜
#   底部 1661~1920 输入框、点赞、礼物按钮
#   右侧 968~1080 礼物栏、连击特效
# 内容全部收进 0~968 × 258~1661 这块安全区，才不会被平台浮层压住。
# 代码窗口铺满 → 空间全用上；微信做成右上角悬浮小窗（412×636，和手机端
# 微信聊天窗比例一致，聊天内容能完整看到）。绘制顺序按 KEYS，code 先画，
# wechat 后画覆盖上去。

_jpeg_lock = threading.Lock()
_jpeg: bytes | None = None
_jpeg_ts = 0.0


def log(msg: str) -> None:
    print(f"[合成] {msg}", flush=True)


# ---------------------------------------------------------------- 窗口捕获
class WindowGrabber:
    """按窗口名匹配 → hwnd → WGC 抓帧（按名字匹配容易挑错窗口，所以用 hwnd）"""

    def __init__(self, key: str, match, target: list[int] | None = None,
                 crop: list[int] | None = None, meta: dict | None = None):
        self.key = key
        self.match = match
        self.target = target            # layout.json 里配的期望 rect，用来在重名窗口里挑对的那个
        self.crop = crop                # 相对窗口逻辑坐标的裁切区域 [x,y,w,h]，None=整窗
        self.meta = meta or {}          # layout.json 里的整块配置（exclude/pin_pid/pick…）
        self.hwnd: int | None = None
        self.frame: np.ndarray | None = None
        self.ts = 0.0
        self.rect: list[int] | None = None      # 逻辑像素 [x,y,w,h]
        self._lock = threading.Lock()
        self._control = None
        self._cap = None
        self._stop = False

    def apply_crop(self, arr: np.ndarray) -> np.ndarray:
        """按 crop 配置裁掉窗口里不需要的部分（比如 ZCode 的侧边栏）。

        为什么要裁：竖屏槽位比例 0.5625、ZCode 窗口比例 0.837，直接 cover 会把
        左右各裁掉 33% —— 内容既看不清又不完整。先按比例裁出「代码编辑区」，
        再 cover 到槽位，画面就干净了。
        """
        if not self.crop or not self.rect or arr is None:
            return arr
        fh, fw = arr.shape[:2]
        lw, lh = self.rect[2], self.rect[3]
        if lw <= 0 or lh <= 0 or fw <= 0:
            return arr
        kx, ky = fw / lw, fh / lh
        x, y, w, h = self.crop
        x0 = max(0, int(x * kx)); y0 = max(0, int(y * ky))
        x1 = min(fw, int((x + w) * kx)); y1 = min(fh, int((y + h) * ky))
        if x1 - x0 < 20 or y1 - y0 < 20:
            return arr
        return arr[y0:y1, x0:x1]

    @property
    def logical_w(self) -> int:
        return self.rect[2] if self.rect else 0

    @property
    def logical_h(self) -> int:
        return self.rect[3] if self.rect else 0

    def pos(self) -> tuple[int, int]:
        if not self.rect:
            return 0, 0
        return int(self.rect[0] * SCALE), int(self.rect[1] * SCALE)

    def _find_hwnd(self) -> int | None:
        """按关键字找窗口：先要标题完全一致的，再退化到包含关键字的（面积最大的那个）"""
        try:
            import windows_layout
            wins = windows_layout.list_windows()
            # 统一走 windows_layout.find_window：
            # 支持 match 列表 / exclude / pin_pid / pin_hwnd / pick
            meta = dict(self.meta)
            meta.setdefault("target", self.target)
            w = windows_layout.find_window(self.match, wins, meta)
            if not w:
                return None
            x, y, ww, hh = w["rect"]
            if x < -2000 or y < -2000 or ww < 100 or hh < 100:
                return None          # 最小化了
            self.rect = [int(x), int(y), int(ww), int(hh)]
            return int(w["hwnd"])
        except Exception:
            return None

    def on_frame_arrived(self, frame, _control):
        try:
            arr = np.asarray(frame.frame_buffer)
            with self._lock:
                self.frame = arr
                self.ts = time.time()
        except Exception:
            pass

    def start(self) -> None:
        def run():
            while not self._stop:
                hwnd = self._find_hwnd()
                if hwnd and hwnd != self.hwnd:
                    self.hwnd = hwnd
                    log(f"{self.key}: 绑定窗口 hwnd={hwnd}")
                    try:
                        cap = WindowsCapture(window_hwnd=hwnd, cursor_capture=True)
                        cap.event(self.on_frame_arrived)
                        cap.event(self.on_closed)
                        self._cap = cap
                        self._control = cap.start_free_threaded()
                    except Exception as e:
                        log(f"{self.key}: 捕获启动失败 {type(e).__name__}: {e}")
                        self._control = None
                elif not hwnd and self.hwnd:
                    log(f"{self.key}: 窗口没了/最小化")
                    self.hwnd = None
                    self._control = None
                    self._cap = None
                time.sleep(1.5)

        threading.Thread(target=run, daemon=True).start()

    def on_closed(self):
        self.hwnd = None
        self._control = None

    def latest(self):
        with self._lock:
            return self.frame


# ---------------------------------------------------------------- 合成
def fit_cover(frame: np.ndarray, sw: int, sh: int) -> np.ndarray | None:
    """把一帧缩放到刚好盖满 (sw, sh)，居中裁掉溢出部分。

    不要用 contain（min）——桌面是 16:10、画布是 16:9，按 contain 缩完
    宽度不够，两侧就留一条深色空隙，直播里看就是黑边。
    """
    h, w = frame.shape[:2]
    if h <= 0 or w <= 0:
        return None
    k = max(sw / w, sh / h)
    tw = max(sw, int(round(w * k)))
    th = max(sh, int(round(h * k)))
    small = cv2.resize(frame, (tw, th), interpolation=cv2.INTER_AREA)
    cx = max(0, (tw - sw) // 2)
    cy = max(0, (th - sh) // 2)
    patch = small[cy:cy + sh, cx:cx + sw]
    return patch if patch.size else None


def load_layout() -> dict:
    from pathlib import Path
    p = Path(__file__).resolve().parent / "layout.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


_ratio_warn: dict[str, float] = {}


class Compositor:
    def __init__(self):
        cfg = load_layout()
        self.grabbers = []
        for key in KEYS:
            meta = cfg.get(key) or {}
            match = meta.get("match", "")
            if match:
                self.grabbers.append(WindowGrabber(key, match, meta.get("rect"),
                                                   meta.get("crop"), meta))

    def run(self) -> None:
        global _jpeg, _jpeg_ts
        for g in self.grabbers:
            g.start()
        # 底色不用纯黑：安全区留白是给抖音 UI 让位的，纯黑看着像"黑边"，
        # 做成深蓝竖向渐变更像刻意设计的边框
        canvas = np.zeros((CANVAS_H, CANVAS_W, 3), dtype=np.uint8)
        top = np.array([22, 27, 40], dtype=np.float32)
        bot = np.array([13, 16, 24], dtype=np.float32)
        ramp = np.linspace(0, 1, CANVAS_H, dtype=np.float32)[:, None, None]
        canvas[:] = (top[None, None, :] * (1 - ramp) + bot[None, None, :] * ramp
                     ).astype(np.uint8)
        errs = 0
        while True:
            drew = False
            for g in self.grabbers:
                sx0, sy0, sw, sh = SLOTS[g.key][:4]
                arr = g.latest()
                if arr is None or arr.size == 0:
                    continue
                arr = g.apply_crop(arr)
                # 比例守护：fit_cover 在窗口比例 ≠ 槽位比例时会**静默裁掉两侧**，
                # 画面看起来"正常"，其实可能只剩中间三分之一。这里把它喊出来。
                _h, _w = arr.shape[:2]
                if _w and _h:
                    _r, _slot = _w / _h, sw / sh
                    if abs(_r / _slot - 1) > 0.03:
                        _now = time.time()
                        if _now - _ratio_warn.get(g.key, 0) > 30:
                            _ratio_warn[g.key] = _now
                            _k = max(sw / _w, sh / _h)
                            _vx = sw / (_w * _k) * 100
                            _vy = sh / (_h * _k) * 100
                            log(f"{g.key}: 窗口 {_w}×{_h} 比例 {_r:.3f} ≠ 槽位 {_slot:.3f}，"
                                f"只剩 横 {_vx:.0f}% / 纵 {_vy:.0f}%（被裁掉了）"
                                f" → 跑 python windows_layout.py --apply")
                try:
                    patch = fit_cover(arr[:, :, :3], sw, sh)
                    if patch is None:
                        continue
                    ph, pw = patch.shape[:2]
                    canvas[sy0:sy0 + ph, sx0:sx0 + pw] = patch
                    drew = True
                except Exception as e:
                    if errs < 5:
                        log(f"{g.key}: 合成失败 {type(e).__name__}: {e}")
                        errs += 1
                    continue
            try:
                ok, buf = cv2.imencode(".jpg", canvas,
                                       [int(cv2.IMWRITE_JPEG_QUALITY),
                                        88 if drew else 70])
                if ok:
                    with _jpeg_lock:
                        _jpeg = buf.tobytes()
                        _jpeg_ts = time.time()
            except Exception as e:
                log(f"编码失败 {type(e).__name__}: {e}")
            time.sleep(0.05)


# ---------------------------------------------------------------- MJPEG 输出
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.startswith("/bg.jpg"):
            with _jpeg_lock:
                data = _jpeg
            if not data:
                self.send_error(503, "no frame yet")
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
            return
        if self.path.startswith("/bg.mjpeg"):
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            last = b""
            try:
                while True:
                    with _jpeg_lock:
                        cur = _jpeg
                    if cur and cur != last:
                        last = cur
                        self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\n")
                        self.wfile.write(f"Content-Length: {len(cur)}\r\n\r\n".encode())
                        self.wfile.write(cur)
                        self.wfile.write(b"\r\n")
                        self.wfile.flush()
                    time.sleep(0.04)
            except Exception:
                pass
            return
        self.send_error(404)


def main() -> None:
    comp = Compositor()
    # 端口被占用时直接报错，不要静默抢端口（两个合成器会互相串画面）
    ThreadingHTTPServer.allow_reuse_address = False
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError as e:
        raise SystemExit(f"[合成] 端口 {PORT} 被占用（可能有旧的合成器进程）：{e}")
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log(f"MJPEG 输出 http://127.0.0.1:{PORT}/bg.mjpeg")
    comp.run()


if __name__ == "__main__":
    main()
