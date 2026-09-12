import pytest

from bdown import streams

# 按真实 playurl 响应的形状构造，只保留与挑流有关的字段
SAMPLE = {
    "timelength": 100_000,
    "dash": {
        "duration": 100,
        "video": [
            {"id": 80, "codecs": "avc1.640033", "bandwidth": 1_600_000,
             "width": 1920, "height": 1080, "frameRate": "24",
             "baseUrl": "https://cdn/v-avc-1080", "backupUrl": ["https://bk/v"]},
            {"id": 80, "codecs": "hvc1.1.6.L150.90", "bandwidth": 800_000,
             "width": 1920, "height": 1080, "frameRate": "24",
             "baseUrl": "https://cdn/v-hevc-1080"},
            {"id": 80, "codecs": "av01.0.08M.08", "bandwidth": 600_000,
             "width": 1920, "height": 1080, "frameRate": "24",
             "baseUrl": "https://cdn/v-av1-1080"},
            {"id": 32, "codecs": "avc1.64001F", "bandwidth": 400_000,
             "width": 852, "height": 480, "frameRate": "24",
             "base_url": "https://cdn/v-avc-480"},
        ],
        "audio": [
            {"id": 30216, "codecs": "mp4a.40.2", "bandwidth": 64_000,
             "baseUrl": "https://cdn/a-64"},
            {"id": 30280, "codecs": "mp4a.40.2", "bandwidth": 192_000,
             "baseUrl": "https://cdn/a-192"},
        ],
        "flac": {"display": True, "audio": {"id": 30251, "codecs": "fLaC",
                 "bandwidth": 1_000_000, "baseUrl": "https://cdn/a-flac"}},
        "dolby": {"type": 2, "audio": [{"id": 30250, "codecs": "ec-3",
                  "bandwidth": 500_000, "baseUrl": "https://cdn/a-dolby"}]},
    },
}


def test_parse_dash_reads_both_field_styles():
    videos, audios = streams.parse_dash(SAMPLE)
    assert len(videos) == 4
    # base_url（下划线）写法也要能取到地址
    assert any(v.url == "https://cdn/v-avc-480" for v in videos)
    assert {v.codec for v in videos} == {"avc", "hevc", "av1"}
    assert videos[0].backups == ["https://bk/v"]


def test_parse_dash_includes_dolby_and_flac():
    _videos, audios = streams.parse_dash(SAMPLE)
    ids = {a.quality for a in audios}
    assert ids == {30216, 30280, 30250, 30251}
    assert next(a for a in audios if a.quality == 30251).codec == "Hi-Res 无损"


def test_parse_dash_without_dash_section():
    assert streams.parse_dash({"durl": [{"url": "x"}]}) == ([], [])


def test_size_hint_from_bandwidth():
    videos, _ = streams.parse_dash(SAMPLE)
    avc = next(v for v in videos if v.codec == "avc" and v.quality == 80)
    assert avc.size_hint == 1_600_000 * 100 // 8


def test_pick_video_prefers_highest_quality():
    videos, _ = streams.parse_dash(SAMPLE)
    assert streams.pick_video(videos).quality == 80


def test_pick_video_honours_codec():
    videos, _ = streams.parse_dash(SAMPLE)
    assert streams.pick_video(videos, codec="hevc").url == "https://cdn/v-hevc-1080"
    assert streams.pick_video(videos, codec="av1").url == "https://cdn/v-av1-1080"


def test_pick_video_falls_back_to_smallest_when_codec_missing():
    videos, _ = streams.parse_dash(SAMPLE)
    # 480P 只有 avc 一路，要求 av1 时应回落到该档现有的流
    picked = streams.pick_video(videos, quality=32, codec="av1")
    assert picked.quality == 32 and picked.codec == "avc"


def test_pick_video_steps_down_to_available_quality():
    videos, _ = streams.parse_dash(SAMPLE)
    # 请求 4K，实际最高 1080P
    assert streams.pick_video(videos, quality=120).quality == 80
    # 请求 720P，可用的只有 480P 和 1080P，取不超过目标的最高档
    assert streams.pick_video(videos, quality=64).quality == 32


def test_pick_video_returns_lowest_when_target_below_all():
    videos, _ = streams.parse_dash(SAMPLE)
    assert streams.pick_video(videos, quality=6).quality == 32


def test_pick_video_on_empty_list():
    with pytest.raises(ValueError):
        streams.pick_video([])


def test_pick_audio_skips_hires_by_default():
    _videos, audios = streams.parse_dash(SAMPLE)
    assert streams.pick_audio(audios).quality == 30280


def test_pick_audio_hires_opt_in():
    _videos, audios = streams.parse_dash(SAMPLE)
    assert streams.pick_audio(audios, hires=True).quality == 30251


def test_pick_audio_without_candidates():
    assert streams.pick_audio([]) is None


def test_parse_durl_fallback():
    data = {"timelength": 60_000, "quality": 32, "format": "flv",
            "durl": [{"url": "https://cdn/seg1", "size": 123,
                      "backup_url": ["https://bk/seg1"]}]}
    segments = streams.parse_durl(data)
    assert len(segments) == 1
    assert segments[0].url == "https://cdn/seg1"
    assert segments[0].backups == ["https://bk/seg1"]
    assert segments[0].codec == "flv"
