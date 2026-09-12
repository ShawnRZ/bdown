"""从 playurl 的响应里挑出要下载的音视频轨。"""

from __future__ import annotations

from .api import CODEC_PREFIX, Stream

AUDIO_NAMES = {
    30216: "64kbps",
    30232: "132kbps",
    30280: "192kbps",
    30250: "杜比全景声",
    30251: "Hi-Res 无损",
}


def _codec_name(codecs: str) -> str:
    """把 avc1.640033 之类的长串收敛成 avc / hevc / av1。"""
    for name, prefix in CODEC_PREFIX.items():
        if codecs.startswith(prefix):
            return name
    return codecs.split(".")[0]


def _base_url(item: dict) -> str:
    return item.get("baseUrl") or item.get("base_url") or ""


def _backups(item: dict) -> list[str]:
    return list(item.get("backupUrl") or item.get("backup_url") or [])


def _duration(data: dict) -> int:
    dash = data.get("dash") or {}
    return int(dash.get("duration") or round((data.get("timelength") or 0) / 1000))


def parse_dash(data: dict) -> tuple[list[Stream], list[Stream]]:
    """返回 (视频轨, 音频轨)；没有 dash 字段时两者都为空。"""
    dash = data.get("dash")
    if not dash:
        return [], []
    seconds = _duration(data) or 1

    videos = [
        Stream(
            url=_base_url(v),
            backups=_backups(v),
            quality=v.get("id", 0),
            codec=_codec_name(v.get("codecs", "")),
            size_hint=v.get("bandwidth", 0) * seconds // 8,
            kind="video",
            width=v.get("width", 0),
            height=v.get("height", 0),
            frame_rate=v.get("frameRate") or v.get("frame_rate") or "",
        )
        for v in dash.get("video") or []
    ]

    raw_audio = list(dash.get("audio") or [])
    # 杜比与 Hi-Res 轨挂在单独字段下，一并纳入候选
    for extra in (dash.get("dolby") or {}, dash.get("flac") or {}):
        audio = extra.get("audio")
        if isinstance(audio, dict):
            raw_audio.append(audio)
        elif isinstance(audio, list):
            raw_audio.extend(a for a in audio if a)

    audios = [
        Stream(
            url=_base_url(a),
            backups=_backups(a),
            quality=a.get("id", 0),
            codec=AUDIO_NAMES.get(a.get("id", 0), _codec_name(a.get("codecs", ""))),
            size_hint=a.get("bandwidth", 0) * seconds // 8,
            kind="audio",
        )
        for a in raw_audio
        if _base_url(a)
    ]
    return videos, audios


def parse_durl(data: dict) -> list[Stream]:
    """老接口的整段 flv/mp4，作为无 dash 时的兜底。"""
    seconds = _duration(data) or 1
    out = []
    for seg in data.get("durl") or []:
        out.append(
            Stream(
                url=seg.get("url", ""),
                backups=list(seg.get("backup_url") or []),
                quality=data.get("quality", 0),
                codec=data.get("format", "flv"),
                size_hint=seg.get("size", 0),
                kind="video",
            )
        )
    return out


def pick_video(
    videos: list[Stream], quality: int | None = None, codec: str = "avc"
) -> Stream:
    """按清晰度优先、编码次之的顺序挑一路视频轨。

    quality 为 None 时取可用的最高清晰度；指定了但拿不到时退回不超过它的最高档。
    """
    if not videos:
        raise ValueError("没有可用的视频轨")

    if quality is None:
        target = max(v.quality for v in videos)
    else:
        candidates = [v.quality for v in videos if v.quality <= quality]
        target = max(candidates) if candidates else min(v.quality for v in videos)

    same_quality = [v for v in videos if v.quality == target]
    preferred = [v for v in same_quality if v.codec == codec]
    # 指定编码缺档时用体积最小的同档视频顶上
    return min(preferred or same_quality, key=lambda v: v.size_hint or 1 << 62)


def pick_audio(audios: list[Stream], hires: bool = False) -> Stream | None:
    """默认取普通音轨里码率最高的一路；hires=True 时优先无损/杜比。"""
    if not audios:
        return None
    if not hires:
        normal = [a for a in audios if a.quality not in (30250, 30251)]
        if normal:
            return max(normal, key=lambda a: a.size_hint)
    return max(audios, key=lambda a: a.size_hint)
