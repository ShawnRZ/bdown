import pytest

from bdown import merge
from bdown.api import parse_target
from bdown.cli import build_parser, human, parse_pages, select_pages


@pytest.mark.parametrize(
    "text,expected",
    [
        ("BV1kktD69EaX", ("BV1kktD69EaX", None)),
        ("https://www.bilibili.com/video/BV1kktD69EaX/?p=2", ("BV1kktD69EaX", None)),
        ("https://b23.tv/BV1kktD69EaX", ("BV1kktD69EaX", None)),
        ("av117202141384022", (None, 117202141384022)),
        ("https://www.bilibili.com/video/av2/", (None, 2)),
    ],
)
def test_parse_target(text, expected):
    assert parse_target(text) == expected


def test_parse_target_rejects_garbage():
    with pytest.raises(ValueError):
        parse_target("这不是一个视频号")


@pytest.mark.parametrize(
    "spec,total,expected",
    [
        (None, 3, [1, 2, 3]),
        ("2", 3, [2]),
        ("1,3-5", 6, [1, 3, 4, 5]),
        ("3-1", 5, [1, 2, 3]),          # 逆序范围按升序处理
        (" 1 , 2 ,", 3, [1, 2]),        # 容忍空白与尾随逗号
        ("2,2,2", 3, [2]),              # 去重
        ("1-99", 3, [1, 2, 3]),         # 超出部分裁掉
    ],
)
def test_parse_pages(spec, total, expected):
    assert parse_pages(spec, total) == expected


@pytest.mark.parametrize("spec", ["0", "99", "abc", "1-x", "-"])
def test_parse_pages_rejects_invalid(spec):
    with pytest.raises(ValueError):
        parse_pages(spec, 3)


def test_safe_name_strips_illegal_chars():
    assert merge.safe_name('a/b\\c:d*e?f"g<h>i|j') == "a_b_c_d_e_f_g_h_i_j"
    assert merge.safe_name("  . 标题 .  ") == "标题"
    assert merge.safe_name("", fallback="BV1") == "BV1"
    assert merge.safe_name("///", fallback="BV1") == "BV1"


def test_safe_name_truncates_to_byte_budget():
    name = merge.safe_name("中" * 200)
    assert len(name.encode("utf-8")) <= 180
    assert name.startswith("中中")


def test_human_readable_sizes():
    assert human(512) == "512B"
    assert human(2048) == "2.0KiB"
    assert human(5 * 1024 * 1024) == "5.0MiB"
    assert human(3 * 1024**3) == "3.0GiB"


def test_parser_defaults():
    args = build_parser().parse_args(["BV1kktD69EaX"])
    assert args.targets == ["BV1kktD69EaX"]
    assert args.codec == "avc"
    assert args.threads == 8
    assert args.quality is None


def test_parser_rejects_unknown_quality():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["BV1", "-q", "999"])


# --- 分 P 选择的优先级 ---


def test_select_pages_defaults_to_all():
    assert select_pages(3, None, None, False) == ([1, 2, 3], None)


def test_select_pages_honours_link_page():
    indexes, note = select_pages(5, None, 3, False)
    assert indexes == [3]
    assert "P3" in note and "--all-pages" in note


def test_select_pages_stays_quiet_on_single_part_video():
    """只有一个分 P 时，链接带 p=1 是常态，不必提示。"""
    assert select_pages(1, None, 1, False) == ([1], None)


def test_explicit_pages_override_link():
    assert select_pages(5, "2,4", 3, False) == ([2, 4], None)


def test_all_pages_flag_overrides_link():
    assert select_pages(4, None, 2, True) == ([1, 2, 3, 4], None)


def test_link_page_out_of_range_falls_back_to_all():
    indexes, note = select_pages(2, None, 9, False)
    assert indexes == [1, 2]
    assert "超出范围" in note


def test_pages_and_all_pages_are_mutually_exclusive():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["BV1kktD69EaX", "-p", "1", "--all-pages"])


def test_all_pages_defaults_false():
    assert build_parser().parse_args(["BV1kktD69EaX"]).all_pages is False
