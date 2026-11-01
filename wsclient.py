"""极简 WebSocket 客户端（只用标准库，不装 websocket-client）

够用就行：连上去、按行收文本帧、回 pong、断线抛异常。
弹幕桥只需要读，不需要发，所以出站帧只实现了 pong 和 close。
"""

from __future__ import annotations

import base64
import os
import socket
import ssl
import struct
from urllib.parse import urlparse

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class WSError(Exception):
    pass


class WSClient:
    def __init__(self, url: str, timeout: float = 30.0):
        u = urlparse(url)
        if u.scheme not in ("ws", "wss"):
            raise WSError(f"不支持的协议: {u.scheme}")
        self.secure = u.scheme == "wss"
        self.host = u.hostname or "127.0.0.1"
        self.port = u.port or (443 if self.secure else 80)
        self.path = (u.path or "/") + (("?" + u.query) if u.query else "")
        self.timeout = timeout
        self.sock: socket.socket | None = None
        self._buf = b""

    # ---------------------------------------------------------------- 连接
    def connect(self) -> None:
        raw = socket.create_connection((self.host, self.port), timeout=self.timeout)
        raw.settimeout(self.timeout)
        if self.secure:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            raw = ctx.wrap_socket(raw, server_hostname=self.host)  # type: ignore[assignment]
        self.sock = raw

        key = base64.b64encode(os.urandom(16)).decode()
        req = (
            f"GET {self.path} HTTP/1.1\r\n"
            f"Host: {self.host}:{self.port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "User-Agent: live-stage-bridge/1.0\r\n"
            "\r\n"
        )
        self.sock.sendall(req.encode())

        # 读响应头
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise WSError("握手时连接被关闭")
            head += chunk
        header, _, rest = head.partition(b"\r\n\r\n")
        self._buf = rest
        if b"101" not in header.split(b"\r\n")[0]:
            raise WSError("握手失败: " + header.decode("utf-8", "replace")[:200])

    # ---------------------------------------------------------------- 收帧
    def _read(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self.sock.recv(max(4096, n - len(self._buf)))  # type: ignore[union-attr]
            if not chunk:
                raise WSError("连接已关闭")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def _send_frame(self, opcode: int, payload: bytes = b"") -> None:
        if not self.sock:
            return
        mask = os.urandom(4)
        n = len(payload)
        head = bytearray([0x80 | opcode])
        if n < 126:
            head.append(0x80 | n)
        elif n < 65536:
            head.append(0x80 | 126)
            head += struct.pack(">H", n)
        else:
            head.append(0x80 | 127)
            head += struct.pack(">Q", n)
        head += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(bytes(head) + masked)

    def messages(self):
        """生成器：不断吐出文本消息。"""
        while True:
            b1, b2 = self._read(2)
            opcode = b1 & 0x0F
            masked = b2 & 0x80
            ln = b2 & 0x7F
            if ln == 126:
                ln = struct.unpack(">H", self._read(2))[0]
            elif ln == 127:
                ln = struct.unpack(">Q", self._read(8))[0]
            mask = self._read(4) if masked else b""
            data = self._read(ln) if ln else b""
            if mask:
                data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))

            if opcode == 0x8:  # close
                raise WSError("对端关闭了连接")
            if opcode == 0x9:  # ping
                self._send_frame(0xA, data)
                continue
            if opcode == 0xA:  # pong
                continue
            if opcode in (0x1, 0x2):
                try:
                    yield data.decode("utf-8", "replace")
                except Exception:
                    continue

    def close(self) -> None:
        try:
            if self.sock:
                self._send_frame(0x8)
                self.sock.close()
        except Exception:
            pass
        self.sock = None
