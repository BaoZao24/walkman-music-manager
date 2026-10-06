#!/usr/bin/env python3
"""Walkman music workflow: one entry for the whole pipeline.

Subcommands:
  search    Search NetEase Cloud Music
  download  Download tracks + Walkman-ready LRC
  convert   Convert local .ncm files with ncmdump
  repair    Repair LRC files on the storage card
  album     Reorganize downloaded files into an album folder
  full      One-shot: search -> download -> album folder
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

import download_music
import process_music
import walkman_lrc_repair
from bilibili_download import build_parser as build_bilibili_parser
from download_music import (
    NeteaseCliError,
    download_one,
    ensure_volume_mounted,
    read_track_ids,
    run_cli,
)
from process_music import NcmdumpError, iter_ncm_files, process_one
from search_music import format_track


def cmd_search(args: argparse.Namespace) -> int:
    if shutil.which(args.cli) is None:
        print(f"找不到 neteasecli: {args.cli}", file=sys.stderr)
        return 2
    try:
        data = run_cli(
            args.cli,
            args.profile,
            ["search", "track", args.query, "--limit", str(args.limit), "--offset", str(args.offset)],
            args.timeout,
        )
    except (NeteaseCliError, OSError) as exc:
        print(f"搜索失败: {exc}", file=sys.stderr)
        return 1
    tracks = [
        format_track(index, track)
        for index, track in enumerate(data.get("tracks") or [], start=1)
        if isinstance(track, dict)
    ]
    result = {
        "query": args.query,
        "total": data.get("total", len(tracks)),
        "offset": data.get("offset", args.offset),
        "limit": data.get("limit", args.limit),
        "tracks": tracks,
    }
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if not tracks:
        print("没有找到结果")
        return 0
    print(f"搜索：{args.query}（共 {result['total']} 首，显示 {len(tracks)} 首）")
    for track in tracks:
        artists = "、".join(track["artists"]) or "未知艺术家"
        album = track["album"] or "未知专辑"
        print(
            f"{track['index']:>2}. ID {track['id']:<12} "
            f"{track['name']} — {artists} [{album}] ({track['durationFormatted']})"
        )
    return 0


def cmd_download(args: argparse.Namespace) -> int:
    if shutil.which(args.cli) is None and not Path(args.cli).is_file():
        print(f"找不到 neteasecli: {args.cli}", file=sys.stderr)
        return 2
    try:
        track_ids = read_track_ids(args.ids, args.input)
        args.output_dir = args.output_dir.expanduser().resolve()
        ensure_volume_mounted(args.output_dir)
        counts: dict[str, int] = defaultdict(int)
        for track_id in track_ids:
            try:
                counts[download_one(track_id, args)] += 1
            except (NeteaseCliError, OSError, subprocess.TimeoutExpired) as exc:
                counts["failed"] += 1
                print(f"{track_id}: 失败 — {exc}", file=sys.stderr)
        print("汇总: " + ", ".join(f"{key}={value}" for key, value in sorted(counts.items())))
        return 1 if counts.get("failed") else 0
    except (ValueError, OSError) as exc:
        print(f"参数错误: {exc}", file=sys.stderr)
        return 2


def cmd_convert(args: argparse.Namespace) -> int:
    args.source = args.source.expanduser().resolve()
    args.source_root = args.source if args.source.is_dir() else args.source.parent
    args.output_dir = args.output_dir.expanduser().resolve()
    try:
        ensure_volume_mounted(args.output_dir)
    except OSError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if not args.source.exists():
        print(f"找不到输入路径: {args.source}", file=sys.stderr)
        return 2
    if not args.dry_run and shutil.which(args.ncmdump) is None and not Path(args.ncmdump).is_file():
        print(f"找不到 ncmdump: {args.ncmdump}", file=sys.stderr)
        return 2
    tracks = iter_ncm_files(args.source)
    if not tracks:
        print("没有找到 .ncm 文件", file=sys.stderr)
        return 2
    counts: dict[str, int] = defaultdict(int)
    for track in tracks:
        try:
            counts[process_one(track, args)] += 1
        except (NcmdumpError, OSError, subprocess.TimeoutExpired) as exc:
            counts["failed"] += 1
            print(f"{track}: 失败 — {exc}", file=sys.stderr)
    print("汇总: " + ", ".join(f"{key}={value}" for key, value in sorted(counts.items())))
    return 1 if counts.get("failed") else 0


def cmd_repair(args: argparse.Namespace) -> int:
    try:
        return walkman_lrc_repair.run_repair(args.root.resolve(), args.apply, args.backup_root)
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 2


AUDIO_SUFFIXES = {".aac", ".flac", ".m4a", ".mp3", ".ogg", ".opus", ".wav"}


def cmd_album(args: argparse.Namespace) -> int:
    """Move downloaded audio/LRC files into a single album folder.

    Only files directly inside ``source`` are moved (no recursion).  Moving
    recursively over an entire music library is what caused the 2026-08-13
    accident (every file under Music was shifted into 巫师/测试 without any
    backup).  Use --recursive only when you truly mean to flatten a tree,
    and pair it with --confirm.
    """
    source = args.source.expanduser().resolve()
    if not source.is_dir():
        print(f"不是目录: {source}", file=sys.stderr)
        return 2
    output_dir = args.output_dir.expanduser().resolve()
    album_dir = output_dir / args.album_name
    try:
        ensure_volume_mounted(output_dir)
    except OSError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    if args.recursive and not args.confirm:
        print(
            "--recursive 会把整个目录树中的文件移动到专辑目录，属于破坏性操作。"
            "如确认无误，请同时加上 --confirm。",
            file=sys.stderr,
        )
        return 2

    candidates = list(source.rglob("*")) if args.recursive else list(source.iterdir())
    files = [
        path
        for path in candidates
        if path.is_file()
        and not path.name.startswith("._")
        and (path.suffix.lower() in AUDIO_SUFFIXES or path.suffix.lower() == ".lrc")
        and path.parent != album_dir
        and album_dir not in path.parents
    ]

    if args.dry_run:
        print(f"预览：将把 {len(files)} 个文件移动到 {album_dir}")
        for path in files[: args.preview]:
            print(f"  将移动: {path.name}")
        if len(files) > args.preview:
            print(f"  ... 及另外 {len(files) - args.preview} 个文件")
        return 0

    album_dir.mkdir(parents=True, exist_ok=True)
    moved: list[Path] = []
    for path in files:
        target = album_dir / path.name
        if target.exists() and not args.overwrite:
            print(f"已存在，跳过: {target.name}")
            continue
        shutil.move(str(path), str(target))
        moved.append(target)
        print(f"已移动: {path.name} -> {album_dir}")
    print(f"汇总: moved={len(moved)}")
    return 0


def cmd_full(args: argparse.Namespace) -> int:
    """Search, download top matches, then organize into an album folder."""
    search_args = argparse.Namespace(
        cli=args.cli, profile=args.profile, timeout=args.timeout,
        query=args.query, limit=args.limit, offset=0, json=False,
    )
    search_result = cmd_search(search_args)
    if search_result != 0:
        return search_result

    try:
        data = run_cli(
            args.cli, args.profile,
            ["search", "track", args.query, "--limit", str(args.limit), "--offset", "0"],
            args.timeout,
        )
    except (NeteaseCliError, OSError) as exc:
        print(f"搜索失败: {exc}", file=sys.stderr)
        return 1
    tracks = [format_track(i, t) for i, t in enumerate(data.get("tracks") or [], start=1) if isinstance(t, dict)]
    if not tracks:
        print("没有找到结果", file=sys.stderr)
        return 1
    ids = [t["id"] for t in tracks]
    print(f"\n将下载 {len(ids)} 首并整理到专辑目录: {args.album_name}")

    download_args = argparse.Namespace(
        ids=ids, input=None, output_dir=args.output_dir,
        quality=args.quality, lyrics=args.lyrics, profile=args.profile, cli=args.cli,
        flat=False, overwrite=False, dry_run=args.dry_run,
        by_album=args.by_album,
        no_cover=args.no_cover,
        translate_japanese=args.translate_japanese,
        translation_model=args.translation_model,
        translation_base_url=args.translation_base_url,
        translation_timeout=args.translation_timeout,
        timeout=args.timeout,
    )
    result = cmd_download(download_args)
    if result != 0:
        return result

    album_args = argparse.Namespace(
        source=args.output_dir, output_dir=args.output_dir,
        album_name=args.album_name, overwrite=False,
        dry_run=args.dry_run, recursive=False, confirm=True, preview=5,
    )
    return cmd_album(album_args)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="walkman", description="Walkman music workflow: search, download, convert, repair."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_search = sub.add_parser("search", help="搜索网易云音乐")
    p_search.add_argument("query", help="歌曲名、歌手或关键词")
    p_search.add_argument("--limit", type=int, default=10)
    p_search.add_argument("--offset", type=int, default=0)
    p_search.add_argument("--profile")
    p_search.add_argument("--cli", default="neteasecli")
    p_search.add_argument("--timeout", type=int, default=60)
    p_search.add_argument("--json", action="store_true")
    p_search.set_defaults(func=cmd_search)

    p_download = sub.add_parser("download", help="下载歌曲与歌词")
    p_download.add_argument("ids", nargs="*")
    p_download.add_argument("--input", type=Path)
    p_download.add_argument("--output-dir", type=Path, required=True)
    p_download.add_argument("--quality", choices=["standard", "higher", "exhigh", "lossless", "hires"], default="exhigh")
    p_download.add_argument("--lyrics", choices=["original", "translated", "bilingual"], default="translated")
    p_download.add_argument("--profile")
    p_download.add_argument("--cli", default="neteasecli")
    p_download.add_argument("--flat", action="store_true")
    p_download.add_argument("--by-album", action="store_true", help="按专辑名建子目录（同一专辑放一起）")
    p_download.add_argument("--overwrite", action="store_true")
    p_download.add_argument("--dry-run", action="store_true")
    p_download.add_argument("--no-cover", action="store_true", help="不嵌入专辑封面")
    p_download.add_argument("--translate-japanese", action="store_true")
    p_download.add_argument("--translation-model", default="deepseek-v4-flash")
    p_download.add_argument("--translation-base-url", default=None, help="OpenAI 兼容 API 端点（默认读 OPENAI_BASE_URL 环境变量）")
    p_download.add_argument("--translation-timeout", type=int, default=120)
    p_download.add_argument("--timeout", type=int, default=120)
    p_download.set_defaults(func=cmd_download)

    p_convert = sub.add_parser("convert", help="转换 .ncm 文件")
    p_convert.add_argument("source", type=Path)
    p_convert.add_argument("--output-dir", type=Path, required=True)
    p_convert.add_argument("--ncmdump", default="ncmdump")
    p_convert.add_argument("--flat", action="store_true")
    p_convert.add_argument("--overwrite", action="store_true")
    p_convert.add_argument("--dry-run", action="store_true")
    p_convert.add_argument("--no-cover", action="store_true", help="不嵌入专辑封面")
    p_convert.add_argument("--translate-japanese", action="store_true")
    p_convert.add_argument("--translation-model", default="deepseek-v4-flash")
    p_convert.add_argument("--translation-base-url", default=None, help="OpenAI 兼容 API 端点（默认读 OPENAI_BASE_URL 环境变量）")
    p_convert.add_argument("--translation-timeout", type=int, default=120)
    p_convert.add_argument("--timeout", type=int, default=120)
    p_convert.set_defaults(func=cmd_convert)

    p_repair = sub.add_parser("repair", help="修复存储卡上的 LRC")
    p_repair.add_argument("root", type=Path)
    p_repair.add_argument("--apply", action="store_true")
    p_repair.add_argument("--backup-root", type=Path)
    p_repair.set_defaults(func=cmd_repair)

    p_album = sub.add_parser("album", help="整理为单张专辑目录（只移动源目录顶层文件）")
    p_album.add_argument("source", type=Path, help="源目录（通常是刚下载文件的输出目录）")
    p_album.add_argument("--output-dir", type=Path, required=True)
    p_album.add_argument("--album-name", required=True, help="例如 巫师/The Witcher 3 - Hearts of Stone")
    p_album.add_argument("--overwrite", action="store_true")
    p_album.add_argument("--dry-run", action="store_true", help="只显示将要移动的文件，不实际移动")
    p_album.add_argument("--recursive", action="store_true", help="危险：递归移动整个目录树（需配合 --confirm）")
    p_album.add_argument("--confirm", action="store_true", help="确认递归移动（破坏性操作）")
    p_album.add_argument("--preview", type=int, default=5, help="dry-run 时显示的文件数量")
    p_album.set_defaults(func=cmd_album)

    p_full = sub.add_parser("full", help="一键：搜索并下载，然后整理为专辑目录")
    p_full.add_argument("query", help="搜索关键词")
    p_full.add_argument("--output-dir", type=Path, required=True)
    p_full.add_argument("--album-name", required=True)
    p_full.add_argument("--limit", type=int, default=1, help="下载前 N 个搜索结果")
    p_full.add_argument("--by-album", action="store_true", help="按专辑名建子目录（同一专辑放一起）")
    p_full.add_argument("--quality", choices=["standard", "higher", "exhigh", "lossless", "hires"], default="exhigh")
    p_full.add_argument("--lyrics", choices=["original", "translated", "bilingual"], default="translated")
    p_full.add_argument("--profile")
    p_full.add_argument("--cli", default="neteasecli")
    p_full.add_argument("--dry-run", action="store_true")
    p_full.add_argument("--no-cover", action="store_true", help="不嵌入专辑封面")
    p_full.add_argument("--translate-japanese", action="store_true")
    p_full.add_argument("--translation-model", default="deepseek-v4-flash")
    p_full.add_argument("--translation-base-url", default=None, help="OpenAI 兼容 API 端点（默认读 OPENAI_BASE_URL 环境变量）")
    p_full.add_argument("--translation-timeout", type=int, default=120)
    p_full.add_argument("--timeout", type=int, default=120)
    p_full.set_defaults(func=cmd_full)

    build_bilibili_parser(sub)

    return parser


def main() -> int:
    args = build_parser().parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())