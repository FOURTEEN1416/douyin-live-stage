"""把 TTS 语音 + 背景音乐混音后直接播到指定输出设备（默认 VB-Cable）

为什么不让浏览器播：
    舞台页面跑在 WebView2 里，音频走**系统默认设备**。直播伴侣要采声音
    只能开「系统声音」，于是微信提示音、别人的视频声全混进来。而 WebView2
    在这台机器上有 40+ 个进程，按进程做应用级路由会把别的应用一起静音。

    所以改成服务端精确投放：只有大肥鱼 + BGM 进虚拟声卡，直播伴侣麦克风
    选 CABLE Output，观众就只听到她。

混音：
    一个常驻 OutputStream，callback 里把 BGM（循环）和 TTS（一次性）相加。
    不能各开一个流 —— 同一设备多流在 Windows 共享模式下会打架，而且
    TTS 一播就把 BGM 顶掉了。

环境变量：
    AUDIO_OUT_DEVICE   输出设备关键字，默认 "CABLE"（自动挑能打开的那个）
    AUDIO_MONITOR      1=同时播到扬声器（默认 1）
    BGM_VOLUME         背景音乐音量 0~1，默认 0.28
"""

from __future__ import annotations

import os
import subprocess
import threading
from pathlib import Path

import numpy as np
import sounddevice as sd

SR = 48000
TTS_PEAK = 0.9          # 语音归一化目标峰值
BGM_DIR = Path(__file__).resolve().parent / "bgm"
_DECODE_CACHE: dict[str, tuple[np.ndarray, int]] = {}
_LOCK = threading.Lock()


def _hostapi_name(dev: dict) -> str:
    try:
        return sd.query_hostapis(dev["hostapi"])["name"]
    except Exception:
        return ""


def open_output(keyword: str) -> tuple[int, str] | None:
    """按名字列候选，**真开一次**，第一个能开的就用。

    光看名字不可靠：这台机器上 VB-Cable 的 CABLE Input / CABLE In 16ch
    可用性会反转（一会儿这个 Invalid device，一会儿那个），所以逐个试开。
    """
    cands: list[tuple[int, int, int, str]] = []
    for i, d in enumerate(sd.query_devices()):
        if d["max_output_channels"] < 2:
            continue
        if keyword.lower() not in d["name"].lower():
            continue
        api = _hostapi_name(d)
        # DirectSound / MME 更宽容：WASAPI 共享模式在别的程序也占着设备时会报
        # AUDCLNT_E_DEVICE_IN_USE，直播场景下这个坑踩过好几次
        prio = 0 if "Direct" in api else (1 if "MME" in api else 2)
        # ★ 关键字 "CABLE" 会同时命中 CABLE Input（放音端）和 CABLE Output（录音端）。
        #   Windows 把 CABLE Output 也列成可放音设备，往里写等于写进黑洞 ——
        #   表现就是"整条链路没声音"，而且设备顺序一变就时好时坏。
        low = d["name"].lower()
        if "input" in low or " in " in low:
            side = 0          # 放音端，正解
        elif "output" in low:
            side = 2          # 录音端，只有当没有别的可选时才用
        else:
            side = 1
        cands.append((side, prio, i, d["name"]))
    cands.sort()
    for _side, _prio, i, name in cands:
        try:
            s = sd.OutputStream(samplerate=SR, channels=2, device=i, dtype="float32")
            s.start()
            s.abort()
            s.close()
            return i, name
        except Exception:
            continue
    return None


def find_device(keyword: str, want_output: bool = True) -> int | None:
    fallback = None
    for i, d in enumerate(sd.query_devices()):
        if want_output and d["max_output_channels"] < 2:
            continue
        if not want_output and d["max_input_channels"] < 1:
            continue
        if keyword.lower() not in d["name"].lower():
            continue
        if "WASAPI" in _hostapi_name(d):
            return i
        if fallback is None:
            fallback = i
    return fallback


