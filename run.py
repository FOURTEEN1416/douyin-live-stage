"""一键启动器

    python run.py --mode stage             只起舞台（手动推台词）
    python run.py --mode auto              舞台 + 自动旁白（推荐）
    python run.py --mode all --ws ws://127.0.0.1:1088/ws/房间号
                                           全部：舞台 + 旁白 + 弹幕桥
    python run.py --mode bridge --ws ...   只起弹幕桥

配置放同目录 `.env`，或临时用环境变量覆盖。
Ctrl+C 一次停掉所有子进程。
"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PY = sys.executable

# 必须在解析参数之前加载 .env —— DOUYIN_ROOM 的默认值要读它
try:
    import env_loader  # noqa: F401
except Exception:
    pass

PROCS: list[dict] = []          # {name, args, proc, restarts}
MAX_RESTARTS = 20


def spawn(name: str, args: list[str], pipe: bool = True) -> subprocess.Popen:
    if pipe:
        p = subprocess.Popen([PY, "-u"] + args, cwd=str(ROOT),
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, encoding="utf-8", errors="replace",
                             bufsize=1)
        # 每个进程一个读线程：原来在主循环里 readline，某个进程不吐字就会
        # 把整个循环卡住，其它进程的日志和看护全停摆
        threading.Thread(target=_reader, args=(name, p), daemon=True).start()
    else:
        # 悬浮层这类带 GUI 的进程不接管管道（实测接管后 WebView2 页面加载不出来）
        p = subprocess.Popen([PY, "-u"] + args, cwd=str(ROOT))
    PROCS.append({"name": name, "args": args, "pipe": pipe,
                  "proc": p, "restarts": 0})
    return p


def _reader(name: str, p: subprocess.Popen) -> None:
    try:
        for line in p.stdout:                       # type: ignore[union-attr]
            print(f"[{name}] {line.rstrip()}", flush=True)
    except Exception:
        pass


def supervise() -> None:
    """看护子进程：退出了就重新拉起来。

    直播场景下服务静默挂掉很致命 —— 舞台一断，页面会自己降级成 demo
    （显示假代码/假注册页/假微信），从外面看就像"画面变成这样了"。
    """
    while True:
        time.sleep(3)
        for item in PROCS:
            p: subprocess.Popen = item["proc"]
            if p.poll() is None:
                continue
            code = p.returncode
            if item["name"] == "抓取":        # 外部 exe，别反复拉
                continue
            if item["restarts"] >= MAX_RESTARTS:
                print(f"[x] {item['name']} 重启 {MAX_RESTARTS} 次仍失败，放弃", flush=True)
                continue
            item["restarts"] += 1
            print(f"[!] {item['name']} 退出了（code={code}），第 {item['restarts']} 次重启",
                  flush=True)
            time.sleep(2)
            try:
                item["proc"] = _respawn(item)
            except Exception as e:
                print(f"[x] {item['name']} 重启失败：{type(e).__name__}: {e}", flush=True)


def _respawn(item: dict) -> subprocess.Popen:
    name, args, pipe = item["name"], item["args"], item["pipe"]
    if pipe:
        p = subprocess.Popen([PY, "-u"] + args, cwd=str(ROOT),
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, encoding="utf-8", errors="replace",
                             bufsize=1)
        threading.Thread(target=_reader, args=(name, p), daemon=True).start()
    else:
        p = subprocess.Popen([PY, "-u"] + args, cwd=str(ROOT))
    return p


def wait_for_server(port: int, timeout: float = 30.0) -> bool:
    """等舞台服务真的在监听（否则悬浮层会打开一个连不上的页面 → 白屏）"""
    import urllib.request
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state",
                                        timeout=3) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.5)
    print(f"[!] 舞台服务 {timeout:.0f}s 内没起来")
    return False


def check_env() -> None:
    try:
        import env_loader  # noqa: F401
    except Exception:
        pass
    if not (os.environ.get("MIMO_API_KEY") or "").strip():
        print("[!] 没有 MIMO_API_KEY —— 舞台会跑，但不会出声")
        print("    在 .env 里写一行：MIMO_API_KEY=sk-xxxx")
    if not (os.environ.get("AGNES_API_KEY") or os.environ.get("VISION_API_KEY")):
        print("[!] 没有识图 key（AGNES_API_KEY / VISION_API_KEY）—— 自动旁白会失败")


def show_intro(port: int) -> None:
    print("=" * 58)
    print(" 大肥鱼直播间")
    print("=" * 58)
    print(f" 舞台（OBS 用）  http://127.0.0.1:{port}/stage.html")
    print(f" 舞台（预览）    http://127.0.0.1:{port}/stage.html?demo=1")
    print(f" 控制台          http://127.0.0.1:{port}/panel.html")
    print()


def shutdown(*_a) -> None:
    print("\n正在停止…")
    for item in PROCS:
        p = item["proc"]
        if p.poll() is None:
            try:
                p.terminate()
            except Exception:
                pass
    for item in PROCS:
        p = item["proc"]
        try:
            p.wait(timeout=5)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass
    print("已停止")


def acquire_lock():
    """单实例锁：防止同时跑多个实例（会叠出好几个悬浮层/合成器，画面全乱）"""
    import atexit
    lock = ROOT / ".run.lock"
    if lock.exists():
        try:
            pid = int(lock.read_text(encoding="utf-8").strip())
            import subprocess
            subprocess.run(["tasklist", "/FI", f"PID eq {pid}"],
                           capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
            os.kill(pid, 0)          # 还在就报错
            print(f"[!] 已经有一个实例在跑（pid={pid}），先把它关掉：")
            print(f"    taskkill /PID {pid} /F")
            raise SystemExit(1)
        except (ValueError, OSError, ProcessLookupError):
            lock.unlink(missing_ok=True)   # 旧锁，删掉
    lock.write_text(str(os.getpid()), encoding="utf-8")
    atexit.register(lambda: lock.unlink(missing_ok=True))
    print(f"[√] 单实例锁已建立（pid={os.getpid()}）")


def ensure_grabber() -> bool:
    """确保 douyinLive 抓取服务在跑（1088 端口）。

    抖音没有开放接口，弹幕必须靠这个本地服务转发。它没起就先把舞台跑起来，
    弹幕那边等它——而不是整个流程卡住。
    """
    import socket

    def listening(port: int = 1088) -> bool:
        with socket.socket() as s:
            s.settimeout(0.4)
            return s.connect_ex(("127.0.0.1", port)) == 0

    if listening():
        print("[√] 抓取服务已在运行（1088）")
        return True

    exe = ROOT / "tools" / "douyinLive" / "douyinLive.exe"
    if not exe.is_file():
        print(f"[!] 没有抓取工具：{exe}")
        print("    下载：https://github.com/jwwsjlm/douyinLive/releases")
        return False

    print("[..] 启动抓取服务…")
    p = subprocess.Popen([str(exe)], cwd=str(exe.parent),
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    PROCS.append({"name": "抓取", "args": [], "pipe": False,
                  "proc": p, "restarts": 0})
    for _ in range(30):
        time.sleep(0.5)
        if listening():
            print("[√] 抓取服务就绪（1088）")
            return True
    print("[!] 抓取服务 15 秒内没起来")
    return False


def setup_log() -> None:
    """把 stdout/stderr 落到 logs/run-*.log。

    计划任务启动时没有控制台，print 出去的东西全丢了 —— 出问题时等于瞎的。
    注意必须用 os.dup2 改 **OS 层**句柄：只换 sys.stdout 的话，子进程
    （悬浮层等）继承的还是原来的无效句柄，它们的输出照样丢。
    """
    try:
        d = ROOT / "logs"
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"run-{time.strftime('%Y%m%d-%H%M%S')}.log"
        f = open(path, "a", encoding="utf-8", buffering=1)
        os.dup2(f.fileno(), 1)
        os.dup2(f.fileno(), 2)
        sys.stdout = f
        sys.stderr = f
        print(f"===== 启动 {time.strftime('%Y-%m-%d %H:%M:%S')} =====")
        print(f"日志: {path}")
    except Exception:
        pass


def main() -> None:
    setup_log()
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="auto",
                    choices=["stage", "auto", "all", "bridge"])
    ap.add_argument("--port", type=int, default=8790)
    ap.add_argument("--interval", type=float, default=55,
                    help="旁白间隔秒数（对标节奏 55s；画面没变时会从话题库抛一句）")
    ap.add_argument("--ws", default="", help="弹幕源 ws 地址（给了就不再自动拼）")
    ap.add_argument("--room", default=os.environ.get("DOUYIN_ROOM") or "",
                    help="抖音直播间号；填了就自动起抓取服务 + 弹幕桥")
    ap.add_argument("--no-reply", action="store_true", help="弹幕只读不答")
    ap.add_argument("--noframes", action="store_true",
                    help="悬浮层不画取景框（直播用，只留立绘/字幕/弹幕）")
    args = ap.parse_args()

    acquire_lock()
    check_env()
    show_intro(args.port)

    if args.mode in ("stage", "auto", "all"):
        spawn("舞台", ["server.py", "--port", str(args.port)])
        wait_for_server(args.port)
        # 合成器：自己拼好「三个窗口 + 大肥鱼」的完整画面
        spawn("合成", ["compositor.py"])
        time.sleep(1)
        # 透明悬浮层（盖在真实桌面上，采集工具直接采屏幕即可）
        stage_url = f"http://127.0.0.1:{args.port}/stage.html"
        if args.noframes:
            stage_url += "?noframes=1"
        # 默认不置顶（不挡别的窗口）；直播时要始终可见就设 OVERLAY_ON_TOP=1
        overlay_extra = ["--on-top"] if os.environ.get("OVERLAY_ON_TOP") == "1" else []
        spawn("悬浮层", ["overlay_win.py", "--url", stage_url] + overlay_extra, pipe=False)
        time.sleep(2)
        # 注意：不摆窗口、不锁 z-order。窗口随你拖。
        # 想一键对齐直播位时手动跑：python windows_layout.py --apply
        # 想让它别跑偏：python windows_layout.py --watch 5
    if args.mode in ("auto", "all"):
        spawn("旁白", ["narrator.py", "--interval", str(args.interval)])

    # 弹幕：--ws 优先；只给 --room 就自动拼地址并把抓取服务拉起来
    ws = args.ws
    if not ws and args.room:
        if ensure_grabber():
            ws = f"ws://127.0.0.1:1088/ws/{args.room}"
            print(f"[√] 弹幕源 {ws}")

    want_bridge = args.mode in ("all", "bridge") or (args.mode == "auto" and ws)
    if want_bridge:
        if not ws:
            print("[!] 没有弹幕源：在 .env 填 DOUYIN_ROOM=直播间号，或用 --ws 指定地址")
        else:
            extra = ["--auto-reply"] if not args.no_reply else []
            spawn("弹幕", ["douyin_bridge.py", "--ws", ws] + extra)

    if not PROCS:
        print("没有要启动的进程")
        return

    signal.signal(signal.SIGINT, lambda *_: (shutdown(), sys.exit(0)))
    supervise()


if __name__ == "__main__":
    main()
