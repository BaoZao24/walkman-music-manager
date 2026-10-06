#!/usr/bin/env python3
"""Download audio from Bilibili and organize covers (花谱 / 异世界情绪 / 理芽...).

This mirrors the scripts that lived in the old BILIBILIdownload project and
adds them to the walkman workflow:

  list       List all videos of a Bilibili user (WBI-signed API).
  fetch      Fetch titles/durations for a list of BV ids.
  download   Batch-download audio via yt-dlp (mp3, best quality).
  rename     Translate + rename downloaded files (歌名 - 译名), strip [BV] tags.
  copy       Copy renamed files into the final music folders.

Safety: every subcommand is read-only by default (``--dry-run``); only
``download --apply`` and ``rename --apply`` actually write to disk.
"""

from __future__ import annotations

import argparse
import hashlib
import http.cookiejar
import json
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# wbi 混淆表（bilibili 公开签名算法）
MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
]

VIDEO_URL_RE = re.compile(r"(BV[A-Za-z0-9]+)")


class BilibiliError(RuntimeError):
    pass


def load_cookie_header(cookies: Path) -> str:
    if not cookies.is_file():
        return ""
    jar = http.cookiejar.MozillaCookieJar(str(cookies))
    try:
        jar.load(ignore_discard=True, ignore_expires=True)
    except Exception as exc:
        raise BilibiliError(f"加载 cookie 失败: {exc}") from exc
    return "; ".join(f"{c.name}={c.value}" for c in jar)


def http_get(url: str, cookies: Path, timeout: int = 25) -> dict[str, Any]:
    headers = {"User-Agent": UA, "Referer": "https://www.bilibili.com/"}
    cookie = load_cookie_header(cookies)
    if cookie:
        headers["Cookie"] = cookie
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except OSError as exc:
        raise BilibiliError(f"请求失败 {url}: {exc}") from exc
    if not isinstance(payload, dict):
        raise BilibiliError(f"响应格式错误: {url}")
    if payload.get("code") != 0:
        raise BilibiliError(f"API 错误 code={payload.get('code')} msg={payload.get('message')}")
    return payload


def get_mixin_key(cookies: Path) -> str:
    payload = http_get("https://api.bilibili.com/x/web-interface/nav", cookies)
    data = payload.get("data") or {}
    img_url = (data.get("wbi_img") or {}).get("img_url", "")
    sub_url = (data.get("wbi_img") or {}).get("sub_url", "")
    img_key = img_url.rsplit("/", 1)[-1].split(".")[0]
    sub_key = sub_url.rsplit("/", 1)[-1].split(".")[0]
    raw = img_key + sub_key
    return "".join(raw[i] for i in MIXIN_KEY_ENC_TAB)[:32]


def wbi_sign(params: dict[str, Any], mixin_key: str) -> dict[str, Any]:
    params["wts"] = int(time.time())
    cleaned = {k: "".join(c for c in str(v) if c not in "!'()*") for k, v in params.items()}
    qs = "&".join(f"{k}={urllib.parse.quote(str(v), safe='')}" for k, v in sorted(cleaned.items()))
    cleaned["w_rid"] = hashlib.md5((qs + mixin_key).encode("utf-8")).hexdigest()
    return cleaned


def read_bvids(input_path: Path | None, inline: Iterable[str]) -> list[str]:
    values = list(inline)
    if input_path is not None and input_path.is_file():
        values.extend(input_path.read_text(encoding="utf-8", errors="replace").splitlines())
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        value = value.strip()
        if not value or value.startswith("#"):
            continue
        match = VIDEO_URL_RE.search(value)
        bvid = match.group(1) if match else None
        if not bvid:
            raise ValueError(f"无法从 {value!r} 中解析 BV 号")
        if bvid not in seen:
            seen.add(bvid)
            result.append(bvid)
    if not result:
        raise ValueError("至少提供一个 BV 号或 --input 文件")
    return result