def decode(path: str | Path) -> tuple[np.ndarray, int]:
    key = str(path)
    with _LOCK:
        if key in _DECODE_CACHE:
            return _DECODE_CACHE[key]
    p = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", key,
         "-f", "s16le", "-ac", "2", "-ar", str(SR), "-"],
        capture_output=True, timeout=120,
    )
    if p.returncode != 0 or not p.stdout:
        raise RuntimeError(f"ffmpeg 解码失败: {p.stderr.decode('utf-8', 'replace')[:200]}")
    data = np.frombuffer(p.stdout, dtype=np.int16).reshape(-1, 2).astype(np.float32) / 32768.0
    with _LOCK:
        if len(_DECODE_CACHE) > 30:
            _DECODE_CACHE.clear()
        _DECODE_CACHE[key] = (data, SR)
    return data, SR


class _Mixer:
    """一条输出流：BGM 循环 + TTS 插播，callback 里实时相加。"""

    def __init__(self, device: int, name: str, bgm_vol: float = 0.28):
        self.device = device
        self.name = name
        self.bgm_vol = bgm_vol
        self._lock = threading.Lock()
        self._bgm: np.ndarray | None = None
        self._bgm_pos = 0
        self._tts: np.ndarray | None = None
        self._tts_pos = 0
        self._stream = sd.OutputStream(samplerate=SR, channels=2, device=device,
                                       dtype="float32", blocksize=2048,
                                       callback=self._cb)
        self._stream.start()

    def _cb(self, outdata, frames, _t, _status):
        out = np.zeros((frames, 2), dtype=np.float32)
        with self._lock:
            # 说话时把 BGM 压到 35%：人声和音乐一样响的时候，观众只听得见音乐。
            speaking = self._tts is not None and self._tts_pos < len(self._tts)
            bgm_gain = self.bgm_vol * (0.35 if speaking else 1.0)
            if self._bgm is not None and len(self._bgm):
                n = len(self._bgm)
                pos = self._bgm_pos
                take = min(frames, n - pos)
                out[:take] += self._bgm[pos:pos + take] * bgm_gain
                if take < frames:                      # 循环回开头
                    rest = frames - take
                    out[take:take + rest] += self._bgm[:rest] * bgm_gain
                    self._bgm_pos = rest % n if n else 0
                else:
                    self._bgm_pos = (pos + take) % n if n else 0
            if self._tts is not None and self._tts_pos < len(self._tts):
                pos = self._tts_pos
                take = min(frames, len(self._tts) - pos)
                out[:take] += self._tts[pos:pos + take]
                self._tts_pos += take
                if self._tts_pos >= len(self._tts):
                    self._tts = None
                    self._tts_pos = 0
        np.clip(out, -1.0, 1.0, out=out)
        outdata[:] = out

    def set_bgm(self, data: np.ndarray | None) -> None:
        with self._lock:
            self._bgm = data
            self._bgm_pos = 0

    def play_tts(self, data: np.ndarray) -> None:
        with self._lock:
            self._tts = data
            self._tts_pos = 0

    def stop_tts(self) -> None:
        with self._lock:
            self._tts = None
            self._tts_pos = 0

    def close(self) -> None:
        try:
            self._stream.abort()
            self._stream.close()
        except Exception:
            pass


