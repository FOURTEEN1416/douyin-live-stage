"""抓一批原始消息存成 jsonl（UTF-8），用来核对解析器

    python _capture_raw.py ws://... [条数] [秒数]
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from wsclient import WSError, WSClient

OUT = Path(__file__).resolve().parent / "data" / "_raw_msgs.jsonl"


def main() -> None:
    url = sys.argv[1]
    limit = int(sys.argv[2]) if len(sys.argv) > 2 else 60
    secs = float(sys.argv[3]) if len(sys.argv) > 3 else 25.0
    OUT.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    t0 = time.time()
    try:
        c = WSClient(url, timeout=int(secs) + 10)
        c.connect()
        for raw in c.messages():
            rows.append(raw)
            if len(rows) >= limit or time.time() - t0 > secs:
                break
    except (WSError, OSError) as e:
        print(f"[×] {type(e).__name__}: {e}")
    with OUT.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(r.replace("\n", " ") + "\n")
    print(f"存了 {len(rows)} 条 → {OUT}")


if __name__ == "__main__":
    main()
