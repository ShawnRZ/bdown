"""bilibili Web 接口封装。"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import httpx

from . import wbi

API_NAV = "https://api.bilibili.com/x/web-interface/nav"
API_VIEW = "https://api.bilibili.com/x/web-interface/view"
API_PLAYURL = "https://api.bilibili.com/x/player/wbi/playurl"

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
)

# CDN 会校验来路，缺了 Referer 一律 403
# 尽量贴近 Chrome 的请求头。缺少这些头的请求更容易被判为爬虫而吃 412
BASE_HEADERS = {
    "User-Agent": USER_AGENT,
    "Referer": "https://www.bilibili.com/",
    "Origin": "https://www.bilibili.com",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "sec-ch-ua": '"Not;A=Brand";v="8", "Chromium";v="150", "Google Chrome";v="150"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Linux"',
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-site",
}

HOME_URL = "https://www.bilibili.com/"
# 官方的设备标识接口，返回 b_3 / b_4 即 buvid3 / buvid4
API_SPI = "https://api.bilibili.com/x/frontend/finger/spi"

COOKIE_DOMAIN = ".bilibili.com"

# 风控拦截：HTTP 412，或业务层的这几个 code
RISK_CODES = {-412, -352}
RETRY_ATTEMPTS = 3
RETRY_DELAY = 2.0

RISK_HINT = (
    "请求被 bilibili 风控拦截（HTTP 412 / code=-412）。"
    "机房与云服务器 IP 特别容易触发，可以试：\n"
    "  1) 提供登录 Cookie（含 SESSDATA）——最有效，见 README「关于清晰度与登录」\n"
    "  2) 降低并发并放慢节奏：-j 2\n"
    "  3) 等几分钟再试，或换出口 IP / 挂代理（设 HTTPS_PROXY 环境变量即可生效）"
)

# fnval 位掩码：DASH(16) + HDR(64) + 4K(128) + 杜比音频(256) + 杜比视界(512) + 8K(1024) + AV1(2048)
FNVAL_ALL = 4048

QUALITY_NAMES = {
    6: "240P 极速",
    16: "360P 流畅",
    32: "480P 清晰",
    64: "720P 高清",
    74: "720P60 高帧率",
    80: "1080P 高清",
    112: "1080P+ 高码率",
    116: "1080P60 高帧率",
    120: "4K 超清",
    125: "HDR 真彩",
    126: "杜比视界",
    127: "8K 超高清",
}

# playurl 只认这三种编码，键为 --codec 的取值
CODEC_PREFIX = {"avc": "avc1", "hevc": "hvc1", "av1": "av01"}


# 接口常见错误码到人话的映射
CODE_MESSAGES = {
    -400: "请求参数有误，通常是 BV 号 / av 号写错或该稿件不存在",
    -403: "权限不足，可能需要登录，或该内容有观看限制",
    -404: "视频不存在或已被删除",
    -10403: "该内容有地区限制，当前网络无法访问",
    62002: "稿件不可见（已被 UP 主隐藏或未通过审核）",
    62004: "稿件正在审核中，暂时无法访问",
    62012: "该稿件仅 UP 主自己可见",
    87007: "付费内容，当前账号无观看权限",
    87008: "付费内容，当前账号无观看权限",
}


class BilibiliError(RuntimeError):
    """接口返回了非 0 的 code。"""


@dataclass
class Target:
    """一次解析的结果：定位到哪个稿件，以及链接是否指定了分 P。"""

    bvid: str | None = None
    aid: int | None = None
    page: int | None = None


@dataclass
class Page:
    """视频的一个分 P。"""

    cid: int
    index: int
    title: str
    duration: int


@dataclass
class VideoInfo:
    bvid: str
    aid: int
    title: str
    owner: str
    cover: str
    pages: list[Page] = field(default_factory=list)


@dataclass
class Stream:
    """一路可下载的媒体流。"""

    url: str
    backups: list[str]
    quality: int
    codec: str
    size_hint: int  # 由码率估算，仅用于展示
    kind: str  # "video" | "audio"
    width: int = 0
    height: int = 0
    frame_rate: str = ""

    @property
    def label(self) -> str:
        if self.kind == "audio":
            return f"音频 {self.codec}"
        name = QUALITY_NAMES.get(self.quality, f"qn={self.quality}")
        return f"{name} {self.width}x{self.height}@{self.frame_rate} {self.codec}"


_BV_RE = re.compile(r"BV[0-9A-Za-z]{10}")
# 前面不能紧跟字母数字，免得把短链里的随机串（如 x1av2bc）误认成 av 号
_AV_RE = re.compile(r"(?:^|[^0-9A-Za-z])av(\d+)", re.I)
# b23.tv / bili2233.cn 是官方短链域名，只展开这两个，不跟随任意外部地址
_SHORT_RE = re.compile(r"(?:https?://)?(?:b23\.tv|bili2233\.cn)/[0-9A-Za-z]+", re.I)

# 只认查询串里的 p=N，避免匹配到标题等无关文本
_PAGE_RE = re.compile(r"[?&]p=(\d+)")

MAX_REDIRECTS = 5


def parse_target(text: str) -> tuple[str | None, int | None]:
    """从 BV 号、av 号或视频链接里解析出 (bvid, aid)。不联网。"""
    if match := _BV_RE.search(text):
        return match.group(0), None
    if match := _AV_RE.search(text):
        return None, int(match.group(1))
    raise ValueError(f"无法从 {text!r} 中识别 BV 号或 av 号")


def parse_page(text: str) -> int | None:
    """取出链接里的 p=N。App 分享多 P 稿件时会用它指明是第几个分 P。"""
    match = _PAGE_RE.search(text)
    if not match:
        return None
    page = int(match.group(1))
    return page if page >= 1 else None


def find_short_link(text: str) -> str | None:
    """从一段文本里揪出 b23.tv 短链，兼容 App 分享出来的整段话。"""
    match = _SHORT_RE.search(text)
    if not match:
        return None
    url = match.group(0)
    return url if url.lower().startswith("http") else f"https://{url}"


def load_cookie(explicit: str | None = None) -> str:
    """按 参数 > 环境变量 > 配置文件 的顺序取 Cookie。"""
    if explicit:
        return explicit.strip()
    if env := os.environ.get("BILI_COOKIE"):
        return env.strip()
    path = Path(
        os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")
    ) / "bdown" / "cookie.txt"
    if path.is_file():
        return path.read_text(encoding="utf-8").strip()
    return ""


def parse_cookie_string(cookie: str) -> list[tuple[str, str]]:
    """把浏览器里复制的 Cookie 串拆成键值对。"""
    pairs = []
    for item in cookie.split(";"):
        name, sep, value = item.partition("=")
        name, value = name.strip(), value.strip().strip('"')
        if sep and name:
            pairs.append((name, value))
    return pairs


class BilibiliClient:
    def __init__(self, cookie: str = "", timeout: float = 15.0):
        self.http = httpx.Client(
            headers=dict(BASE_HEADERS), timeout=timeout, follow_redirects=True
        )
        # 放进 cookie jar 而不是写死 Cookie 头：既能与接口下发的 Set-Cookie 合并，
        # 也把作用域限制在 bilibili.com，不会把 SESSDATA 带给 CDN
        for name, value in parse_cookie_string(cookie):
            self.http.cookies.set(name, value, domain=COOKIE_DOMAIN)
        self._wbi_keys: tuple[str, str] | None = None
        self._warmed = False
        self.logged_in = False
        self.vip = False

    def close(self) -> None:
        self.http.close()

    def __enter__(self) -> "BilibiliClient":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def warm_up(self) -> None:
        """补齐浏览器才有的设备标识 Cookie，显著降低被风控的概率。

        用户自带的 Cookie 里已有 buvid3 时不去覆盖。
        """
        if self._warmed:
            return
        self._warmed = True
        if not self.http.cookies.get("buvid3"):
            self.refresh_device_ids()
        if not self.http.cookies.get("b_nut"):
            self.http.cookies.set("b_nut", str(int(time.time())), domain=COOKIE_DOMAIN)

    def refresh_device_ids(self) -> None:
        """申领一组新的 buvid。被风控拦下后换一组再试往往就通了。"""
        data = {}
        try:
            resp = self.http.get(API_SPI)
            if resp.status_code == 200:
                data = resp.json().get("data") or {}
        except (httpx.HTTPError, ValueError):
            pass
        for name, key in (("buvid3", "b_3"), ("buvid4", "b_4")):
            if data.get(key):
                self.http.cookies.set(name, str(data[key]), domain=COOKIE_DOMAIN)
        if not self.http.cookies.get("buvid3"):
            # 退路：首页的 Set-Cookie 也会下发 buvid3 与 b_nut
            try:
                self.http.get(HOME_URL)
            except httpx.HTTPError:
                pass

    def _get_json(
        self, url: str, params: dict | None = None, ignore_code: bool = False
    ) -> dict:
        """请求 JSON 接口。风控与 5xx 会换设备标识后退避重试，业务错误直接抛出。"""
        self.warm_up()
        delay = RETRY_DELAY
        last: BilibiliError | None = None

        for attempt in range(RETRY_ATTEMPTS):
            try:
                resp = self.http.get(url, params=params)
            except httpx.HTTPError as exc:
                last = BilibiliError(f"网络请求失败：{exc}")
            else:
                if resp.status_code == 412:
                    last = BilibiliError(RISK_HINT)
                elif resp.status_code >= 500:
                    last = BilibiliError(f"服务端错误（HTTP {resp.status_code}）")
                elif resp.status_code >= 400:
                    raise BilibiliError(f"请求被拒绝（HTTP {resp.status_code}）")
                else:
                    try:
                        body = resp.json()
                    except ValueError:
                        last = BilibiliError("响应不是 JSON，可能被风控页面挡住了")
                    else:
                        code = body.get("code")
                        if code in RISK_CODES:
                            last = BilibiliError(RISK_HINT)
                        elif code != 0 and not ignore_code:
                            hint = (
                                CODE_MESSAGES.get(code)
                                or body.get("message")
                                or "接口返回异常"
                            )
                            raise BilibiliError(f"{hint}（code={code}）")
                        else:
                            return body.get("data") or {}

            if attempt < RETRY_ATTEMPTS - 1:
                self.refresh_device_ids()
                time.sleep(delay)
                delay *= 2

        raise last if last else BilibiliError("请求失败")

    def wbi_keys(self) -> tuple[str, str]:
        """nav 接口下发的签名密钥，每天轮换一次，进程内缓存。"""
        if self._wbi_keys is None:
            # 未登录时 nav 的 code 是 -101，但 wbi_img 照样下发，故容忍非 0 code
            data = self._get_json(API_NAV, ignore_code=True)
            self.logged_in = bool(data.get("isLogin"))
            self.vip = bool(data.get("vipStatus"))
            img = data.get("wbi_img") or {}
            if not img.get("img_url"):
                raise BilibiliError("nav 接口未返回 wbi 密钥")
            self._wbi_keys = (
                wbi.key_from_url(img["img_url"]),
                wbi.key_from_url(img["sub_url"]),
            )
        return self._wbi_keys

    def _signed_get(self, url: str, params: dict) -> dict:
        img_key, sub_key = self.wbi_keys()
        return self._get_json(url, wbi.sign(params, img_key, sub_key))

    def resolve_short_link(self, url: str) -> str:
        """顺着 302 找到真实地址。

        只读响应头里的 Location，不消费正文，所以不会把整个播放页下下来。
        """
        seen: set[str] = set()
        for _ in range(MAX_REDIRECTS):
            if url in seen:
                raise BilibiliError(f"短链跳转成环：{url}")
            seen.add(url)
            try:
                with self.http.stream("GET", url, follow_redirects=False) as resp:
                    location = resp.headers.get("location")
            except httpx.HTTPError as exc:
                raise BilibiliError(f"短链展开失败：{exc}") from exc
            if not location:
                return url
            url = str(httpx.URL(url).join(location))
            # 地址里一旦出现 BV / av 号就不必再跳，省掉后续请求
            if _BV_RE.search(url) or _AV_RE.search(url):
                return url
        return url

    def resolve_target(
        self, text: str, on_expand: Callable[[str, str], None] | None = None
    ) -> Target:
        """把用户给的字符串解析成 Target，必要时先展开短链。"""
        try:
            bvid, aid = parse_target(text)
        except ValueError:
            pass
        else:
            return Target(bvid, aid, parse_page(text))

        short = find_short_link(text)
        if short is None:
            raise ValueError(f"无法从 {text!r} 中识别 BV 号或 av 号")

        final = self.resolve_short_link(short)
        if on_expand:
            on_expand(short, final)
        try:
            bvid, aid = parse_target(final)
        except ValueError:
            raise BilibiliError(
                f"短链展开后是 {final}，其中没有视频稿件"
                "（番剧、直播、动态等不在支持范围内）"
            ) from None
        # 用户原文里的 p= 优先于展开后地址带的
        return Target(bvid, aid, parse_page(text) or parse_page(final))

    def video_info(self, bvid: str | None = None, aid: int | None = None) -> VideoInfo:
        params = {"bvid": bvid} if bvid else {"aid": aid}
        data = self._get_json(API_VIEW, params)
        pages = [
            Page(
                cid=p["cid"],
                index=p["page"],
                title=p.get("part") or f"P{p['page']}",
                duration=p.get("duration", 0),
            )
            for p in data.get("pages", [])
        ]
        return VideoInfo(
            bvid=data["bvid"],
            aid=data["aid"],
            title=data["title"],
            owner=(data.get("owner") or {}).get("name", ""),
            cover=data.get("pic", ""),
            pages=pages,
        )

    def playurl(self, bvid: str, cid: int, qn: int = 127) -> dict:
        return self._signed_get(
            API_PLAYURL,
            {
                "bvid": bvid,
                "cid": cid,
                "qn": qn,
                "fnver": 0,
                "fnval": FNVAL_ALL,
                "fourk": 1,
            },
        )