# ---------------------------------------------------------------- list ----

def cmd_bili_list(args: argparse.Namespace) -> int:
    if not args.cookies.is_file():
        print(f"找不到 cookie 文件: {args.cookies}", file=sys.stderr)
        return 2
    try:
        mixin_key = get_mixin_key(args.cookies)
        all_videos: list[dict[str, Any]] = []
        pn = 1
        while True:
            params = wbi_sign({"mid": args.uid, "pn": pn, "ps": 30, "order": "pubdate"}, mixin_key)
            qs = "&".join(f"{k}={urllib.parse.quote(str(v), safe='')}" for k, v in params.items())
            url = f"https://api.bilibili.com/x/space/wbi/arc/search?{qs}"
            payload = http_get(url, args.cookies)
            data = payload.get("data") or {}
            vlist = ((data.get("list") or {}).get("vlist")) or []
            total = (data.get("page") or {}).get("count", 0)
            if not vlist:
                break
            all_videos.extend(vlist)
            print(f"已获取第 {pn} 页, 累计 {len(all_videos)}/{total}")
            if len(all_videos) >= total:
                break
            pn += 1
            time.sleep(0.4)
    except BilibiliError as exc:
        print(f"列出视频失败: {exc}", file=sys.stderr)
        return 1

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(all_videos, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"已保存 {len(all_videos)} 条到 {args.output}")
    for video in all_videos:
        print(f"[{video.get('bvid')}] {video.get('title')}")
    return 0


# ---------------------------------------------------------------- fetch ----

def cmd_bili_fetch(args: argparse.Namespace) -> int:
    try:
        bvids = read_bvids(args.input, args.bvids)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    results: list[dict[str, Any]] = []
    failed = 0
    for bvid in bvids:
        url = f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}"
        try:
            payload = http_get(url, args.cookies)
        except BilibiliError as exc:
            failed += 1
            print(f"{bvid}: {exc}", file=sys.stderr)
            results.append({"bvid": bvid, "title": "__FAILED__", "duration": 0, "pubdate": 0})
            continue
        data = payload.get("data") or {}
        results.append({
            "bvid": bvid,
            "title": data.get("title", ""),
            "duration": data.get("duration", 0),
            "pubdate": data.get("pubdate", 0),
        })
        print(f"{bvid}: {data.get('title', '')} ({data.get('duration', 0)}s)")
        time.sleep(0.3)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"已保存 {len(results)} 条到 {args.output}")
    return 1 if failed else 0


# ------------------------------------------------------------- download ----

def resolve_ytdlp(args: argparse.Namespace) -> str:
    if args.ytdlp != "yt-dlp":
        return args.ytdlp
    found = shutil.which("yt-dlp")
    if found:
        return found
    workdir = getattr(args, "workdir", Path.cwd())
    for candidate in (workdir / "yt-dlp.exe", workdir / "yt-dlp"):
        if candidate.is_file():
            return str(candidate)
    raise BilibiliError("找不到 yt-dlp（请安装或指定 --ytdlp）")


