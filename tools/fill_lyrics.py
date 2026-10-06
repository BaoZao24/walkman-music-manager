#!/usr/bin/env python3
"""Fill missing LRC lyrics for downloaded audio from NetEase Cloud Music.

Usage:
  python3 tools/fill_lyrics.py <audio-or-dir> [--recursive] [--dry-run]

For each audio file without a same-named .lrc, search NetEase by filename,
fetch the lyric, repair it to Walkman format, and write it next to the audio.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
import time
import unicodedata
from pathlib import Path

NETEASECLI_DIR = Path("/opt/homebrew/lib/node_modules/neteasecli")
WR = Path(__file__).resolve().parent.parent
AUDIO_EXT = {".flac", ".mp3", ".m4a"}


def norm(s: str) -> str:
    s = unicodedata.normalize("NFC", s)
    return re.sub(r"[\s\-_.,!?！？:：'\"()（）\[\]【】~～〜・\uff5e⧸_／*×#/]", "", s.lower())


def run_node(body: str) -> str:
    script = Path(tempfile.gettempdir()) / "fill_lyrics_tmp.mjs"
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


def search_id(song: str) -> str | None:
    song_esc = song.replace("'", "\\'")
    body = (
        f"const r = await c.request('/cloudsearch/get/web', {{ s: '{song_esc}', type: 1, limit: 1, offset: 0 }}, 'weapi');\n"
        f"const t = r.result?.songs?.[0];\n"
        f"console.log(t ? t.id + '|' + t.name + '|' + (t.ar?.map(a=>a.name).join(',')) : '');\n"
    )
    out = run_node(body)
    return out if "|" in out else None


def fetch_lyric(track_id: str) -> str | None:
    r = subprocess.run(
        ["neteasecli", "--json", "track", "lyric", str(track_id)],
        capture_output=True, text=True, timeout=30,
    )
    try:
        payload = json.loads(r.stdout.strip()).get("data") or {}
    except json.JSONDecodeError:
        return None
    lrc = payload.get("lrc") or ""
    if isinstance(lrc, dict):
        lrc = lrc.get("lyric") or ""
    tlyric = payload.get("tlyric") or ""
    if isinstance(tlyric, dict):
        tlyric = tlyric.get("lyric") or ""
    text = tlyric.strip() or lrc.strip()
    return text or None


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
    missing = [p for p in audios if not p.with_suffix(".lrc").exists()]
    print(f"音频 {len(audios)}, 缺 lrc {len(missing)}")

    sys.path.insert(0, str(WR))
    from walkman_lrc_repair import transform, write_atomic

    ok = fail = skip = 0
    for i, audio in enumerate(missing, 1):
        song = extract_song(audio.stem)
        if not song:
            skip += 1
            continue
        if args.dry_run:
            print(f"[{i}/{len(missing)}] 计划: {song} <- {audio.name[:40]}", flush=True)
            ok += 1
            continue
        hit = search_id(song)
        if not hit:
            fail += 1
            print(f"[{i}/{len(missing)}] FAIL 无匹配 {audio.name[:40]}", flush=True)
            time.sleep(0.5)
            continue
        tid = hit.split("|")[0]
        text = fetch_lyric(tid)
        if not text:
            fail += 1
            print(f"[{i}/{len(missing)}] FAIL 无歌词 {audio.name[:40]}", flush=True)
            time.sleep(0.5)
            continue
        repaired, _ = transform(text.encode("utf-8"), audio.with_suffix(".lrc"))
        if repaired is None:
            fail += 1
            print(f"[{i}/{len(missing)}] FAIL 修复失败 {audio.name[:40]}", flush=True)
            time.sleep(0.5)
            continue
        write_atomic(audio.with_suffix(".lrc"), repaired)
        ok += 1
        print(f"[{i}/{len(missing)}] OK {audio.name[:40]}", flush=True)
        time.sleep(0.8)

    print(f"完成: 成功 {ok} / 跳过 {skip} / 失败 {fail}")
    return 1 if fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
