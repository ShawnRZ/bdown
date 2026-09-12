import json
from pathlib import Path

import pytest

from bdown import wbi

VECTORS = json.loads((Path(__file__).parent / "wbi_vectors.json").read_text())


@pytest.mark.parametrize("vector", VECTORS["vectors"], ids=lambda v: v["path"])
def test_sign_matches_browser(vector):
    """签名必须与浏览器实际发出的 w_rid 逐字节一致。"""
    signed = wbi.sign(
        vector["params"], VECTORS["img_key"], VECTORS["sub_key"], ts=vector["wts"]
    )
    assert signed["w_rid"] == vector["w_rid"]
    assert signed["wts"] == vector["wts"]


def test_mixin_key_is_32_chars():
    key = wbi.get_mixin_key(VECTORS["img_key"], VECTORS["sub_key"])
    assert len(key) == 32


def test_sign_overwrites_stale_signature():
    """带着旧签名再签一次，不能把旧的 w_rid / wts 混进计算。"""
    params = {"bvid": "BV1kktD69EaX", "cid": 1}
    fresh = wbi.sign(params, VECTORS["img_key"], VECTORS["sub_key"], ts=1700000000)
    resigned = wbi.sign(
        {**params, **fresh}, VECTORS["img_key"], VECTORS["sub_key"], ts=1700000000
    )
    assert resigned["w_rid"] == fresh["w_rid"]


def test_quote_matches_urlsearchparams():
    assert wbi._quote("a b") == "a+b"
    assert wbi._quote('{"offset":""}') == "%7B%22offset%22%3A%22%22%7D"
    assert wbi._quote("中文") == "%E4%B8%AD%E6%96%87"
    assert wbi._quote("~*-._") == "%7E*-._"


def test_key_from_url():
    assert wbi.key_from_url("https://i0.hdslb.com/bfs/wbi/abc123.png") == "abc123"
