#!/usr/bin/env python3
"""Search NetEase Cloud Music through neteasecli and print selectable results."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from typing import Any

from download_music import NeteaseCliError, run_cli


def format_duration(milliseconds: Any) -> str:
    try:
        seconds = int(milliseconds or 0) // 1000
    except (TypeError, ValueError):
        return "-"
    return f"{seconds // 60}:{seconds % 60:02d}"


def format_track(index: int, track: dict[str, Any]) -> dict[str, Any]:
    artists = track.get("artists") or []
    artist_names = [str(item.get("name", "")) for item in artists if item.get("name")]
    album = track.get("album") or {}
    return {
        "index": index,
        "id": str(track.get("id", "")),
        "name": str(track.get("name", "")),
        "artists": artist_names,
        "album": str(album.get("name", "")),
        "duration": track.get("duration", 0),
        "durationFormatted": format_duration(track.get("duration", 0)),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Search NetEase Cloud Music and show track IDs for download."
    )
    parser.add_argument("query", help="Song title, artist, or other search keywords")
    parser.add_argument("--limit", type=int, default=10, help="Number of results (default: 10)")
    parser.add_argument("--offset", type=int, default=0, help="Result offset (default: 0)")
    parser.add_argument("--profile", help="neteasecli profile name")
    parser.add_argument("--cli", default="neteasecli", help="neteasecli executable or path")
    parser.add_argument("--timeout", type=int, default=60, help="Command timeout in seconds")
    parser.add_argument("--json", action="store_true", help="Print structured JSON")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if shutil.which(args.cli) is None:
        print(f"找不到 neteasecli: {args.cli}", file=sys.stderr)
        return 2
    if args.limit < 1 or args.limit > 100:
        print("--limit 必须在 1 到 100 之间", file=sys.stderr)
        return 2
    try:
        data = run_cli(
            args.cli,
            args.profile,
            [
                "search",
                "track",
                args.query,
                "--limit",
                str(args.limit),
                "--offset",
                str(args.offset),
            ],
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
    print("\n下载时使用上面的 ID，例如：")
    print(
        "python3 download_music.py "
        f"{tracks[0]['id']} --output-dir /Volumes/Biwin/Music"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
