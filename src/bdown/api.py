"""bilibili Web 接口封装。"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

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
BASE_HEADERS = {
    "User-Agent": USER_AGENT,
    "Referer": "https://www.bilibili.com/",
    "Origin": "https://www.bilibili.com",
    "Accept-Language": "zh-CN,zh;q=0.9",
}

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
_AV_RE = re.compile(r"av(\d+)", re.I)


def parse_target(text: str) -> tuple[str | None, int | None]:
    """从 BV 号、av 号或视频链接里解析出 (bvid, aid)。"""
    if match := _BV_RE.search(text):
        return match.group(0), None
    if match := _AV_RE.search(text):
        return None, int(match.group(1))
    raise ValueError(f"无法从 {text!r} 中识别 BV 号或 av 号")


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


class BilibiliClient:
    def __init__(self, cookie: str = "", timeout: float = 15.0):
        headers = dict(BASE_HEADERS)
        if cookie:
            headers["Cookie"] = cookie
        self.http = httpx.Client(
            headers=headers, timeout=timeout, follow_redirects=True
        )
        self._wbi_keys: tuple[str, str] | None = None
        self.logged_in = False
        self.vip = False

    def close(self) -> None:
        self.http.close()

    def __enter__(self) -> "BilibiliClient":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def _get_json(self, url: str, params: dict | None = None) -> dict:
        resp = self.http.get(url, params=params)
        resp.raise_for_status()
        body = resp.json()
        code = body.get("code")
        if code != 0:
            hint = CODE_MESSAGES.get(code) or body.get("message") or "接口返回异常"
            raise BilibiliError(f"{hint}（code={code}）")
        return body.get("data") or {}

    def wbi_keys(self) -> tuple[str, str]:
        """nav 接口下发的签名密钥，每天轮换一次，进程内缓存。"""
        if self._wbi_keys is None:
            # 未登录时 nav 的 code 是 -101，但 wbi_img 照样下发，所以不走 _get_json
            resp = self.http.get(API_NAV)
            resp.raise_for_status()
            body = resp.json()
            data = body.get("data") or {}
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
