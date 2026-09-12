"""一个支持 Range 的本地测试服务器，用于在不联网的前提下验证下载逻辑。"""

from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest


def make_payload(size: int) -> bytes:
    """生成确定性内容，便于逐字节比对。"""
    out = bytearray()
    seed = b"bdown"
    while len(out) < size:
        seed = hashlib.sha256(seed).digest()
        out += seed
    return bytes(out[:size])


@dataclass
class ServerLog:
    ranges: list[str] = field(default_factory=list)
    hits: dict[str, int] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def record(self, path: str, rng: str | None) -> int:
        with self.lock:
            self.hits[path] = self.hits.get(path, 0) + 1
            if rng:
                self.ranges.append(rng)
            return self.hits[path]


# 短链展开用的跳转表，Location 故意写成相对路径以覆盖 URL 拼接
REDIRECTS = {
    "/short": "/video/BV1wUYQ6ME4Q/?p=1&share_source=copy",
    "/hop1": "/hop2",
    "/hop2": "/video/BV1kktD69EaX/",
    "/loop": "/loop",
    "/nobv": "/bangumi/play/ep123",
}


@pytest.fixture
def payload() -> bytes:
    return make_payload(5000)


@pytest.fixture
def server(payload):
    """路由说明：

    /file      正常资源，支持 Range
    /norange   忽略 Range，总是整体返回
    /broken    总是 500
    /flaky     首次请求断在中途，之后正常
    其余路径   按 REDIRECTS 表跳转，或直接返回内容
    """
    log = ServerLog()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args):  # 静音访问日志
            pass

        def _send(self, body: bytes, status: int = 200, extra: dict | None = None):
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            for key, value in (extra or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            rng = self.headers.get("Range")
            count = log.record(self.path, rng)

            if self.path == "/broken":
                self._send(b"nope", 500)
                return

            if self.path == "/norange":
                self._send(payload)
                return

            if self.path in REDIRECTS:
                self._send(b"", 302, {"Location": REDIRECTS[self.path]})
                return

            start, end = 0, len(payload) - 1
            if rng:
                spec = rng.split("=", 1)[1]
                head, _, tail = spec.partition("-")
                start = int(head)
                end = int(tail) if tail else len(payload) - 1
            chunk = payload[start : end + 1]

            if self.path == "/flaky" and count == 1:
                # 只回一半且不补齐，逼下载器走重试路径
                half = chunk[: max(1, len(chunk) // 2)]
                self.send_response(206)
                self.send_header("Content-Length", str(len(chunk)))
                self.send_header(
                    "Content-Range", f"bytes {start}-{end}/{len(payload)}"
                )
                self.end_headers()
                self.wfile.write(half)
                self.close_connection = True
                return

            if rng:
                self._send(
                    chunk,
                    206,
                    {"Content-Range": f"bytes {start}-{end}/{len(payload)}"},
                )
            else:
                self._send(payload, 200, {"Accept-Ranges": "bytes"})

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    httpd.log = log
    httpd.base = f"http://127.0.0.1:{httpd.server_address[1]}"
    yield httpd
    httpd.shutdown()
    httpd.server_close()


@pytest.fixture
def client():
    with httpx.Client(timeout=10.0) as http:
        yield http
