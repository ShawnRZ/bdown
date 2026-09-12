"""风控（412）相关行为：退避重试、设备标识补齐、Cookie 作用域。"""

import pytest

from bdown import api
from bdown.api import BilibiliClient, BilibiliError, parse_cookie_string


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch):
    """去掉退避等待，让测试跑得快。"""
    monkeypatch.setattr(api, "RETRY_DELAY", 0)


@pytest.fixture
def bili(monkeypatch, server):
    """设备标识相关地址都指向本地测试服务器，全程不触网。"""
    monkeypatch.setattr(api, "API_SPI", f"{server.base}/spi")
    monkeypatch.setattr(api, "HOME_URL", f"{server.base}/home")
    with BilibiliClient() as client:
        yield client


# --- 重试 ---


def test_retries_http_412_then_succeeds(bili, server):
    assert bili._get_json(f"{server.base}/risk-once") == {"ok": True}
    assert server.log.hits["/risk-once"] == 2


def test_retries_business_risk_code(bili, server):
    assert bili._get_json(f"{server.base}/risk-code") == {"ok": True}
    assert server.log.hits["/risk-code"] == 2


def test_gives_actionable_message_when_always_blocked(bili, server):
    with pytest.raises(BilibiliError) as excinfo:
        bili._get_json(f"{server.base}/risk412")
    message = str(excinfo.value)
    assert "412" in message
    assert "SESSDATA" in message          # 指出最有效的解法
    assert "HTTPS_PROXY" in message       # 以及换出口的办法
    assert server.log.hits["/risk412"] == api.RETRY_ATTEMPTS


def test_business_error_is_not_retried(bili, server):
    """稿件不存在这类错误重试没有意义，应立即抛出。"""
    with pytest.raises(BilibiliError, match="视频不存在"):
        bili._get_json(f"{server.base}/biz-error")
    assert server.log.hits["/biz-error"] == 1


def test_non_json_response_is_treated_as_interception(bili, server):
    with pytest.raises(BilibiliError, match="不是 JSON"):
        bili._get_json(f"{server.base}/notjson")
    assert server.log.hits["/notjson"] == api.RETRY_ATTEMPTS


def test_server_error_is_retried(bili, server):
    with pytest.raises(BilibiliError, match="服务端错误"):
        bili._get_json(f"{server.base}/broken")
    assert server.log.hits["/broken"] == api.RETRY_ATTEMPTS


def test_device_ids_refreshed_between_attempts(bili, server, monkeypatch):
    bili.warm_up()  # 先把预热那次申领消化掉，只统计重试引发的
    calls = []
    monkeypatch.setattr(
        BilibiliClient, "refresh_device_ids", lambda self: calls.append(1)
    )
    with pytest.raises(BilibiliError):
        bili._get_json(f"{server.base}/risk412")
    # 最后一次失败后不必再换，故比尝试次数少一次
    assert len(calls) == api.RETRY_ATTEMPTS - 1


def test_ignore_code_tolerates_nonzero(bili, server):
    """nav 未登录时 code=-101 但数据可用，这种情况不能报错。"""
    assert bili._get_json(f"{server.base}/biz-error", ignore_code=True) == {}


# --- 设备标识 ---


def test_warm_up_acquires_buvid(bili):
    bili.warm_up()
    assert bili.http.cookies.get("buvid3") == "BUVID3TEST"
    assert bili.http.cookies.get("buvid4") == "BUVID4TEST"
    assert bili.http.cookies.get("b_nut")          # 与 buvid3 配套的时间戳


def test_warm_up_keeps_user_supplied_buvid(monkeypatch, server):
    monkeypatch.setattr(api, "API_SPI", f"{server.base}/spi")
    with BilibiliClient(cookie="buvid3=MINE; SESSDATA=secret") as client:
        client.warm_up()
        assert client.http.cookies.get("buvid3") == "MINE"
        assert "/spi" not in server.log.hits     # 已有就不必申领


def test_warm_up_runs_once(bili, server):
    bili.warm_up()
    bili.warm_up()
    assert server.log.hits["/spi"] == 1


def test_falls_back_to_homepage_when_spi_fails(monkeypatch, server):
    monkeypatch.setattr(api, "API_SPI", f"{server.base}/broken")
    monkeypatch.setattr(api, "HOME_URL", f"{server.base}/home")
    with BilibiliClient() as client:
        client.warm_up()
        assert client.http.cookies.get("buvid3") == "FROMHOME"


# --- Cookie 处理 ---


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("SESSDATA=abc", [("SESSDATA", "abc")]),
        ("SESSDATA=abc; bili_jct=def", [("SESSDATA", "abc"), ("bili_jct", "def")]),
        ("  SESSDATA = abc ;; ", [("SESSDATA", "abc")]),
        ('SESSDATA="abc"', [("SESSDATA", "abc")]),
        # 值里含 = 和 % 不能被截断
        ("SESSDATA=a%2Cb%2Cc==", [("SESSDATA", "a%2Cb%2Cc==")]),
        ("", []),
        ("垃圾数据", []),
    ],
)
def test_parse_cookie_string(raw, expected):
    assert parse_cookie_string(raw) == expected


def test_cookies_are_not_leaked_to_other_hosts(server):
    """SESSDATA 只应发往 bilibili.com，不能带给 CDN 或别的主机。"""
    with BilibiliClient(cookie="SESSDATA=secret") as client:
        body = client.http.get(f"{server.base}/echo").json()
    assert "SESSDATA" not in body["cookie"]
