#!/usr/bin/env python3
"""Make LRC files more compatible with Sony Walkman lyric parsing.

The program is intentionally conservative:

* It only considers files ending in ``.lrc``.
* AppleDouble sidecars (``._*.lrc``) and known system directories are ignored.
* It keeps only lines beginning with one or more standard LRC timestamps.
* It removes NetEase JSON lyric metadata and unsupported metadata tags.
* It converts UTF-8 and common GB18030 source text to UTF-8.
* In apply mode, it copies each changed original into a mirrored backup tree
  before replacing the file atomically.

The default mode is a dry run. Use ``--apply`` only after reviewing the
reported counts.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path


TIMED_LINE_RE = re.compile(
    r"^(?P<tags>(?:\[\d+:\d+(?:[.:]\d+)?\])+)(?P<text>.*)$"
)
TIMESTAMP_RE = re.compile(
    r"\[(?P<minute>\d+):(?P<second>\d+)(?:(?P<sep>[.:])(?P<fraction>\d+))?\]"
)
SKIP_DIR_NAMES = {
    ".Spotlight-V100",
    ".fseventsd",
    ".Trashes",
    "System Volume Information",
}


@dataclass
class FileResult:
    path: str
    action: str
    encoding: str = ""
    json_lines_removed: int = 0
    metadata_lines_removed: int = 0
    timed_lines_kept: int = 0
    timestamps_normalized: int = 0
    original_sha256: str = ""
    repaired_sha256: str = ""
    error: str = ""


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def decode_source(data: bytes) -> tuple[str, str]:
    """Decode UTF-8 first, then the GB18030 family used by old LRCs."""
    try:
        return data.decode("utf-8-sig"), "UTF-8"
    except UnicodeDecodeError:
        return data.decode("gb18030"), "GB18030"


def is_netease_json_metadata(line: str) -> bool:
    candidate = line.strip()
    if not candidate.startswith("{"):
        return False
    try:
        obj = json.loads(candidate)
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    return isinstance(obj, dict) and "t" in obj and "c" in obj


TIMED_METADATA_RE = re.compile(
    r"^(?P<tags>(?:\[\d+:\d+(?:[.:]\d+)?\])+)\s*"
    r"(?P<text>(?:作詞|作词|作曲|編曲|编曲|歌詞|歌词|翻訳|翻译|唄|歌|"
    r"タイトル|调教|制作|混音|Mastering|lyrics|lyricist|composer|"
    r"arranged|produced|producer|album|artist|title|"
    r"[\-–—:：\s]*$))",
    re.I,
)


def is_timed_metadata(line: str) -> bool:
    return bool(TIMED_METADATA_RE.match(line))


def normalize_timestamp(match: re.Match[str]) -> str:
    minute = int(match.group("minute"))
    second = int(match.group("second"))
    fraction = match.group("fraction") or ""

    # Walkman lyric timestamps use hundredths of a second. A one-digit
    # fraction is tenths; longer fractions are truncated, never rounded up.
    if not fraction:
        hundredths = "00"
    elif len(fraction) == 1:
        hundredths = fraction + "0"
    else:
        hundredths = fraction[:2]

    return f"[{minute:02d}:{second:02d}.{hundredths}]"


def normalize_line(line: str) -> tuple[str, bool]:
    line = line.lstrip("\ufeff").lstrip()
    match = TIMED_LINE_RE.match(line)
    if not match:
        return "", False

    original_tags = match.group("tags")
    normalized_tags = TIMESTAMP_RE.sub(normalize_timestamp, original_tags)
    text = match.group("text").rstrip()
    normalized = normalized_tags + text
    return normalized, normalized != line


def transform(data: bytes, path: Path) -> tuple[bytes | None, FileResult]:
    result = FileResult(path=str(path), action="unchanged")
    try:
        source, encoding = decode_source(data)
    except UnicodeDecodeError as exc:
        result.action = "skipped"
        result.error = f"cannot decode as UTF-8 or GB18030: {exc}"
        return None, result

    result.encoding = encoding
    output_lines: list[str] = []
    had_timed_line = False

    for raw_line in source.splitlines():
        if is_netease_json_metadata(raw_line):
            result.json_lines_removed += 1
            continue
        if is_timed_metadata(raw_line):
            result.metadata_lines_removed += 1
            continue

        normalized, timestamp_changed = normalize_line(raw_line)
        if not normalized:
            if raw_line.strip():
                result.metadata_lines_removed += 1
            continue

        had_timed_line = True
        output_lines.append(normalized)
        result.timed_lines_kept += 1
        if timestamp_changed:
            result.timestamps_normalized += 1

    if not had_timed_line:
        result.action = "skipped"
        result.error = "no timestamped lyric lines found"
        return None, result

    repaired = ("\n".join(output_lines) + "\n").encode("utf-8")
    result.original_sha256 = sha256(data)
    result.repaired_sha256 = sha256(repaired)
    if repaired != data:
        result.action = "would-repair"
    return repaired, result


def iter_lrc_files(root: Path, walk_errors: list[str]):
    def onerror(error: OSError):
        walk_errors.append(str(error))

    for base, dirs, files in os.walk(
        root, topdown=True, followlinks=False, onerror=onerror
    ):
        dirs[:] = [
            directory
            for directory in dirs
            if directory not in SKIP_DIR_NAMES
            and not directory.startswith(".walkman-lrc-backup-")
        ]
        for name in files:
            if name.startswith("._"):
                continue
            if name.lower().endswith(".lrc"):
                yield Path(base) / name


def write_atomic(path: Path, data: bytes) -> None:
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".walkman-tmp", dir=str(path.parent)
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def run_repair(root: Path, apply: bool, backup_root: Path | None = None) -> int:
    """Repair LRC files under root. Returns 0 on success, 1 on errors."""
    if not root.is_dir():
        raise FileNotFoundError(f"root is not a directory: {root}")

    if apply and backup_root is None:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_root = root / f".walkman-lrc-backup-{stamp}"
    if apply and backup_root is not None:
        backup_root.mkdir(parents=True, exist_ok=False)

    walk_errors: list[str] = []
    results: list[FileResult] = []
    changed = 0
    skipped = 0
    errors = 0

    for path in iter_lrc_files(root, walk_errors):
        try:
            original = path.read_bytes()
            repaired, result = transform(original, path)
            if result.action == "would-repair" and repaired is not None:
                if apply:
                    assert backup_root is not None
                    relative = path.relative_to(root)
                    backup_path = backup_root / relative
                    backup_path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(path, backup_path)
                    write_atomic(path, repaired)
                    result.action = "repaired"
                changed += 1
            elif result.action == "skipped":
                skipped += 1
            results.append(result)
        except (OSError, ValueError, UnicodeError) as exc:
            errors += 1
            results.append(FileResult(path=str(path), action="error", error=str(exc)))

    if apply and backup_root is not None:
        manifest = {
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "root": str(root),
            "backup_root": str(backup_root),
            "files": [
                asdict(result) for result in results if result.action == "repaired"
            ],
        }
        (backup_root / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    action_word = "repaired" if apply else "would repair"
    print(f"LRC files scanned: {len(results)}")
    print(f"Files {action_word}: {changed}")
    print(f"Files skipped: {skipped}")
    print(f"Read/write errors: {errors}")
    print(f"Directory-walk errors: {len(walk_errors)}")
    if apply and backup_root is not None:
        print(f"Originals backed up to: {backup_root}")

    for result in results:
        if result.action in {"skipped", "error"}:
            print(f"{result.action.upper()}: {result.path} — {result.error}")
    for error in walk_errors:
        print(f"WALK ERROR: {error}")

    return 1 if errors or walk_errors else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "root", type=Path, help="Storage-card root, for example /Volumes/Biwin"
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually repair files. Without this flag, only perform a dry run.",
    )
    parser.add_argument(
        "--backup-root",
        type=Path,
        help="Optional backup directory. Defaults to a timestamped hidden folder under root.",
    )
    args = parser.parse_args()
    return run_repair(args.root.resolve(), args.apply, args.backup_root)


if __name__ == "__main__":
    raise SystemExit(main())
