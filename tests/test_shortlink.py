import pytest

from bdown.api import (
    BilibiliClient,
    BilibiliError,
    Target,
    find_short_link,
    parse_page,
    parse_target,
)

SHARE_TEXT = (
    "【这是一个视频标题-UP主名】 https://b23.tv/4lyxfrR "
    "转发自哔哩哔哩，快来看看吧"
)


@pytest.fixture
def bili():
    with BilibiliClient() as client:
        yield client


@pytest.mark.parametrize(
    "text,expected",
    [
        ("https://b23.tv/4lyxfrR", "https://b23.tv/4lyxfrR"),
        ("http://b23.tv/4lyxfrR", "http://b23.tv/4lyxfrR"),
        ("b23.tv/4lyxfrR", "https://b23.tv/4lyxfrR"),          # 缺协议头也认
        (SHARE_TEXT, "https://b23.tv/4lyxfrR"),                # 从整段分享文字里揪出来
        ("https://bili2233.cn/abc123", "https://bili2233.cn/abc123"),
        ("https://www.bilibili.com/video/BV1kktD69EaX", None),
        ("https://evil.example.com/4lyxfrR", None),            # 只展开官方短链域名
        ("随便一句话", None),
    ],
)
def test_find_short_link(text, expected):
    assert find_short_link(text) == expected


def test_short_slug_not_mistaken_for_av_number():
    """短链随机串里可能出现 avN 的字样，不能误判成 av 号。"""
    with pytest.raises(ValueError):
        parse_target("https://b23.tv/x1av2bc")


def test_resolve_short_link_single_hop(bili, server):
    final = bili.resolve_short_link(f"{server.base}/short")
    assert "BV1wUYQ6ME4Q" in final
    # 命中 BV 号就该停下，只发一个请求
    assert server.log.hits == {"/short": 1}


def test_resolve_short_link_follows_chain(bili, server):
    final = bili.resolve_short_link(f"{server.base}/hop1")
    assert "BV1kktD69EaX" in final
    assert server.log.hits == {"/hop1": 1, "/hop2": 1}


def test_resolve_short_link_detects_loop(bili, server):
    with pytest.raises(BilibiliError, match="成环"):
        bili.resolve_short_link(f"{server.base}/loop")


def test_resolve_short_link_stops_at_final_page(bili, server):
    """跳到没有 BV 号的页面时，返回该地址本身而不是报错。"""
    final = bili.resolve_short_link(f"{server.base}/nobv")
    assert final.endswith("/bangumi/play/ep123")


def test_resolve_target_uses_inline_id_without_network(bili, server):
    """文本里已有 BV 号时不该发任何请求。"""
    assert bili.resolve_target(
        SHARE_TEXT.replace("4lyxfrR", "BV1kktD69EaX")
    ) == Target("BV1kktD69EaX", None, None)
    assert server.log.hits == {}


def test_resolve_target_expands_and_reports(bili, monkeypatch):
    monkeypatch.setattr(
        BilibiliClient,
        "resolve_short_link",
        lambda self, url: "https://www.bilibili.com/video/BV1wUYQ6ME4Q/?p=1",
    )
    seen = []
    result = bili.resolve_target(SHARE_TEXT, on_expand=lambda s, f: seen.append((s, f)))
    # 展开后地址里的 p=1 要被带出来
    assert result == Target("BV1wUYQ6ME4Q", None, 1)
    assert seen == [
        ("https://b23.tv/4lyxfrR", "https://www.bilibili.com/video/BV1wUYQ6ME4Q/?p=1")
    ]


def test_resolve_target_rejects_non_video_link(bili, monkeypatch):
    monkeypatch.setattr(
        BilibiliClient,
        "resolve_short_link",
        lambda self, url: "https://www.bilibili.com/bangumi/play/ep123",
    )
    with pytest.raises(BilibiliError, match="没有视频稿件"):
        bili.resolve_target("https://b23.tv/4lyxfrR")


def test_resolve_target_without_any_id(bili):
    with pytest.raises(ValueError, match="无法从"):
        bili.resolve_target("随便一句话")


@pytest.mark.parametrize(
    "text,expected",
    [
        ("https://www.bilibili.com/video/BV1kktD69EaX/?p=3", 3),
        ("https://www.bilibili.com/video/BV1kktD69EaX?spm=x&p=12&y=1", 12),
        ("https://www.bilibili.com/video/BV1kktD69EaX", None),
        ("BV1kktD69EaX", None),
        ("https://www.bilibili.com/video/BV1kktD69EaX/?p=0", None),   # 无效序号
        ("https://www.bilibili.com/video/BV1kktD69EaX/?p=abc", None),
        ("标题里带 p=5 这种字样", None),                              # 非查询串不算
    ],
)
def test_parse_page(text, expected):
    assert parse_page(text) == expected


def test_resolve_target_keeps_page_from_direct_url(bili):
    url = "https://www.bilibili.com/video/BV1kktD69EaX/?p=4"
    assert bili.resolve_target(url) == Target("BV1kktD69EaX", None, 4)


def test_original_page_wins_over_expanded(bili, monkeypatch):
    """用户自己写的 p= 优先于短链展开后地址里的。"""
    monkeypatch.setattr(
        BilibiliClient,
        "resolve_short_link",
        lambda self, url: "https://www.bilibili.com/video/BV1wUYQ6ME4Q/?p=1",
    )
    result = bili.resolve_target("https://b23.tv/4lyxfrR?p=7")
    assert result.page == 7
