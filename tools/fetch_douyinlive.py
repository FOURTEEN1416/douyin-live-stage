"""一键获取抖音弹幕抓取工具 douyinLive。

    python tools/fetch_douyinlive.py            # 缺才装（run.py 会自动调用）
    python tools/fetch_douyinlive.py --force    # 强制重装
    python tools/fetch_douyinlive.py --check    # 只看状态，不下载

做法：从上游 GitHub release 取当前平台压缩包（Windows 为 windows-amd64.zip，约 12 MB），
校验 sha256，解压到 tools/douyinLive/。已有的 config.yaml 不会被覆盖。

可用环境变量覆盖：
    DOUYINLIVE_VERSION=v2.3.2        指定版本（默认取 latest）
    DOUYINLIVE_URL=<直链>            完全自定义下载地址（跳过 API）

上游：https://github.com/jwwsjlm/douyinLive （MIT）
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEST = ROOT / "tools" / "douyinLive"
UPSTREAM = "jwwsjlm/douyinLive"
UA = {"User-Agent": "dafish-live-stage/fetch"}
CHUNK = 128 * 1024


# ── 基础工具 ──────────────────────────────────────────────

def platform_tag() -> str:
    m = platform.machine().lower()
    arch = "arm64" if m in ("arm64", "aarch64") else "amd64"
    if sys.platform.startswith("win"):
        os_tag = "windows"
    elif sys.platform == "darwin":
        os_tag = "darwin"
    else:
        os_tag = "linux"
    return f"{os_tag}-{arch}"


def exe_path() -> Path:
    name = "douyinLive.exe" if sys.platform.startswith("win") else "douyinLive"
    return DEST / name


def installed() -> bool:
    return exe_path().is_file()


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}GB"


def get_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={**UA, "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def download(url: str, target: Path, title: str = "") -> str:
    """流式下载并返回 sha256。"""
    req = urllib.request.Request(url, headers=UA)
    h = hashlib.sha256()
    with urllib.request.urlopen(req, timeout=60) as r, open(target, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        while True:
            buf = r.read(CHUNK)
            if not buf:
                break
            f.write(buf)
            h.update(buf)
            done += len(buf)
            if total:
                pct = done * 100 // total
                sys.stdout.write(f"\r  {title or '下载'} {human(done)}/{human(total)} ({pct}%)")
                sys.stdout.flush()
    if total:
        sys.stdout.write("\n")
    return h.hexdigest()


# ── 选包 / 校验 ───────────────────────────────────────────

def pick_asset(assets: list[dict], tag: str) -> dict | None:
    """优先当前平台，其次任意 windows 包。"""
    cands = [a for a in assets if a["name"].endswith((".zip", ".tar.gz"))]
    for keyword in (tag, "windows-amd64" if sys.platform.startswith("win") else tag):
        for a in cands:
            if keyword in a["name"]:
                return a
    return None


def checksum_for(assets: list[dict], asset_name: str) -> str | None:
    txt = next((a for a in assets if a["name"].endswith("-checksums.txt")), None)
    if not txt:
        return None
    try:
        req = urllib.request.Request(txt["browser_download_url"], headers=UA)
        with urllib.request.urlopen(req, timeout=30) as r:
            body = r.read().decode("utf-8", "replace")
    except Exception:
        return None
    for line in body.splitlines():
        parts = line.split()
        if not parts:
            continue
        digest = parts[0].strip()
        name = parts[-1].lstrip("*").strip()
        if name == asset_name and len(digest) == 64:
            return digest.lower()
    return None


# ── 解压 ─────────────────────────────────────────────────

def extract(archive: Path) -> list[str]:
    """解压并拍平到 DEST；返回保留未覆盖的已有文件名。"""
    DEST.mkdir(parents=True, exist_ok=True)
    kept: list[str] = []
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        if archive.suffix == ".zip":
            with zipfile.ZipFile(archive) as z:
                z.extractall(tmp)
        else:
            with tarfile.open(archive) as t:
                t.extractall(tmp, filter="data")

        entries = list(tmp.iterdir())
        src = entries[0] if len(entries) == 1 and entries[0].is_dir() else tmp

        for item in sorted(src.rglob("*")):
            rel = item.relative_to(src)
            target = DEST / rel
            if item.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            # 用户自己的配置不覆盖
            if target.exists() and target.name in ("config.yaml", "config.yml"):
                kept.append(str(rel))
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)
    if exe_path().is_file() and not sys.platform.startswith("win"):
        exe_path().chmod(0o755)
    return kept


# ── 主流程 ───────────────────────────────────────────────

def install(force: bool = False) -> int:
    if installed() and not force:
        print(f"[√] 已安装：{exe_path().relative_to(ROOT)}")
        return 0

    tag_platform = platform_tag()
    print(f"[..] 目标平台 {tag_platform}，从 {UPSTREAM} 获取…")

    custom = os.environ.get("DOUYINLIVE_URL")
    version = os.environ.get("DOUYINLIVE_VERSION", "").strip()

    try:
        if custom:
            url, name, expect = custom, Path(custom).name or "douyinLive.zip", None
        else:
            api = (f"https://api.github.com/repos/{UPSTREAM}/releases/tags/{version}"
                   if version else f"https://api.github.com/repos/{UPSTREAM}/releases/latest")
            rel = get_json(api)
            assets = rel.get("assets", [])
            asset = pick_asset(assets, tag_platform)
            if not asset:
                print(f"[!] 该版本没有匹配 {tag_platform} 的包")
                names = "\n".join("    " + a["name"] for a in assets)
                print(names)
                return 1
            url, name = asset["browser_download_url"], asset["name"]
            expect = checksum_for(assets, name)
            print(f"[√] 版本 {rel.get('tag_name')} · {name} · {human(asset.get('size', 0))}")
    except urllib.error.HTTPError as e:
        print(f"[!] 查询上游失败：HTTP {e.code}")
        print(f"    手动下载：https://github.com/{UPSTREAM}/releases")
        return 1
    except Exception as e:
        print(f"[!] 查询上游失败：{type(e).__name__}: {e}")
        print(f"    手动下载：https://github.com/{UPSTREAM}/releases")
        print("    或设 DOUYINLIVE_URL=<压缩包直链> 后重试")
        return 1

    with tempfile.TemporaryDirectory() as td:
        archive = Path(td) / name
        try:
            digest = download(url, archive, "下载")
        except Exception as e:
            print(f"[!] 下载失败：{type(e).__name__}: {e}")
            print(f"    手动下载：https://github.com/{UPSTREAM}/releases")
            return 1

        if expect:
            if digest != expect:
                print(f"[!] 校验不通过\n    期望 {expect}\n    实际 {digest}")
                return 1
            print("[√] sha256 校验通过")
        else:
            print("[i] 上游未提供校验文件，跳过 sha256")

        kept = extract(archive)

    if not exe_path().is_file():
        print(f"[!] 解压后没找到可执行文件：{exe_path()}")
        print("    可能上游改了包结构，请手动下载确认")
        return 1

    if kept:
        print(f"[i] 保留你自己的配置：{', '.join(kept)}")
    print(f"[√] 安装完成：{exe_path().relative_to(ROOT)}（{human(exe_path().stat().st_size)}）")
    return 0


def show_status() -> int:
    tag = platform_tag()
    if installed():
        size = human(exe_path().stat().st_size)
        print(f"[√] 抓取工具已就绪：{exe_path().relative_to(ROOT)}（{size}，目标平台 {tag}）")
        return 0
    print(f"[!] 未安装，目标平台 {tag}，运行 python tools/fetch_douyinlive.py 获取")
    return 1


def main() -> int:
    ap = argparse.ArgumentParser(description="获取抖音弹幕抓取工具 douyinLive")
    ap.add_argument("--force", action="store_true", help="已安装也重新下载")
    ap.add_argument("--check", action="store_true", help="只检查状态，不下载")
    args = ap.parse_args()
    return show_status() if args.check else install(force=args.force)


if __name__ == "__main__":
    sys.exit(main())
