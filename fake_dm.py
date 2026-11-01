"""假弹幕源：本地 ws 服务器，按真实抓取工具的 JSON 结构发弹幕

用来在没有真实弹幕源时验证「弹幕 → LLM → 应答 → TTS → 字幕」整条链路。

    python fake_dm.py 1088
    python fake_dm.py 1088 '{"nickname":"小林","content":"这是真人还是AI"}'
"""

from __future__ import annotations

import base64
import hashlib
import json
import socket
import struct
import sys
import time

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

DEFAULT = [
    {"data": {"nickname": "小林", "content": "这是真人还是AI？"}},
    {"data": {"nickname": "阿白", "content": "主播你在写什么代码呀"}},
]


def _frame(text: str) -> bytes:
    payload = text.encode()
    head = bytearray([0x81])
    n = len(payload)
    if n < 126:
        head.append(n)
    elif n < 65536:
        head.append(126)
        head += struct.pack(">H", n)
    else:
        head.append(127)
        head += struct.pack(">Q", n)
    return bytes(head) + payload


def handle(conn: socket.socket, msgs: list[dict], delay: float = 3.0) -> None:
    head = b""
    while b"\r\n\r\n" not in head:
        chunk = conn.recv(4096)
        if not chunk:
            return
        head += chunk
    key = ""
    for line in head.decode("utf-8", "replace").split("\r\n"):
        if line.lower().startswith("sec-websocket-key:"):
            key = line.split(":", 1)[1].strip()
    accept = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
    conn.sendall(
        ("HTTP/1.1 101 Switching Protocols\r\n"
         "Upgrade: websocket\r\nConnection: Upgrade\r\n"
         f"Sec-WebSocket-Accept: {accept}\r\n\r\n").encode()
    )
    print("[fake] 已握手，等推送…")
    for m in msgs:
        time.sleep(delay)
        payload = json.dumps(m, ensure_ascii=False)
        conn.sendall(_frame(payload))
        print("[fake] 发出:", payload)
    time.sleep(600)
    conn.close()


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 1088
    if len(sys.argv) > 2:
        msgs = [json.loads(sys.argv[2])]
    else:
        msgs = DEFAULT
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", port))
    srv.listen(5)
    print(f"[fake] 弹幕源已监听 ws://127.0.0.1:{port}")
    while True:
        conn, _ = srv.accept()
        print("[fake] 有客户端连上")
        try:
            handle(conn, msgs)
        except Exception as e:
            print("[fake] 连接结束:", type(e).__name__)


if __name__ == "__main__":
    main()
