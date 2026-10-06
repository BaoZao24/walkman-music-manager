#!/usr/bin/env python3
"""Convert local NCM files and place Walkman-ready audio and lyrics."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

from download_music import ensure_volume_mounted, safe_filename, write_walkman_lyrics
from lrc_translate import TranslationError, translate_lrc


AUDIO_SUFFIXES = {".aac", ".flac", ".m4a", ".mp3", ".ogg", ".opus", ".wav"}


class NcmdumpError(RuntimeError):
    pass


def iter_ncm_files(root: Path) -> list[Path]:
    if root.is_file():
        return [root] if root.suffix.lower() == ".ncm" else []
    return sorted(
        path
        for path in root.rglob("*")
        if path.suffix.lower() == ".ncm"
        if path.is_file() and not path.name.startswith("._")
    )


def convert_ncm(path: Path, ncmdump: str, timeout: int) -> tuple[Path, str]:
    """Convert one NCM into a temporary audio file and return its path."""
    with tempfile.TemporaryDirectory(prefix="walkman-ncmdump-") as directory:
        output_dir = Path(directory)
        process = subprocess.run(
            [ncmdump, "--output", str(output_dir), str(path)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        if process.returncode != 0:
            detail = process.stderr.strip() or process.stdout.strip() or "ncmdump failed"
            raise NcmdumpError(detail)
        outputs = sorted(
            item for item in output_dir.iterdir() if item.is_file() and item.suffix.lower() in AUDIO_SUFFIXES
        )
        if len(outputs) != 1:
            raise NcmdumpError(f"expected one converted audio file, found {len(outputs)}")
        # The temporary directory cannot be returned after the context closes.
        descriptor, retained_name = tempfile.mkstemp(
            prefix="walkman-converted-", suffix=outputs[0].suffix
        )
        os.close(descriptor)
        retained = Path(retained_name)
        shutil.copy2(outputs[0], retained)
        return retained, outputs[0].name


def matching_lrc(path: Path) -> Path | None:
    candidate = path.with_suffix(".lrc")
    if candidate.is_file():
        return candidate
    for sibling in path.parent.iterdir():
        if sibling.is_file() and sibling.suffix.lower() == ".lrc" and sibling.stem.casefold() == path.stem.casefold():
            return sibling
    return None


def process_one(track: Path, args: argparse.Namespace) -> str:
    artist = "未知艺术家" if track.parent == args.source_root else track.parent.name
    artist_dir = args.output_dir if args.flat else args.output_dir / safe_filename(artist)
    if args.dry_run:
        print(f"预览: {track} -> {artist_dir / safe_filename(track.stem + '.mp3')}")
        return "planned"

    converted: Path | None = None
    try:
        converted, converted_name = convert_ncm(track, args.ncmdump, args.timeout)
        converted_path = Path(converted_name)
        target_audio = artist_dir / safe_filename(
            converted_path.stem + converted_path.suffix.lower()
        )
        artist_dir.mkdir(parents=True, exist_ok=True)
        if target_audio.exists() and not args.overwrite:
            print(f"音频已存在，跳过: {target_audio}")
        else:
            shutil.move(str(converted), str(target_audio))
            converted = None
            print(f"已转换: {target_audio}")

        lyric = matching_lrc(track)
        if lyric is None:
            return "audio-only"
        lyric_text = lyric.read_text(encoding="utf-8", errors="replace")
        if args.translate_japanese:
            lyric_text = translate_lrc(
                lyric_text,
                model=args.translation_model,
                base_url=getattr(args, "translation_base_url", None),
                timeout=args.translation_timeout,
            )
        target_lrc = artist_dir / f"{target_audio.stem}.lrc"
        if target_lrc.exists() and not args.overwrite:
            print(f"歌词已存在，跳过: {target_lrc}")
            return "skipped"
        error = write_walkman_lyrics(target_lrc, lyric_text)
        if error:
            print(f"歌词跳过: {error}")
            return "audio-only"
        print(f"已写入歌词: {target_lrc}")
        return "processed"
    finally:
        if converted is not None:
            converted.unlink(missing_ok=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Convert local .ncm files with ncmdump and organize Walkman-ready output."
    )
    parser.add_argument("source", type=Path, help="A .ncm file or directory containing .ncm files")
    parser.add_argument("--output-dir", type=Path, required=True, help="Final music directory")
    parser.add_argument("--ncmdump", default="ncmdump", help="ncmdump executable or path")
    parser.add_argument("--flat", action="store_true", help="Do not create artist subfolders")
    parser.add_argument("--overwrite", action="store_true", help="Replace existing audio and LRC files")
    parser.add_argument("--dry-run", action="store_true", help="Show planned files without converting")
    parser.add_argument("--translate-japanese", action="store_true", help="Translate Japanese LRC lines with GPT")
    parser.add_argument("--translation-model", default="deepseek-v4-flash")
    parser.add_argument("--translation-base-url", default=None, help="OpenAI 兼容 API 端点（默认读 OPENAI_BASE_URL 环境变量）")
    parser.add_argument("--translation-timeout", type=int, default=120)
    parser.add_argument("--timeout", type=int, default=120, help="ncmdump timeout in seconds")
    return parser


def main() -> int:
    args = build_parser().parse_args()
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
        except (NcmdumpError, OSError, TranslationError, subprocess.TimeoutExpired) as exc:
            counts["failed"] += 1
            print(f"{track}: 失败 — {exc}", file=sys.stderr)
    print("汇总: " + ", ".join(f"{key}={value}" for key, value in sorted(counts.items())))
    return 1 if counts.get("failed") else 0


if __name__ == "__main__":
    raise SystemExit(main())