def cmd_bili_download(args: argparse.Namespace) -> int:
    try:
        bvids = read_bvids(args.input, args.bvids)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    try:
        ytdlp = resolve_ytdlp(args)
    except BilibiliError as exc:
        if args.dry_run:
            ytdlp = "yt-dlp"
        else:
            print(str(exc), file=sys.stderr)
            return 2

    outdir = args.output_dir.expanduser().resolve()
    outdir.mkdir(parents=True, exist_ok=True)
    audio_suffixes = {".mp3", ".flac", ".m4a", ".ogg", ".opus"}

    def exists_in_output(bvid: str) -> bool:
        for path in outdir.iterdir():
            if path.is_file() and path.suffix.lower() in audio_suffixes and bvid in path.name:
                return True
        return False

    ok = skip = fail = 0
    failed: list[str] = []
    for bvid in bvids:
        if exists_in_output(bvid):
            skip += 1
            print(f"SKIP {bvid} (已存在)", flush=True)
            continue
        url = f"https://www.bilibili.com/video/{bvid}"
        command = [
            ytdlp,
            "--no-playlist",
            "-x", "--audio-format", "mp3", "--audio-quality", "0",
            "--embed-thumbnail",
            "--retries", "5", "--fragment-retries", "5",
            "--newline", "--no-warnings",
            "-o", str(outdir / "%(title)s [%(id)s].%(ext)s"),
        ]
        if args.cookies.is_file():
            command.extend(["--cookies", str(args.cookies)])
        if args.no_proxy:
            command.extend(["--proxy", ""])
        if args.ffmpeg_location:
            command.extend(["--ffmpeg-location", str(args.ffmpeg_location)])
        command.append(url)
        print(f"DL {bvid}", flush=True)
        if args.dry_run:
            print(f"  预览命令: {' '.join(command[:4])} ... {url}")
            ok += 1
            continue
        try:
            process = subprocess.run(command)
        except OSError as exc:
            print(f"  启动 yt-dlp 失败: {exc}", file=sys.stderr)
            return 1
        if process.returncode == 0:
            ok += 1
            print("    OK", flush=True)
        else:
            fail += 1
            failed.append(bvid)
            print(f"    FAIL rc={process.returncode}", flush=True)
        time.sleep(2)

    print(f"\n==== 完成: 成功 {ok} / 跳过 {skip} / 失败 {fail} ====", flush=True)
    if failed:
        failed_file = outdir / "failed_bvids.txt"
        failed_file.write_text("\n".join(failed) + "\n", encoding="utf-8")
        print(f"失败 BV 号已写入 {failed_file}")
    return 1 if fail else 0


# --------------------------------------------------------------- rename ----

def cmd_bili_rename(args: argparse.Namespace) -> int:
    source = args.source.expanduser().resolve()
    if not source.is_dir():
        print(f"不是目录: {source}", file=sys.stderr)
        return 2
    translations: dict[str, str] = {}
    if args.translations and args.translations.is_file():
        translations = json.loads(args.translations.read_text(encoding="utf-8"))
    if not translations:
        print(
            "未提供译名表（--translations）。文件名将只去掉 [BV] 后缀。",
            file=sys.stderr,
        )

    plan: list[tuple[Path, str]] = []
    for path in sorted(source.iterdir()):
        if not path.is_file() or path.suffix.lower() not in (".mp3", ".flac", ".m4a"):
            continue
        name = path.stem
        bvid_match = re.search(r"\[(BV[A-Za-z0-9]+)\]", path.name)
        bvid = bvid_match.group(1) if bvid_match else None
        # 歌名部分：标题 [BV] 中的标题（通常含 歌名/译名 或纯歌名）
        newname = name
        if bvid and translations:
            target = translations.get(bvid)
            if target:
                newname = target
            else:
                # 从标题提取: 取最后一段 / 或 - 后的中文译名
                parts = re.split(r"[/⧸]", name)
                tail = parts[-1].strip()
                if re.match(r"^[\u3400-\u9fff]", tail):
                    newname = tail
        newname = re.sub(r"\s*\[BV[A-Za-z0-9]+\]\s*$", "", newname).strip()
        newname = newname.replace(":", "：").replace("/", "／")
        if not newname:
            newname = name
        plan.append((path, newname))

    counter = Counter(new for _, new in plan)
    used: set[str] = set()
    renamed = conflict = 0
    for path, newname in plan:
        target_name = newname + path.suffix.lower()
        if counter[newname] > 1 and newname in used:
            base = newname
            i = 2
            while f"{base} ({i})" in used:
                i += 1
            newname = f"{base} ({i})"
            target_name = newname + path.suffix.lower()
            conflict += 1
        used.add(newname)
        if path.name == target_name:
            continue
        print(f"{path.name[:40]} -> {target_name[:50]}")
        if not args.dry_run:
            path.replace(path.with_name(target_name))
        renamed += 1
    print(f"=== 重命名 {renamed} 个, 重名加序号 {conflict} 个 (dry_run={args.dry_run}) ===")
    return 0


