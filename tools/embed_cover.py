#!/usr/bin/env python3
"""Embed album covers into downloaded audio files (FLAC picture / MP3 APIC).

Usage:
  python3 tools/embed_cover.py <audio-or-dir> [--recursive] [--dry-run]

For each audio file without an embedded cover, search NetEase by filename,
fetch the album cover URL, download it, and embed it via ffmpeg.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

NETEASECLI_DIR = Path("/Users/shushu/Documents/Codex/2026-08-13/volumes-biwin-music/work/neteasecli")
AUDIO_EXT = {".flac", ".mp3", ".m4a"}


def run_node(body: str) -> str:
    script = Path(tempfile.gettempdir()) / "embed_cover_tmp.mjs"
    script.write_text(
        "import { getApiClient } from '"
        + str(NETEASECLI_DIR)
        + "/dist/api/client.js';\nconst c = getApiClient();\n"
        + body,
        encoding="utf-8",
    )
    try:
        r = subprocess.run(["node", str(script)], capture_output=True, text=True,
                           cwd=NETEASECLI_DIR, timeout=60)
        out = r.stdout.strip().splitlines()
        return out[-1] if out else ""
    finally:
        script.unlink(missing_ok=True)


def search_cover(song: str) -> str | None:
    song_esc = song.replace("'", "\\'")
    body = (
        f"const r = await c.request('/cloudsearch/get/web', {{ s: '{song_esc}', type: 1, limit: 1, offset: 0 }}, 'weapi');\n"
        f"const t = r.result?.songs?.[0];\n"
        f"console.log(t ? (t.al?.picUrl || '') : '');\n"
    )
    out = run_node(body)
    return out if out.startswith("http") else None


def has_cover(audio: Path) -> bool:
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v",
         "-of", "default=noprint_wrappers=1:nokey=1", str(audio)],
        capture_output=True, text=True,
    )
    return bool(r.stdout.strip())


def embed_cover(audio: Path, cover_url: str) -> bool:
    with tempfile.NamedTemporaryFile(prefix=".cover-", suffix=".jpg", dir=audio.parent, delete=False) as h:
        cover = Path(h.name)
    try:
        req = urllib.request.Request(cover_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            cover.write_bytes(resp.read())
        if cover.stat().st_size < 1000:
            return False
        tmp_out = audio.parent / f".{audio.stem}.cover-tmp{audio.suffix}"
        process = subprocess.run(
            ["ffmpeg", "-y", "-i", str(audio), "-i", str(cover),
             "-map", "0:a", "-map", "1:v", "-c", "copy", "-c:v", "mjpeg",
             "-disposition:v", "attached_pic",
             "-metadata:s:v", "title=Album cover", str(tmp_out)],
            capture_output=True, text=True, timeout=30,
        )
        if process.returncode != 0 or not tmp_out.is_file():
            return False
        tmp_out.replace(audio)
        return True
    finally:
        cover.unlink(missing_ok=True)


def extract_song(stem: str) -> str:
    song = stem
    m = re.match(r"^(.+?)\s+-\s+.+$", song)
    if m:
        song = m.group(1)
    m = re.match(r"^花譜\s*[-–—]\s*(.+)$", song)
    if m:
        song = m.group(1)
    m = re.match(r"^(.+?)\s+at\s+I SCREAM LIVE", song)
    if m:
        song = m.group(1)
    m = re.match(r"^(.+?)\(\s*(?:Cover|Instrumental)\s*(?:.*)\)$", song)
    if m:
        song = m.group(1)
    return song.replace("  ", " ").strip()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("target", type=Path, help="音频文件或目录")
    ap.add_argument("--recursive", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    target = args.target.expanduser().resolve()
    if not target.exists():
        print(f"路径不存在: {target}", file=sys.stderr)
        return 2

    files = sorted(target.rglob("*")) if args.recursive else sorted(target.iterdir())
    audios = [p for p in files if p.is_file() and not p.name.startswith("._") and p.suffix.lower() in AUDIO_EXT]
    todo = [p for p in audios if not has_cover(p)]
    print(f"音频 {len(audios)}, 缺封面 {len(todo)}")

    ok = fail = skip = 0
    for i, audio in enumerate(todo, 1):
        song = extract_song(audio.stem)
        if not song:
            skip += 1
            continue
        if args.dry_run:
            print(f"[{i}/{len(todo)}] 计划: {song} <- {audio.name[:40]}", flush=True)
            ok += 1
            continue
        cover_url = search_cover(song)
        if not cover_url:
            fail += 1
            print(f"[{i}/{len(todo)}] FAIL 无封面 {audio.name[:40]}", flush=True)
            time.sleep(0.5)
            continue
        if embed_cover(audio, cover_url):
            ok += 1
            print(f"[{i}/{len(todo)}] OK {audio.name[:40]}", flush=True)
        else:
            fail += 1
            print(f"[{i}/{len(todo)}] FAIL 内嵌失败 {audio.name[:40]}", flush=True)
        time.sleep(0.6)

    print(f"完成: 补封面 {ok} / 跳过 {skip} / 失败 {fail}")
    return 1 if fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
