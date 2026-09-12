"""bdown 命令行入口。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from rich.console import Console
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    TextColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)
from rich.table import Table

from . import merge, streams
from .api import (
    QUALITY_NAMES,
    BilibiliClient,
    BilibiliError,
    Page,
    Stream,
    VideoInfo,
    load_cookie,
)
from .download import DownloadError, download

console = Console()
err_console = Console(stderr=True)

QUALITY_CHOICES = sorted(QUALITY_NAMES)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bdown",
        description="下载 bilibili 视频：给出 BV 号即可。",
        epilog=(
            "示例：\n"
            "  bdown BV1GJ411x7h7                 下载最高可用画质\n"
            "  bdown BV1GJ411x7h7 -q 80 -o ~/视频  指定 1080P 并放到指定目录\n"
            "  bdown BV1GJ411x7h7 -p 1,3-5        只下第 1、3~5 个分 P\n"
            "  bdown BV1GJ411x7h7 --list          只看信息，不下载\n\n"
            "高画质需要登录态：把浏览器里的 Cookie（至少含 SESSDATA）写入\n"
            "~/.config/bdown/cookie.txt，或设置环境变量 BILI_COOKIE。"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "targets", nargs="+", metavar="BV号",
        help="BV 号、av 号、视频链接或 b23.tv 短链（也可直接粘贴 App 分享的整段文字）",
    )
    parser.add_argument(
        "-o", "--output", type=Path, default=Path.cwd(), metavar="目录", help="输出目录，默认当前目录"
    )
    parser.add_argument(
        "-q", "--quality", type=int, choices=QUALITY_CHOICES, metavar="清晰度",
        help="目标清晰度 qn，默认取最高可用；可选：" + "、".join(
            f"{q}({QUALITY_NAMES[q]})" for q in QUALITY_CHOICES
        ),
    )
    parser.add_argument(
        "--codec", choices=("avc", "hevc", "av1"), default="avc",
        help="视频编码，默认 avc（兼容性最好），hevc/av1 同画质体积更小",
    )
    pages_group = parser.add_mutually_exclusive_group()
    pages_group.add_argument(
        "-p", "--pages", metavar="范围", help="分 P 选择，如 1,3-5；默认全部"
    )
    pages_group.add_argument(
        "--all-pages", action="store_true",
        help="下载全部分 P，忽略链接里的 p= 指定",
    )
    parser.add_argument("-j", "--threads", type=int, default=8, metavar="并发数", help="分块并发数，默认 8")
    parser.add_argument("--list", action="store_true", help="列出分 P 与可用清晰度后退出")
    parser.add_argument("--audio-only", action="store_true", help="只下音频，输出 m4a")
    parser.add_argument("--hires", action="store_true", help="优先选无损 / 杜比音轨（需大会员）")
    parser.add_argument("--no-merge", action="store_true", help="不合并，保留分离的音视频流")
    parser.add_argument("--keep", action="store_true", help="合并后保留中间文件")
    parser.add_argument("--cookie", metavar="COOKIE", help="直接指定 Cookie 字符串")
    parser.add_argument("--overwrite", action="store_true", help="目标文件已存在时重新下载")
    return parser


def parse_pages(spec: str | None, total: int) -> list[int]:
    """把 '1,3-5' 解析成 [1, 3, 4, 5]。"""
    if not spec:
        return list(range(1, total + 1))
    picked: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            head, _, tail = part.partition("-")
            try:
                lo, hi = int(head), int(tail)
            except ValueError:
                raise ValueError(f"无法解析分 P 范围 {part!r}") from None
            picked.update(range(min(lo, hi), max(lo, hi) + 1))
        else:
            try:
                picked.add(int(part))
            except ValueError:
                raise ValueError(f"无法解析分 P 序号 {part!r}") from None
    valid = sorted(p for p in picked if 1 <= p <= total)
    if not valid:
        raise ValueError(f"分 P 选择 {spec!r} 超出范围（共 {total} 个分 P）")
    return valid


def select_pages(
    total: int, explicit: str | None, link_page: int | None, all_pages: bool
) -> tuple[list[int], str | None]:
    """决定下载哪些分 P，返回 (序号列表, 需要告知用户的话)。

    优先级：命令行 -p > 链接里的 p= > 全部。
    """
    if explicit:
        return parse_pages(explicit, total), None

    if link_page and not all_pages:
        if link_page > total:
            # 链接过期或指向别的稿件时不要硬失败，退回下载全部
            return list(range(1, total + 1)), (
                f"[yellow]提示[/yellow] 链接里的 p={link_page} 超出范围"
                f"（共 {total} 个分 P），改为下载全部"
            )
        note = None
        if total > 1:
            note = (
                f"[dim]链接指定了 P{link_page}，本次只下这一个分 P；"
                f"要全部请加 --all-pages[/dim]"
            )
        return [link_page], note

    return list(range(1, total + 1)), None


def human(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{value:.1f}{unit}" if unit != "B" else f"{int(value)}B"
        value /= 1024
    return f"{value:.1f}GiB"


def show_info(info: VideoInfo, videos: list[Stream], audios: list[Stream]) -> None:
    console.print(f"[bold]{info.title}[/bold]  ({info.bvid})")
    console.print(f"UP 主：{info.owner}   分 P：{len(info.pages)}")

    if len(info.pages) > 1:
        table = Table(title="分 P 列表", title_justify="left", box=None, pad_edge=False)
        table.add_column("P", justify="right")
        table.add_column("标题")
        table.add_column("时长", justify="right")
        for page in info.pages:
            mins, secs = divmod(page.duration, 60)
            table.add_row(str(page.index), page.title, f"{mins}:{secs:02d}")
        console.print(table)

    table = Table(title="可用视频流（第一个分 P）", title_justify="left", box=None, pad_edge=False)
    table.add_column("qn", justify="right")
    table.add_column("清晰度")
    table.add_column("分辨率")
    table.add_column("编码")
    table.add_column("估算体积", justify="right")
    for stream in sorted(videos, key=lambda v: (-v.quality, v.codec)):
        table.add_row(
            str(stream.quality),
            QUALITY_NAMES.get(stream.quality, "?"),
            f"{stream.width}x{stream.height}@{stream.frame_rate}",
            stream.codec,
            human(stream.size_hint),
        )
    console.print(table)

    if audios:
        table = Table(title="可用音频流", title_justify="left", box=None, pad_edge=False)
        table.add_column("id", justify="right")
        table.add_column("规格")
        table.add_column("估算体积", justify="right")
        for stream in sorted(audios, key=lambda a: -a.size_hint):
            table.add_row(str(stream.quality), stream.codec, human(stream.size_hint))
        console.print(table)


def fetch_stream(
    client: BilibiliClient, progress: Progress, stream: Stream, dest: Path, threads: int, label: str
) -> Path:
    task = progress.add_task(label, total=None, start=False)

    def on_total(total: int, done: int) -> None:
        progress.update(task, total=total, completed=done)
        progress.start_task(task)

    def on_progress(delta: int) -> None:
        progress.advance(task, delta)

    try:
        return download(
            client.http,
            [stream.url, *stream.backups],
            dest,
            threads=threads,
            on_total=on_total,
            on_progress=on_progress,
        )
    finally:
        progress.remove_task(task)


def download_page(
    client: BilibiliClient,
    args: argparse.Namespace,
    info: VideoInfo,
    page: Page,
    out_dir: Path,
    multi: bool,
) -> None:
    data = client.playurl(info.bvid, page.cid, qn=args.quality or 127)
    videos, audios = streams.parse_dash(data)

    if multi:
        stem = merge.safe_name(f"P{page.index:02d} {page.title}", f"P{page.index}")
    else:
        stem = merge.safe_name(info.title, info.bvid)

    if args.audio_only:
        audio = streams.pick_audio(audios, hires=args.hires)
        if audio is None:
            raise BilibiliError("该视频没有独立音频流，无法只下音频")
        dest = out_dir / f"{stem}.m4a"
        if dest.exists() and not args.overwrite:
            console.print(f"[yellow]跳过[/yellow] {dest.name}（已存在，用 --overwrite 覆盖）")
            return
        raw = out_dir / f"{stem}.audio.m4s"
        with _progress() as progress:
            fetch_stream(client, progress, audio, raw, args.threads, f"{stem} 音频")
        merge.to_audio(raw, dest)
        if not args.keep:
            raw.unlink(missing_ok=True)
        console.print(f"[green]完成[/green] {dest}  {human(dest.stat().st_size)}", soft_wrap=True)
        return

    if not videos:
        # 没有 dash 时退回老接口的整段文件
        segments = streams.parse_durl(data)
        if not segments:
            raise BilibiliError("接口未返回可用的视频流")
        dest = out_dir / f"{stem}.{segments[0].codec}"
        if dest.exists() and not args.overwrite:
            console.print(f"[yellow]跳过[/yellow] {dest.name}（已存在）")
            return
        with _progress() as progress:
            fetch_stream(client, progress, segments[0], dest, args.threads, stem)
        console.print(f"[green]完成[/green] {dest}  {human(dest.stat().st_size)}", soft_wrap=True)
        return

    video = streams.pick_video(videos, args.quality, args.codec)
    audio = streams.pick_audio(audios, hires=args.hires)

    dest = out_dir / f"{stem}.mp4"
    if dest.exists() and not args.overwrite:
        console.print(f"[yellow]跳过[/yellow] {dest.name}（已存在，用 --overwrite 覆盖）")
        return

    summary = f"{video.label} ({human(video.size_hint)})"
    summary += f" + {audio.label} ({human(audio.size_hint)})" if audio else " (无音轨)"
    console.print(f"  → {summary}")
    if args.quality and video.quality < args.quality:
        hint = "，登录后可解锁更高画质" if not client.logged_in else ""
        console.print(
            f"  [yellow]提示[/yellow] 请求 qn={args.quality} 不可用，实际取到 "
            f"{QUALITY_NAMES.get(video.quality, video.quality)}{hint}"
        )

    video_raw = out_dir / f"{stem}.video.m4s"
    audio_raw = out_dir / f"{stem}.audio.m4s"
    with _progress() as progress:
        fetch_stream(client, progress, video, video_raw, args.threads, f"{stem} 视频")
        if audio:
            fetch_stream(client, progress, audio, audio_raw, args.threads, f"{stem} 音频")

    if audio is None:
        video_raw.replace(dest)
    elif args.no_merge:
        console.print(f"[green]完成[/green] {video_raw} + {audio_raw}（未合并）")
        return
    else:
        merge.merge(video_raw, audio_raw, dest)
        if not args.keep:
            video_raw.unlink(missing_ok=True)
            audio_raw.unlink(missing_ok=True)

    console.print(f"[green]完成[/green] {dest}  {human(dest.stat().st_size)}", soft_wrap=True)


def _progress() -> Progress:
    return Progress(
        TextColumn("[cyan]{task.description}"),
        BarColumn(),
        "[progress.percentage]{task.percentage:>5.1f}%",
        DownloadColumn(),
        TransferSpeedColumn(),
        TimeRemainingColumn(),
        console=console,
        transient=True,
    )


def handle_target(client: BilibiliClient, args: argparse.Namespace, target: str) -> None:
    def report_expand(short: str, final: str) -> None:
        console.print(f"短链 {short} → {final.split('?')[0]}", soft_wrap=True)

    resolved = client.resolve_target(target, on_expand=report_expand)
    info = client.video_info(bvid=resolved.bvid, aid=resolved.aid)

    if args.list:
        data = client.playurl(info.bvid, info.pages[0].cid, qn=args.quality or 127)
        videos, audios = streams.parse_dash(data)
        show_info(info, videos, audios)
        return

    indexes, note = select_pages(
        len(info.pages), args.pages, resolved.page, args.all_pages
    )
    multi = len(info.pages) > 1
    out_dir = args.output / merge.safe_name(info.title, info.bvid) if multi else args.output
    out_dir.mkdir(parents=True, exist_ok=True)

    console.print(f"[bold]{info.title}[/bold] — {info.owner} ({info.bvid})")
    if multi:
        console.print(f"共 {len(info.pages)} 个分 P，本次下载 {len(indexes)} 个 → {out_dir}")
    if note:
        console.print(note)

    failures = 0
    for index in indexes:
        page = info.pages[index - 1]
        if multi:
            console.print(f"[bold cyan]P{page.index}[/bold cyan] {page.title}")
        try:
            download_page(client, args, info, page, out_dir, multi)
        except (BilibiliError, DownloadError, merge.MergeError) as exc:
            failures += 1
            err_console.print(f"[red]P{page.index} 失败[/red] {exc}")
    if failures:
        raise BilibiliError(f"{failures}/{len(indexes)} 个分 P 下载失败")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if not args.list and not args.no_merge and not merge.has_ffmpeg():
        err_console.print(
            "[red]错误[/red] 未找到 ffmpeg，无法合并音视频。\n"
            "  请先安装（Debian/Ubuntu：sudo apt install ffmpeg），或加 --no-merge 保留分离文件。"
        )
        return 2

    cookie = load_cookie(args.cookie)
    exit_code = 0
    with BilibiliClient(cookie=cookie) as client:
        try:
            client.wbi_keys()  # 顺带拿到登录态
        except Exception as exc:  # noqa: BLE001 - 网络层异常种类较多
            err_console.print(f"[red]错误[/red] 无法连接 bilibili：{exc}")
            return 2
        if not client.logged_in:
            console.print("[yellow]未检测到登录态[/yellow]，可下载的清晰度将受限（通常最高 480P）")

        for target in args.targets:
            try:
                handle_target(client, args, target)
            except (ValueError, BilibiliError, DownloadError, merge.MergeError) as exc:
                err_console.print(f"[red]错误[/red] {target}：{exc}")
                exit_code = 1
            except KeyboardInterrupt:
                err_console.print("\n[yellow]已中断[/yellow]，下次运行会从断点继续")
                return 130
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