# ----------------------------------------------------------------- copy ----

def cmd_bili_copy(args: argparse.Namespace) -> int:
    source = args.source.expanduser().resolve()
    dest = args.dest.expanduser().resolve()
    if not source.is_dir():
        print(f"不是目录: {source}", file=sys.stderr)
        return 2
    dest.mkdir(parents=True, exist_ok=True)
    existing = {p.name for p in dest.iterdir() if p.is_file() and not p.name.startswith("._")}
    copied = skipped = 0
    for path in sorted(source.iterdir()):
        if not path.is_file() or path.suffix.lower() not in (".mp3", ".flac", ".m4a"):
            continue
        if path.name in existing:
            skipped += 1
            continue
        if args.dry_run:
            print(f"将复制: {path.name}")
            copied += 1
            continue
        try:
            shutil.copy2(path, dest / path.name)
        except OSError:
            # exFAT（Walkman 卡）不支持 copy2 复制 chflags/xattr，降级为纯数据复制
            shutil.copyfile(path, dest / path.name)
        copied += 1
    print(f"复制 {copied} 个, 跳过同名 {skipped} 个 (dry_run={args.dry_run})")
    return 0


def build_parser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("bilibili", help="从哔哩哔哩下载音频并整理翻唱")
    bsub = p.add_subparsers(dest="bilibili_command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--cookies", type=Path, default=Path("www.bilibili.com_cookies.txt"),
                        help="bilibili 导出的 Netscape cookies 文件")
    common.add_argument("--dry-run", action="store_true", help="只显示将要执行的操作，不落地")

    p_list = bsub.add_parser("list", help="列出 UP 主全部视频", parents=[common])
    p_list.add_argument("uid", type=int, help="UP 主 UID")
    p_list.add_argument("--output", type=Path, help="保存视频列表 JSON")
    p_list.set_defaults(func=cmd_bili_list)

    p_fetch = bsub.add_parser("fetch", help="获取 BV 号标题/时长", parents=[common])
    p_fetch.add_argument("bvids", nargs="*")
    p_fetch.add_argument("--input", type=Path, help="每行一个 BV 号的文件")
    p_fetch.add_argument("--output", type=Path, help="保存结果 JSON")
    p_fetch.set_defaults(func=cmd_bili_fetch)

    p_dl = bsub.add_parser("download", help="批量下载音频(mp3)", parents=[common])
    p_dl.add_argument("bvids", nargs="*")
    p_dl.add_argument("--input", type=Path, help="每行一个 BV 号的文件")
    p_dl.add_argument("--output-dir", type=Path, required=True, help="下载目录(如 KAF_Covers)")
    p_dl.add_argument("--ytdlp", default="yt-dlp", help="yt-dlp 可执行文件路径")
    p_dl.add_argument("--ffmpeg-location", type=Path, help="ffmpeg 所在目录(Windows 需要)")
    p_dl.add_argument("--no-proxy", action="store_true", help="强制直连，绕开系统代理")
    p_dl.set_defaults(func=cmd_bili_download)

    p_rn = bsub.add_parser("rename", help="重命名: 去 [BV] 后缀/应用译名", parents=[common])
    p_rn.add_argument("source", type=Path, help="下载目录")
    p_rn.add_argument("--translations", type=Path, help="JSON 译名表 {BV号: 新名}")
    p_rn.set_defaults(func=cmd_bili_rename)

    p_cp = bsub.add_parser("copy", help="复制到最终音乐目录", parents=[common])
    p_cp.add_argument("source", type=Path)
    p_cp.add_argument("dest", type=Path)
    p_cp.set_defaults(func=cmd_bili_copy)
