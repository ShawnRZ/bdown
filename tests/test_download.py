import json

import pytest

from bdown import download
from bdown.download import DownloadError


@pytest.fixture(autouse=True)
def small_chunks(monkeypatch):
    """把分块调小，让几 KB 的测试数据也能覆盖多块并发路径。"""
    monkeypatch.setattr(download, "CHUNK_SIZE", 1024)
    monkeypatch.setattr(download, "READ_SIZE", 256)


def test_downloads_complete_file(client, server, payload, tmp_path):
    dest = tmp_path / "out.bin"
    seen = {}
    download.download(
        client,
        [f"{server.base}/file"],
        dest,
        threads=4,
        on_total=lambda total, done: seen.update(total=total, done=done),
        on_progress=lambda n: seen.update(bytes=seen.get("bytes", 0) + n),
    )
    assert dest.read_bytes() == payload
    assert seen["total"] == len(payload)
    assert seen["done"] == 0
    assert seen["bytes"] == len(payload)
    # 成功后不留中间文件
    assert not list(tmp_path.glob("*.part"))
    assert not list(tmp_path.glob("*.bdown"))


def test_splits_into_multiple_ranges(client, server, payload, tmp_path):
    download.download(client, [f"{server.base}/file"], tmp_path / "out.bin", threads=4)
    # 5000 字节 / 1024 一块 = 5 块，外加一次探测
    assert len(server.log.ranges) == 6


def test_resumes_from_partial_state(client, server, payload, tmp_path):
    """已下过的块不再重复请求。"""
    dest = tmp_path / "out.bin"
    part = tmp_path / "out.bin.part"
    # 伪造“前两块已完成”的现场
    part.write_bytes(payload[:2048] + bytes(len(payload) - 2048))
    (tmp_path / "out.bin.bdown").write_text(
        json.dumps({"size": len(payload), "done": [1024, 1024, 0, 0, 0]})
    )

    resumed = {}
    download.download(
        client,
        [f"{server.base}/file"],
        dest,
        threads=4,
        on_total=lambda total, done: resumed.update(total=total, done=done),
    )
    assert dest.read_bytes() == payload
    assert resumed["done"] == 2048
    # 探测 1 次 + 剩余 3 块
    assert len(server.log.ranges) == 4


def test_stale_state_is_ignored_when_size_differs(client, server, payload, tmp_path):
    """文件大小不符说明状态文件对应的是别的流，必须整体重下。"""
    dest = tmp_path / "out.bin"
    (tmp_path / "out.bin.part").write_bytes(b"x" * 999)
    (tmp_path / "out.bin.bdown").write_text(json.dumps({"size": 999, "done": [999]}))

    download.download(client, [f"{server.base}/file"], dest, threads=2)
    assert dest.read_bytes() == payload


def test_falls_back_when_server_ignores_range(client, server, payload, tmp_path):
    dest = tmp_path / "out.bin"
    download.download(client, [f"{server.base}/norange"], dest, threads=4)
    assert dest.read_bytes() == payload


def test_uses_backup_url_when_primary_fails(client, server, payload, tmp_path):
    dest = tmp_path / "out.bin"
    download.download(
        client, [f"{server.base}/broken", f"{server.base}/file"], dest, threads=2
    )
    assert dest.read_bytes() == payload


def test_retries_truncated_response(client, server, payload, tmp_path):
    """服务端提前断流时应重试并最终补齐。"""
    dest = tmp_path / "out.bin"
    download.download(client, [f"{server.base}/flaky"], dest, threads=1)
    assert dest.read_bytes() == payload


def test_raises_when_all_urls_fail(client, server, tmp_path):
    with pytest.raises(DownloadError):
        download.download(client, [f"{server.base}/broken"], tmp_path / "out.bin")


def test_rejects_empty_url_list(client, tmp_path):
    with pytest.raises(DownloadError):
        download.download(client, ["", None], tmp_path / "out.bin")
