"""分块并发下载，支持断点续传。"""

from __future__ import annotations

import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import httpx

CHUNK_SIZE = 4 * 1024 * 1024  # 单块 4MiB，兼顾续传粒度与请求开销
READ_SIZE = 256 * 1024
MAX_RETRY = 4
STATE_SUFFIX = ".bdown"


class DownloadError(RuntimeError):
    pass


@dataclass
class _Chunk:
    start: int
    end: int  # 闭区间
    done: int = 0

    @property
    def remaining(self) -> int:
        return self.end - self.start + 1 - self.done


class _State:
    """记录每块已落盘的字节数，使中断后能接着下。"""

    def __init__(self, path: Path, size: int, chunks: list[_Chunk]):
        self.path = path
        self.size = size
        self.chunks = chunks
        self.lock = threading.Lock()
        self._last_flush = 0.0

    @classmethod
    def load_or_new(cls, path: Path, size: int) -> "_State":
        chunks = [
            _Chunk(start, min(start + CHUNK_SIZE, size) - 1)
            for start in range(0, size, CHUNK_SIZE)
        ]
        state = cls(path, size, chunks)
        try:
            saved = json.loads(path.read_text())
        except (OSError, ValueError):
            return state
        # 文件大小一致才认为是同一个流；CDN 地址带时效签名，不参与比对
        if saved.get("size") == size and len(saved.get("done", [])) == len(chunks):
            for chunk, done in zip(chunks, saved["done"]):
                chunk.done = min(int(done), chunk.end - chunk.start + 1)
        return state

    @property
    def downloaded(self) -> int:
        return sum(c.done for c in self.chunks)

    def flush(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_flush < 1.0:
            return
        self._last_flush = now
        payload = {"size": self.size, "done": [c.done for c in self.chunks]}
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload))
        tmp.replace(self.path)

    def discard(self) -> None:
        self.path.unlink(missing_ok=True)


def _probe(client: httpx.Client, urls: list[str]) -> tuple[str, int, bool]:
    """探测可用地址，返回 (url, 总长度, 是否支持 Range)。"""
    last_error: Exception | None = None
    for url in urls:
        try:
            # 用 1 字节的 Range 请求探测，比 HEAD 更可靠（部分 CDN 不接 HEAD）
            resp = client.get(url, headers={"Range": "bytes=0-0"})
            if resp.status_code == 206:
                total = int(resp.headers["content-range"].split("/")[-1])
                return url, total, True
            if resp.status_code == 200:
                return url, int(resp.headers.get("content-length") or 0), False
            last_error = DownloadError(f"HTTP {resp.status_code}")
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            last_error = exc
    raise DownloadError(f"所有地址均不可用：{last_error}")


def _fetch_chunk(
    client: httpx.Client,
    urls: list[str],
    fd: int,
    chunk: _Chunk,
    state: _State,
    on_progress: Callable[[int], None],
) -> None:
    for attempt in range(MAX_RETRY):
        if chunk.remaining <= 0:
            return
        url = urls[attempt % len(urls)]  # 重试时轮换到备用地址
        start = chunk.start + chunk.done
        try:
            headers = {"Range": f"bytes={start}-{chunk.end}"}
            with client.stream("GET", url, headers=headers) as resp:
                if resp.status_code not in (200, 206):
                    raise DownloadError(f"HTTP {resp.status_code}")
                offset = start
                for data in resp.iter_bytes(READ_SIZE):
                    if not data:
                        continue
                    os.pwrite(fd, data, offset)
                    offset += len(data)
                    with state.lock:
                        chunk.done += len(data)
                        state.flush()
                    on_progress(len(data))
            if chunk.remaining <= 0:
                return
            raise DownloadError(f"数据不完整，还差 {chunk.remaining} 字节")
        except (httpx.HTTPError, DownloadError, OSError) as exc:
            if attempt == MAX_RETRY - 1:
                raise DownloadError(f"分块 {chunk.start} 下载失败：{exc}") from exc
            time.sleep(1.5 * (attempt + 1))


def download(
    client: httpx.Client,
    urls: list[str],
    dest: Path,
    threads: int = 8,
    on_total: Callable[[int, int], None] | None = None,
    on_progress: Callable[[int], None] = lambda _n: None,
) -> Path:
    """把 urls 指向的同一份资源下载到 dest。

    on_total 收到 (总字节数, 已完成字节数)；on_progress 收到每次新增字节数。
    """
    urls = [u for u in urls if u]
    if not urls:
        raise DownloadError("没有可用的下载地址")

    dest.parent.mkdir(parents=True, exist_ok=True)
    url, total, ranged = _probe(client, urls)
    part = dest.with_suffix(dest.suffix + ".part")
    # 探测成功的地址排到最前，其余留作重试备用
    ordered = [url] + [u for u in urls if u != url]

    if not ranged or total <= 0:
        if on_total:
            on_total(total, 0)
        _download_plain(client, ordered, part, on_progress)
        part.replace(dest)
        return dest

    state = _State.load_or_new(dest.with_suffix(dest.suffix + STATE_SUFFIX), total)
    if on_total:
        on_total(total, state.downloaded)

    pending = [c for c in state.chunks if c.remaining > 0]
    fd = os.open(part, os.O_RDWR | os.O_CREAT)
    try:
        os.ftruncate(fd, total)
        if pending:
            workers = max(1, min(threads, len(pending)))
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = [
                    pool.submit(
                        _fetch_chunk, client, ordered, fd, chunk, state, on_progress
                    )
                    for chunk in pending
                ]
                for future in as_completed(futures):
                    future.result()  # 让首个异常尽快冒出来
    finally:
        os.close(fd)
        state.flush(force=True)

    if state.downloaded != total:
        raise DownloadError(f"下载不完整：{state.downloaded}/{total} 字节")

    part.replace(dest)
    state.discard()
    return dest


def _download_plain(
    client: httpx.Client,
    urls: list[str],
    part: Path,
    on_progress: Callable[[int], None],
) -> None:
    """不支持 Range 时的单连接下载。"""
    last_error: Exception | None = None
    for url in urls:
        try:
            with client.stream("GET", url) as resp:
                resp.raise_for_status()
                with open(part, "wb") as fh:
                    for data in resp.iter_bytes(READ_SIZE):
                        fh.write(data)
                        on_progress(len(data))
            return
        except (httpx.HTTPError, OSError) as exc:
            last_error = exc
    raise DownloadError(f"下载失败：{last_error}")