class Player:
    def __init__(self) -> None:
        self.keyword = os.environ.get("AUDIO_OUT_DEVICE") or "CABLE"
        self.monitor = os.environ.get("AUDIO_MONITOR", "1") != "0"
        self.bgm_vol = float(os.environ.get("BGM_VOLUME") or 0.28)
        self._mixers: list[_Mixer] = []
        self._devs: list[tuple[str, int]] | None = None
        self._bgm_data: np.ndarray | None = None
        self._bgm_thread: threading.Thread | None = None

    # ---------- 设备 ----------
    def devices(self) -> list[tuple[str, int]]:
        if self._devs is not None:
            return self._devs
        out: list[tuple[str, int]] = []
        cable = open_output(self.keyword)
        if cable:
            out.append(("cable", cable[0]))
            print(f"[音频] 送观众 → #{cable[0]} {cable[1]}", flush=True)
        else:
            print(f"[音频] 没有可用的虚拟声卡（关键字 {self.keyword}）", flush=True)
        if self.monitor:
            spk = open_output("扬声器") or open_output("Speakers")
            if spk and (not cable or spk[0] != cable[0]):
                out.append(("speaker", spk[0]))
                print(f"[音频] 你自己听 → #{spk[0]} {spk[1]}", flush=True)
        self._devs = out
        return out

    def _ensure_mixers(self) -> list[_Mixer]:
        if not self._mixers:
            for name, idx in self.devices():
                try:
                    self._mixers.append(_Mixer(idx, name, self.bgm_vol))
                except Exception as e:
                    print(f"[音频] {name}(#{idx}) 打不开: {type(e).__name__}: {e}", flush=True)
        return self._mixers

    # ---------- 语音 ----------
    def play(self, path: str | Path) -> dict:
        try:
            data, _sr = decode(path)
        except Exception as e:
            return {"ok": False, "error": str(e)}
        # TTS 接口回来的语音峰值只有 0.3 左右，跟 BGM 一样响，会被盖住。
        # 归一化到 0.9，人声才压得住音乐。
        peak = float(np.abs(data).max()) if len(data) else 0.0
        if peak > 1e-4:
            data = data * (TTS_PEAK / peak)
        mixers = self._ensure_mixers()
        if not mixers:
            return {"ok": False, "error": "没有可用输出设备"}
        for m in mixers:
            m.play_tts(data)
        return {"ok": True, "devices": [m.name for m in mixers],
                "seconds": round(len(data) / SR, 2)}

    def stop(self) -> None:
        for m in self._mixers:
            m.stop_tts()

    # ---------- 背景音乐 ----------
    def start_bgm(self, folder: Path | None = None) -> dict:
        """扫描 bgm/ 目录，把里面的音频拼成一条循环流。"""
        folder = folder or BGM_DIR
        files = sorted([p for p in folder.glob("*")
                        if p.suffix.lower() in (".mp3", ".wav", ".m4a", ".flac")])
        if not files:
            print(f"[BGM] {folder} 里没有音频文件，跳过", flush=True)
            return {"ok": False, "error": "没有 BGM 文件"}

        def _build() -> None:
            chunks = []
            for f in files:
                try:
                    d, _ = decode(f)
                    # 首尾各加 1.5 秒淡入淡出，循环时不突兀
                    fade = int(SR * 1.5)
                    if len(d) > fade * 2:
                        d = d.copy()
                        ramp = np.linspace(0, 1, fade, dtype=np.float32)[:, None]
                        d[:fade] *= ramp
                        d[-fade:] *= ramp[::-1]
                    chunks.append(d)
                    print(f"[BGM] 载入 {f.name}（{len(d)/SR:.0f}s）", flush=True)
                except Exception as e:
                    print(f"[BGM] {f.name} 解码失败: {e}", flush=True)
            if not chunks:
                return
            merged = np.concatenate(chunks, axis=0)
            self._bgm_data = merged
            for m in self._ensure_mixers():
                m.set_bgm(merged)
            print(f"[BGM] 循环播放中，共 {len(files)} 首 / {len(merged)/SR/60:.1f} 分钟", flush=True)

        self._bgm_thread = threading.Thread(target=_build, daemon=True)
        self._bgm_thread.start()
        return {"ok": True, "files": [f.name for f in files]}

    def bgm_state(self) -> dict:
        return {"loaded": self._bgm_data is not None,
                "minutes": round(len(self._bgm_data) / SR / 60, 1) if self._bgm_data is not None else 0,
                "volume": self.bgm_vol}


_player: Player | None = None


def player() -> Player:
    global _player
    if _player is None:
        _player = Player()
    return _player


if __name__ == "__main__":
    import sys
    p = player()
    print("输出设备:", p.devices())
    if len(sys.argv) > 1 and sys.argv[1] == "bgm":
        print(p.start_bgm())
        import time
        time.sleep(30)
    elif len(sys.argv) > 1:
        print(p.play(sys.argv[1]))
