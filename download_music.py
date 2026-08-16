#!/usr/bin/env python3
"""Download authorized NetEase Cloud Music tracks and Walkman-ready lyrics.

This wrapper delegates authentication, metadata lookup, streaming and download
to the official ``neteasecli`` command. It only handles local orchestration:
safe filenames, idempotent output, LRC selection/cleanup, and atomic writes.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from collections import defaultdict, deque
from pathlib import Path
from typing import Any, Iterable

from lrc_translate import TranslationError, translate_lrc
from walkman_lrc_repair import transform, write_atomic


TRACK_ID_RE = re.compile(r"^\d+$")
LRC_LINE_RE = re.compile(r"^(?P<tags>(?:\[\d+:\d+(?:[.:]\d+)?\])+)(?P<text>.*)$")
INVALID_FILENAME_RE = re.compile(r'[/:\\\x00-\x1f]')


class NeteaseCliError(RuntimeError):
    pass


def safe_filename(name: str) -> str:
    cleaned = INVALID_FILENAME_RE.sub("_", name)
    cleaned = re.sub(r"\s+", " ", cleaned).strip().rstrip(".")
    return cleaned[:180] or "untitled"


def ensure_biwin_mounted(path: Path) -> None:
    """Refuse writes below /Volumes/Biwin when the card is not mounted."""
    volume = Path("/Volumes/Biwin")
    try:
        path.resolve().relative_to(volume)
    except ValueError:
        return
    if not volume.is_mount():
        raise OSError("/Volumes/Biwin is not mounted; refusing to write to a local fallback directory")


def read_track_ids(inline_ids: Iterable[str], input_path: Path | None) -> list[str]:
    values = list(inline_ids)
    if input_path is not None:
        values.extend(input_path.read_text(encoding="utf-8").splitlines())

    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        value = value.strip()
        if not value or value.startswith("#"):
            continue
        if not TRACK_ID_RE.fullmatch(value):
            raise ValueError(f"invalid NetEase track ID: {value!r}")
        if value not in seen:
            seen.add(value)
            result.append(value)
    if not result:
        raise ValueError("provide at least one track ID or --input file")
    return result


def run_cli(
    cli: str,
    profile: str | None,
    args: list[str],
    timeout: int,
) -> dict[str, Any]:
    command = [cli, "--json"]
    if profile:
        command.extend(["--profile", profile])
    command.extend(args)
    process = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    stdout = process.stdout.strip()
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        detail = process.stderr.strip() or stdout or "no output"
        raise NeteaseCliError(f"neteasecli returned invalid JSON: {detail}") from exc

    if process.returncode != 0 or not payload.get("success", False):
        error = payload.get("error") or {}
        message = error.get("message") if isinstance(error, dict) else str(error)
        raise NeteaseCliError(message or process.stderr.strip() or "neteasecli failed")

    data = payload.get("data")
    if not isinstance(data, dict):
        raise NeteaseCliError("neteasecli returned no data")
    return data


def merge_bilingual_lyrics(original: str, translated: str) -> str:
    """Insert translated lines after matching original timestamped lines."""
    translations: dict[str, deque[str]] = defaultdict(deque)
    for line in translated.splitlines():
        match = LRC_LINE_RE.match(line)
        if match and match.group("text").strip():
            translations[match.group("tags")].append(match.group("text"))

    merged: list[str] = []
    for line in original.splitlines():
        merged.append(line)
        match = LRC_LINE_RE.match(line)
        if not match:
            continue
        candidates = translations.get(match.group("tags"))
        if candidates:
            merged.append(match.group("tags") + candidates.popleft())

    return "\n".join(merged) + ("\n" if merged else "")


def select_lyrics(data: dict[str, Any], mode: str) -> str:
    original = data.get("lrc") or ""
    translated = data.get("tlyric") or ""
    if mode == "original":
        return original
    if mode == "translated":
        return translated or original
    if mode == "bilingual":
        return merge_bilingual_lyrics(original, translated) if translated else original
    raise ValueError(f"unknown lyrics mode: {mode}")


def artist_label(detail: dict[str, Any]) -> str:
    artists = detail.get("artists") or []
    names = [str(item.get("name", "")).strip() for item in artists if item.get("name")]
    return "、".join(names) or "未知艺术家"


def track_stem(detail: dict[str, Any]) -> str:
    title = str(detail.get("name") or "未命名歌曲").strip()
    return safe_filename(f"{title} - {artist_label(detail)}")


def stream_extension(url: str) -> str:
    return ".flac" if ".flac" in url.lower() else ".mp3"


def fetch_extension(
    cli: str,
    profile: str | None,
    track_id: str,
    quality: str,
    timeout: int,
) -> str:
    data = run_cli(cli, profile, ["track", "url", track_id, "--quality", quality], timeout)
    return stream_extension(str(data.get("url") or ""))


def download_audio(
    cli: str,
    profile: str | None,
    track_id: str,
    quality: str,
    target: Path,
    timeout: int,
) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        prefix=f".{target.stem}.", suffix=".part", dir=target.parent, delete=False
    ) as handle:
        temporary = Path(handle.name)

    try:
        run_cli(
            cli,
            profile,
            ["track", "download", track_id, "--quality", quality, "--output", str(temporary)],
            timeout,
        )
        if not temporary.is_file() or temporary.stat().st_size == 0:
            raise NeteaseCliError("neteasecli reported success but produced no audio file")
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)


def write_walkman_lyrics(
    path: Path,
    text: str,
    *,
    translate_japanese: bool = False,
    translation_model: str = "deepseek-v4-flash",
    translation_base_url: str | None = None,
    translation_timeout: int = 120,
) -> str:
    if translate_japanese:
        text = translate_lrc(
            text,
            model=translation_model,
            base_url=translation_base_url,
            timeout=translation_timeout,
        )
    repaired, result = transform(text.encode("utf-8"), path)
    if repaired is None:
        return result.error
    write_atomic(path, repaired)
    return ""


def download_one(
    track_id: str,
    args: argparse.Namespace,
) -> str:
    detail = run_cli(args.cli, args.profile, ["track", "detail", track_id], args.timeout)
    stem = track_stem(detail)
    artist_dir = args.output_dir if args.flat else args.output_dir / safe_filename(artist_label(detail))
    extension = fetch_extension(args.cli, args.profile, track_id, args.quality, args.timeout)
    audio_path = artist_dir / f"{stem}{extension}"
    lyric_path = artist_dir / f"{stem}.lrc"

    print(f"{track_id}: {detail.get('name', '未命名歌曲')} — {artist_label(detail)}")
    if args.dry_run:
        print(f"  预览音频: {audio_path}")
        print(f"  预览歌词: {lyric_path}")
        return "planned"

    if audio_path.exists() and not args.overwrite:
        print(f"  音频已存在，跳过: {audio_path}")
    else:
        download_audio(
            args.cli,
            args.profile,
            track_id,
            args.quality,
            audio_path,
            args.timeout,
        )
        print(f"  已下载: {audio_path}")

    if lyric_path.exists() and not args.overwrite:
        print(f"  歌词已存在，跳过: {lyric_path}")
        return "skipped"

    lyric_data = run_cli(args.cli, args.profile, ["track", "lyric", track_id], args.timeout)
    lyric_text = select_lyrics(lyric_data, args.lyrics)
    if not lyric_text.strip():
        print("  没有可用歌词")
        return "audio-only"

    error = write_walkman_lyrics(
        lyric_path,
        lyric_text,
        translate_japanese=args.translate_japanese,
        translation_model=args.translation_model,
        translation_base_url=getattr(args, "translation_base_url", None),
        translation_timeout=args.translation_timeout,
    )
    if error:
        print(f"  歌词跳过: {error}")
        return "audio-only"
    print(f"  已写入 Walkman 歌词: {lyric_path}")
    return "downloaded"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Use neteasecli to download authorized tracks and Walkman-ready LRC files."
    )
    parser.add_argument("ids", nargs="*", help="NetEase track IDs")
    parser.add_argument("--input", type=Path, help="Text file containing one track ID per line")
    parser.add_argument(
        "--output-dir",
        type=Path,
        required=True,
        help="Music output directory, for example /Volumes/Biwin/Music",
    )
    parser.add_argument("--quality", choices=["standard", "higher", "exhigh", "lossless", "hires"], default="exhigh")
    parser.add_argument("--lyrics", choices=["original", "translated", "bilingual"], default="translated")
    parser.add_argument("--profile", help="neteasecli profile name")
    parser.add_argument("--cli", default="neteasecli", help="neteasecli executable or path")
    parser.add_argument("--flat", action="store_true", help="Do not create an artist subfolder")
    parser.add_argument("--overwrite", action="store_true", help="Replace existing audio and LRC files")
    parser.add_argument("--dry-run", action="store_true", help="Show planned paths without downloading")
    parser.add_argument(
        "--translate-japanese",
        action="store_true",
        help="Translate Japanese lyric lines with GPT when NetEase has no suitable translation",
    )
    parser.add_argument("--translation-model", default="deepseek-v4-flash")
    parser.add_argument("--translation-base-url", default=None, help="OpenAI 兼容 API 端点（默认读 OPENAI_BASE_URL 环境变量）")
    parser.add_argument("--translation-timeout", type=int, default=120)
    parser.add_argument("--timeout", type=int, default=120, help="Per-command timeout in seconds")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if shutil.which(args.cli) is None and not Path(args.cli).is_file():
        print(f"找不到 neteasecli: {args.cli}", file=sys.stderr)
        return 2
    try:
        track_ids = read_track_ids(args.ids, args.input)
        args.output_dir = args.output_dir.expanduser().resolve()
        ensure_biwin_mounted(args.output_dir)
        counts: dict[str, int] = defaultdict(int)
        for track_id in track_ids:
            try:
                counts[download_one(track_id, args)] += 1
            except (NeteaseCliError, TranslationError, OSError, subprocess.TimeoutExpired) as exc:
                counts["failed"] += 1
                print(f"{track_id}: 失败 — {exc}", file=sys.stderr)
        print(
            "汇总: "
            + ", ".join(f"{key}={value}" for key, value in sorted(counts.items()))
        )
        return 1 if counts.get("failed") else 0
    except (ValueError, OSError) as exc:
        print(f"参数错误: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
