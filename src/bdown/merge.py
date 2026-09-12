"""调用 ffmpeg 把 DASH 的音视频轨封装成一个文件。"""

from __future__ import annotations

import re
import shutil
import subprocess
import unicodedata
from pathlib import Path

_ILLEGAL = re.compile(r'[/\\:*?"<>|\x00-\x1f]')
_MAX_BYTES = 180  # 给 .video.m4s 等后缀留出余量，避开文件名长度上限


class MergeError(RuntimeError):
    pass


def has_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None


def safe_name(text: str, fallback: str = "video") -> str:
    """清掉文件名里的非法字符，并按字节数截断。"""
    text = unicodedata.normalize("NFC", text)
    text = _ILLEGAL.sub("_", text).strip(" .")
    while len(text.encode("utf-8")) > _MAX_BYTES:
        text = text[:-1]
    # 全由非法字符组成的标题清洗后只剩下划线，这种名字没有意义，改用 BV 号
    return text if text.strip("_ ") else fallback


def _run(args: list[str]) -> None:
    proc = subprocess.run(args, capture_output=True, text=True)
    if proc.returncode != 0:
        tail = "\n".join(proc.stderr.strip().splitlines()[-6:])
        raise MergeError(f"ffmpeg 执行失败（退出码 {proc.returncode}）：\n{tail}")


def merge(video: Path, audio: Path, dest: Path) -> Path:
    """无损封装：只复制流，不重新编码。"""
    tmp = dest.with_suffix(dest.suffix + ".tmp.mp4")
    _run([
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", str(video),
        "-i", str(audio),
        "-c", "copy",
        "-map", "0:v:0", "-map", "1:a:0",
        "-movflags", "+faststart",
        str(tmp),
    ])
    tmp.replace(dest)
    return dest


def to_audio(audio: Path, dest: Path) -> Path:
    """把裸音频流封装成可直接播放的 m4a。"""
    tmp = dest.with_suffix(dest.suffix + ".tmp.m4a")
    _run([
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", str(audio),
        "-c", "copy",
        "-movflags", "+faststart",
        str(tmp),
    ])
    tmp.replace(dest)
    return dest
