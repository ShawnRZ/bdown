"""WBI 签名。

bilibili 的部分 Web 接口（playurl 等）要求在查询串里附带 `wts` 时间戳和
`w_rid` 校验值，否则返回 -403。校验值由参数本身和一个每日轮换的
mixin_key 共同决定，mixin_key 则从 nav 接口下发的两张图片文件名推导。
"""

from __future__ import annotations

import hashlib
import string
import time

# 官方前端脚本内置的固定重排表：img_key + sub_key 共 64 字符，
# 按此下标顺序取出前 32 位即 mixin_key。
MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
]

# 参数值里的这几个字符会被前端先剔除再参与签名
_STRIPPED = str.maketrans("", "", "!'()*")

# URLSearchParams.toString() 的保留字符集，空格另外转成 '+'
_SAFE = frozenset(string.ascii_letters + string.digits + "*-._")


def get_mixin_key(img_key: str, sub_key: str) -> str:
    raw = img_key + sub_key
    return "".join(raw[i] for i in MIXIN_KEY_ENC_TAB)[:32]


def _quote(value: str) -> str:
    """按浏览器 URLSearchParams 的规则编码，保证与前端逐字节一致。"""
    out = []
    for byte in value.encode():
        char = chr(byte)
        if char in _SAFE:
            out.append(char)
        elif char == " ":
            out.append("+")
        else:
            out.append(f"%{byte:02X}")
    return "".join(out)


def sign(params: dict, img_key: str, sub_key: str, ts: int | None = None) -> dict:
    """返回补上 `wts` 与 `w_rid` 的参数副本。"""
    signed = {k: v for k, v in params.items() if k not in ("w_rid", "wts")}
    signed["wts"] = int(time.time()) if ts is None else ts

    query = "&".join(
        f"{_quote(k)}={_quote(str(signed[k]).translate(_STRIPPED))}"
        for k in sorted(signed)
    )
    mixin_key = get_mixin_key(img_key, sub_key)
    signed["w_rid"] = hashlib.md5((query + mixin_key).encode()).hexdigest()
    return signed


def key_from_url(url: str) -> str:
    """从 https://i0.hdslb.com/bfs/wbi/<key>.png 里取出 key。"""
    return url.rsplit("/", 1)[-1].split(".")[0]
