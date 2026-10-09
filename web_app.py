#!/usr/bin/env python3
"""Local web interface for Walkman Music Manager.

The server only exposes a small allowlist of existing project operations. API
credentials are kept in a user config file and are never sent to the browser.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request, urlopen

from download_music import ensure_volume_mounted
from ai_agent import AgentCancelled, AgentManager, TOOL_SPECS, TOOLS
from token_usage import current_task_id, record_usage, usage_snapshot


PROJECT_ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)).resolve()
WEB_ROOT = PROJECT_ROOT / "web"
MAX_REQUEST_BYTES = 256_000
DEFAULT_LIBRARY_DIR = Path.home() / "Music" / "Walkman"
DEFAULT_SETTINGS = {
    "api_base_url": "https://api.openai.com/v1",
    "model": "gpt-4o-mini",
    "api_key": "",
    "library_dir": str(DEFAULT_LIBRARY_DIR),
    "bilibili_cookies": str(Path.home() / "www.bilibili.com_cookies.txt"),
}
QUALITIES = {"standard", "higher", "exhigh", "lossless", "hires"}
LYRICS_MODES = {"original", "translated", "bilingual"}
TRACK_ID_RE = re.compile(r"^\d+$")
PROCESS_LOCK = threading.Lock()
ACTIVE_PROCESSES: set[subprocess.Popen] = set()


def settings_path() -> Path:
    override = os.environ.get("WALKMAN_APP_CONFIG_DIR")
    if override:
        return Path(override).expanduser() / "settings.json"
    return Path.home() / ".config" / "walkman-music-manager" / "settings.json"


def load_settings() -> dict[str, str]:
    settings = dict(DEFAULT_SETTINGS)
    try:
        stored = json.loads(settings_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return settings
    if isinstance(stored, dict):
        for key in DEFAULT_SETTINGS:
            value = stored.get(key)
            if isinstance(value, str):
                settings[key] = value
    return settings


def public_settings(settings: dict[str, str]) -> dict[str, Any]:
    api_key_configured = bool(settings["api_key"])
    endpoint_host = (urlsplit(settings["api_base_url"]).hostname or "").lower()
    local_endpoint = endpoint_host in {"127.0.0.1", "localhost", "::1"}
    return {
        "api_base_url": settings["api_base_url"],
        "model": settings["model"],
        "api_key_configured": api_key_configured,
        "ai_configured": api_key_configured or local_endpoint,
        "library_dir": settings["library_dir"],
        "bilibili_cookies": settings.get("bilibili_cookies", ""),
    }


def save_settings(settings: dict[str, str]) -> None:
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".settings-", suffix=".json", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(settings, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    finally:
        temporary.unlink(missing_ok=True)


def validate_base_url(value: str) -> str:
    base_url = value.strip().rstrip("/")
    parts = urlsplit(base_url)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise ValueError("API 地址必须是 http:// 或 https:// 开头的完整地址")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError("API 地址中不要包含账号、密码、查询参数或片段")
    return base_url


def chat_completions_url(base_url: str) -> str:
    if base_url.endswith("/chat/completions"):
        return base_url
    return base_url + "/chat/completions"


def call_chat_message(
    settings: dict[str, str], messages: list[dict[str, Any]],
    timeout: int = 60, tools: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    base_url = validate_base_url(settings["api_base_url"])
    if not settings["model"].strip():
        raise ValueError("请先设置模型名称")
    body: dict[str, Any] = {"model": settings["model"], "messages": messages, "temperature": 0.1}
    if tools is not None:
        body.update({"tools": tools, "tool_choice": "auto"})
    payload = json.dumps(body).encode("utf-8")
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if settings["api_key"]:
        headers["Authorization"] = f"Bearer {settings['api_key']}"
    request = Request(chat_completions_url(base_url), data=payload, headers=headers)
    try:
        with urlopen(request, timeout=timeout) as response:
            result = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        record_usage(None, model=settings["model"], source="assistant")
        detail = exc.read(3000).decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"AI 服务返回 HTTP {exc.code}" + (f": {detail}" if detail else "")) from exc
    except (URLError, TimeoutError, OSError) as exc:
        record_usage(None, model=settings["model"], source="assistant")
        raise RuntimeError(f"连接 AI 服务失败：{exc}") from exc
    except json.JSONDecodeError as exc:
        record_usage(None, model=settings["model"], source="assistant")
        raise RuntimeError("AI 服务没有返回有效 JSON") from exc

    response_data = result if isinstance(result, dict) else {}
    usage = record_usage(response_data.get("usage"), model=response_data.get("model") or settings["model"], source="assistant")
    try:
        message = result["choices"][0]["message"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError("AI 服务响应中没有 choices[0].message") from exc
    if not isinstance(message, dict):
        raise RuntimeError("AI 服务返回的消息格式无效")
    content = message.get("content")
    if isinstance(content, list):
        content = "".join(
            str(part.get("text", ""))
            for part in content
            if isinstance(part, dict)
        )
    message["content"] = content
    message["_usage"] = usage
    return message


def call_chat_api(
    settings: dict[str, str], messages: list[dict[str, str]], timeout: int = 60
) -> str:
    content = call_chat_message(settings, messages, timeout=timeout).get("content")
    if not isinstance(content, str) or not content.strip():
        raise RuntimeError("AI 服务返回了空内容")
    return content.strip()


def _as_text(value: Any, *, limit: int = 4096) -> str:
    return value.strip()[:limit] if isinstance(value, str) else ""


def run_search(query: str, limit: int, *, cancel_event=None, on_progress=None) -> dict[str, Any]:
    query = query.strip()
    if not query:
        raise ValueError("请输入歌曲名、歌手或关键词")
    try:
        limit = max(1, min(100, int(limit)))
    except (TypeError, ValueError) as exc:
        raise ValueError("搜索数量必须是 1 到 100 之间的整数") from exc
    process = run_local_process(
        _python_script_command(PROJECT_ROOT / "search_music.py", query, "--limit", str(limit), "--json"),
        timeout=90,
        cancel_event=cancel_event,
    )
    if process.returncode != 0:
        raise RuntimeError((process.stderr or process.stdout or "搜索失败").strip()[-8000:])
    try:
        return json.loads(process.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("搜索工具没有返回有效 JSON") from exc


def action_command(action: str, args: dict[str, Any], apply: bool) -> list[str]:
    walkman = str(PROJECT_ROOT / "walkman.py")
    if action == "download":
        ids = args.get("track_ids", [])
        if not isinstance(ids, list) or not ids or any(not TRACK_ID_RE.fullmatch(str(item)) for item in ids):
            raise ValueError("请先选中搜索结果中的歌曲")
        output_dir = _as_text(args.get("output_dir"))
        if not output_dir:
            raise ValueError("请设置下载目标文件夹")
        command = [
            *_python_script_command(walkman, "download", *[str(item) for item in ids]),
            "--output-dir", str(Path(output_dir).expanduser()),
            "--quality", args.get("quality", "exhigh") if isinstance(args.get("quality"), str) and args.get("quality") in QUALITIES else "exhigh",
            "--lyrics", args.get("lyrics", "translated") if isinstance(args.get("lyrics"), str) and args.get("lyrics") in LYRICS_MODES else "translated",
        ]
        if not apply:
            command.append("--dry-run")
        for field, flag in (("by_album", "--by-album"), ("flat", "--flat")):
            if args.get(field) is True:
                command.append(flag)
        if args.get("translate_japanese") is True:
            command.extend(["--translate-japanese", "--translation-model", args.get("_translation_model", DEFAULT_SETTINGS["model"])])
        return command
    if action.startswith("bili_"):
        command = _python_script_command(walkman, "bilibili")
        cookies = _as_text(args.get("cookies"), limit=4096)
        if action == "bili_list":
            try:
                uid = int(args.get("uid", 0))
            except (TypeError, ValueError) as exc:
                raise ValueError("请输入有效的 B 站 UP 主 UID") from exc
            if uid <= 0:
                raise ValueError("请输入有效的 B 站 UP 主 UID")
            command.extend(["list", str(uid)])
        elif action == "bili_fetch":
            bvids = normalize_bvids(args.get("bvids"))
            command.extend(["fetch", *bvids])
        elif action == "bili_download":
            bvids = normalize_bvids(args.get("bvids"))
            output_dir = _as_text(args.get("output_dir"))
            if not output_dir:
                raise ValueError("请填写翻唱下载的暂存文件夹")
            ensure_volume_mounted(Path(output_dir).expanduser())
            command.extend(["download", *bvids, "--output-dir", str(Path(output_dir).expanduser())])
            if args.get("no_proxy") is True:
                command.append("--no-proxy")
        elif action == "bili_rename":
            source = _as_text(args.get("source"))
            if not source:
                raise ValueError("请填写翻唱暂存文件夹")
            command.extend(["rename", str(Path(source).expanduser())])
            translations = _as_text(args.get("translations"))
            if translations:
                command.extend(["--translations", str(Path(translations).expanduser())])
        elif action == "bili_copy":
            source = _as_text(args.get("source"))
            dest = _as_text(args.get("dest"))
            if not source or not dest:
                raise ValueError("请填写暂存来源和最终音乐文件夹")
            ensure_volume_mounted(Path(dest).expanduser())
            command.extend(["copy", str(Path(source).expanduser()), str(Path(dest).expanduser())])
        else:
            raise ValueError("不支持的哔哩哔哩操作")
        if cookies:
            command.extend(["--cookies", str(Path(cookies).expanduser())])
        if action in {"bili_list", "bili_fetch"} and args.get("_result_path"):
            command.extend(["--output", str(args["_result_path"])])
        if action in {"bili_rename", "bili_copy"} and not apply:
            command.append("--dry-run")
        return command
    if action == "convert":
        source = _as_text(args.get("source"))
        output_dir = _as_text(args.get("output_dir"))
        if not source or not output_dir:
            raise ValueError("请填写 .ncm 来源路径和输出文件夹")
        command = _python_script_command(walkman, "convert", str(Path(source).expanduser()), "--output-dir", str(Path(output_dir).expanduser()))
        if args.get("flat") is True:
            command.append("--flat")
        if args.get("translate_japanese") is True:
            command.extend(["--translate-japanese", "--translation-model", args.get("_translation_model", DEFAULT_SETTINGS["model"])])
        if not apply:
            command.append("--dry-run")
        return command
    if action == "repair":
        root = _as_text(args.get("root"))
        if not root:
            raise ValueError("请填写要扫描的音乐文件夹")
        command = _python_script_command(walkman, "repair", str(Path(root).expanduser()))
        if apply:
            command.append("--apply")
        return command
    if action == "album":
        source = _as_text(args.get("source"))
        output_dir = _as_text(args.get("output_dir"))
        album_name = _as_text(args.get("album_name"))
        if not source or not output_dir or not album_name:
            raise ValueError("请填写源文件夹、目标文件夹和专辑名称")
        output_root = Path(output_dir).expanduser().resolve()
        target = (output_root / album_name).resolve()
        if target == output_root or not target.is_relative_to(output_root):
            raise ValueError("专辑名称必须是目标文件夹内的相对路径")
        command = [
            *_python_script_command(walkman, "album", str(Path(source).expanduser())),
            "--output-dir", str(Path(output_dir).expanduser()),
            "--album-name", album_name,
        ]
        if not apply:
            command.append("--dry-run")
        return command
    raise ValueError("不支持的操作")


def _python_script_command(script: str | Path, *arguments: str) -> list[str]:
    """Build a script command that also works inside the packaged macOS app."""
    if getattr(sys, "frozen", False):
        cli = Path(sys.executable).with_name("WalkmanCLI")
        return [str(cli), Path(script).relative_to(PROJECT_ROOT).as_posix(), *arguments]
    return [sys.executable, str(script), *arguments]


def _process_cwd() -> Path:
    """Use a writable, predictable directory for Finder-launched app actions."""
    return Path.home() if getattr(sys, "frozen", False) else PROJECT_ROOT


def _prepare_gui_path() -> None:
    """Finder-launched apps do not inherit Homebrew's shell PATH."""
    candidates = [
        "/opt/homebrew/bin",
        "/usr/local/bin",
        "/opt/homebrew/sbin",
        "/usr/local/sbin",
        str(Path.home() / ".local" / "bin"),
    ]
    current = os.environ.get("PATH", "").split(os.pathsep)
    os.environ["PATH"] = os.pathsep.join(dict.fromkeys([*candidates, *current]))


def stop_local_process(process: subprocess.Popen) -> None:
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    elif process.poll() is None:
        process.terminate()
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        if os.name == "posix":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        else:
            process.kill()
        process.wait(timeout=2)


def run_local_process(command: list[str], timeout: int, *, cancel_event=None, on_progress=None, environment=None):
    """Capture tool output and stop the whole subprocess group on cancellation."""
    if cancel_event is not None and cancel_event.is_set():
        raise AgentCancelled()
    env = {**os.environ, "PYTHONUNBUFFERED": "1", "WALKMAN_USAGE_TASK_ID": current_task_id(), **(environment or {})}
    process = subprocess.Popen(
        command, cwd=_process_cwd(), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace", env=env,
        start_new_session=os.name == "posix",
    )
    with PROCESS_LOCK:
        ACTIVE_PROCESSES.add(process)
    stdout, stderr = [], []

    def read_stream(stream, lines):
        for line in stream:
            lines.append(line)
            if on_progress and line.strip():
                on_progress(line.rstrip())
        stream.close()

    readers = [
        threading.Thread(target=read_stream, args=(process.stdout, stdout), daemon=True),
        threading.Thread(target=read_stream, args=(process.stderr, stderr), daemon=True),
    ]
    for reader in readers:
        reader.start()
    started = time.monotonic()
    try:
        while process.poll() is None:
            if cancel_event is not None and cancel_event.is_set():
                raise AgentCancelled()
            if time.monotonic() - started > timeout:
                raise subprocess.TimeoutExpired(command, timeout)
            time.sleep(0.1)
        if cancel_event is not None and cancel_event.is_set():
            raise AgentCancelled()
    except BaseException:
        stop_local_process(process)
        raise
    finally:
        for reader in readers:
            reader.join(timeout=2)
        with PROCESS_LOCK:
            ACTIVE_PROCESSES.discard(process)
    return subprocess.CompletedProcess(command, process.returncode, "".join(stdout), "".join(stderr))


def run_action(action: str, args: dict[str, Any], apply: bool, *, cancel_event=None, on_progress=None, environment=None) -> dict[str, Any]:
    if action == "search":
        result = run_search(str(args.get("query", "")), args.get("limit", 10), cancel_event=cancel_event)
        return {"ok": True, "code": 0, "data": result, "output": f"找到 {len(result.get('tracks') or [])} 首歌曲。", "read_only": True}
    if action in {"bili_download", "bili_copy"} and not apply:
        if action == "bili_download":
            bvids = normalize_bvids(args.get("bvids"))
            output_dir = _as_text(args.get("output_dir"))
            if not output_dir:
                raise ValueError("请填写翻唱下载的暂存文件夹")
            target = Path(output_dir).expanduser()
            ensure_volume_mounted(target)
            output = [f"暂存目录：{target}", f"检查 {len(bvids)} 个视频（已有 BV 文件会自动跳过）"]
            output.extend(f"  · {bvid}" for bvid in bvids[:30])
            if len(bvids) > 30:
                output.append(f"  · 及另外 {len(bvids) - 30} 个视频")
            output.append("下载由 yt-dlp 执行，音频将保存为 MP3 并尝试嵌入视频封面。")
        else:
            source = Path(_as_text(args.get("source"))).expanduser()
            dest = Path(_as_text(args.get("dest"))).expanduser()
            if not source.is_dir() or not str(args.get("dest", "")).strip():
                raise ValueError("请填写有效的暂存来源和最终音乐文件夹")
            ensure_volume_mounted(dest)
            suffixes = {".mp3", ".flac", ".m4a"}
            existing = {path.name for path in dest.iterdir() if path.is_file() and not path.name.startswith("._")} if dest.is_dir() else set()
            candidates = sorted(path for path in source.iterdir() if path.is_file() and path.suffix.lower() in suffixes)
            copied = [path for path in candidates if path.name not in existing]
            skipped = len(candidates) - len(copied)
            output = [f"来源：{source}", f"目标：{dest}", f"将复制 {len(copied)} 个，跳过同名 {skipped} 个"]
            output.extend(f"  · {path.name}" for path in copied[:30])
            if len(copied) > 30:
                output.append(f"  · 及另外 {len(copied) - 30} 个文件")
        return {"ok": True, "code": 0, "output": "\n".join(output), "read_only": False}
    if action in {"bili_list", "bili_fetch"}:
        command = action_command(action, args, apply=False)
        process = run_local_process(
            command, timeout=600, cancel_event=cancel_event, on_progress=on_progress, environment=environment,
        )
        output = "\n".join(part.strip() for part in (process.stdout, process.stderr) if part.strip())
        return {"ok": process.returncode == 0, "code": process.returncode, "output": output[-30000:] or "查询完成。", "read_only": True}
    command = action_command(action, args, apply)
    timeout = 3600 if action == "bili_download" else 900 if action in {"download", "convert"} else 300
    process = run_local_process(
        command, timeout=timeout, cancel_event=cancel_event, on_progress=on_progress, environment=environment,
    )
    output = "\n".join(part.strip() for part in (process.stdout, process.stderr) if part.strip())
    return {
        "ok": process.returncode == 0,
        "code": process.returncode,
        "output": output[-30000:] or ("操作完成。" if process.returncode == 0 else "操作失败。"),
    }


def normalize_bvids(value: Any) -> list[str]:
    values = re.split(r"[\s,，]+", value.strip()) if isinstance(value, str) else value
    if not isinstance(values, list):
        raise ValueError("请提供至少一个 BV 号或视频链接")
    result: list[str] = []
    for item in values:
        match = re.search(r"BV[A-Za-z0-9]+", str(item))
        if not match:
            continue
        bvid = match.group(0)
        if bvid not in result:
            result.append(bvid)
    if not result:
        raise ValueError("请提供至少一个有效的 BV 号或视频链接")
    if len(result) > 200:
        raise ValueError("一次最多处理 200 个视频")
    return result


def agent_environment(settings: dict[str, str]) -> dict[str, Any]:
    library = Path(settings["library_dir"]).expanduser()
    cookie_path = settings.get("bilibili_cookies", "")
    devices = []
    volumes = Path("/Volumes")
    if volumes.is_dir():
        for path in sorted(volumes.iterdir()):
            if path.is_dir() and path.resolve() != Path("/"):
                usage = shutil.disk_usage(path)
                devices.append({"name": path.name, "path": str(path), "writable": os.access(path, os.W_OK),
                                "total_bytes": usage.total, "free_bytes": usage.free})
    return {
        "ok": True, "library_dir": str(library),
        "download_staging": str(library / "Incoming"),
        "bilibili_staging": str(library / "Bilibili-Staging"),
        "bilibili_cookies": cookie_path,
        "cookie_file_exists": bool(cookie_path and Path(cookie_path).expanduser().is_file()),
        "locations": {"music": str(Path.home() / "Music"), "downloads": str(Path.home() / "Downloads")},
        "devices": devices,
        "library_free_bytes": shutil.disk_usage(library if library.exists() else Path.home()).free,
        "tools": {name: bool(shutil.which(name)) for name in ("neteasecli", "yt-dlp", "ffmpeg", "ffprobe", "ncmdump", "node")},
    }


def run_agent_tool(name, args, settings, context, cancel_event, on_progress) -> dict[str, Any]:
    if cancel_event.is_set():
        raise AgentCancelled()
    args = dict(args)
    for field in ("path", "source", "root", "output_dir", "dest", "target", "cookies"):
        if field in args and isinstance(args[field], str):
            args[field] = args[field].strip()
            if not args[field]:
                del args[field]
            elif not Path(args[field]).expanduser().is_absolute():
                raise ValueError(f"{field} 必须是本机绝对路径或 ~/ 路径，不能把描述当成文件路径")
    library = Path(settings["library_dir"]).expanduser()
    staging = library / "Bilibili-Staging"
    if name == "environment":
        return agent_environment(settings)
    if name == "list_files":
        path = Path(args.get("path") or library).expanduser().resolve()
        ensure_volume_mounted(path)
        if not path.exists():
            return {"ok": True, "path": str(path), "exists": False, "entries": []}
        candidates = [path] if path.is_file() else sorted(
            (entry for entry in path.iterdir() if not entry.name.startswith(".")),
            key=lambda entry: (not entry.is_dir(), entry.name.casefold()),
        )
        offset, limit = args.get("offset", 0), args.get("limit", 100)
        entries = []
        for entry in candidates[offset:offset + limit]:
            try:
                entries.append({"name": entry.name, "path": str(entry), "kind": "directory" if entry.is_dir() else "file",
                                "size": entry.stat().st_size if entry.is_file() else None, "suffix": entry.suffix.lower()})
                context["bvids"].update(re.findall(r"BV[A-Za-z0-9]+", entry.name))
            except OSError:
                continue
        return {"ok": True, "path": str(path), "exists": True, "total": len(candidates), "entries": entries,
                "next_offset": offset + limit if offset + limit < len(candidates) else None}
    if name == "download":
        ids = args.get("track_ids", [])
        supplied_ids = set(re.findall(r"(?<!\d)\d{4,}(?!\d)", context["user_text"]))
        supplied_ids.update(re.findall(r"(?:\bid\s*[:：=]?\s*|song\?id=)(\d+)", context["user_text"], flags=re.I))
        if any(str(track_id) not in context["track_ids"] | supplied_ids for track_id in ids):
            raise ValueError("歌曲 ID 必须来自真实搜索结果或用户提供的信息；请先搜索")
    if name in {"bili_fetch", "bili_download"}:
        bvids = normalize_bvids(args.get("bvids"))
        supplied = set(re.findall(r"BV[A-Za-z0-9]+", context["user_text"]))
        if not set(bvids).issubset(context["bvids"] | supplied):
            raise ValueError("BV 号必须来自用户文字、文件名或投稿查询；请先查询")
        args["bvids"] = bvids
    if name == "bili_list":
        if str(args["uid"]) not in re.findall(r"(?<!\d)\d+(?!\d)", context["user_text"]):
            raise ValueError("UP 主 UID 必须由用户提供，不能猜测")
        cached = context.get("bili_lists", {}).get((args["uid"], args.get("cookies") or settings.get("bilibili_cookies", "")))
        if cached is not None:
            offset, limit = args.get("offset", 0), args.get("limit", 100)
            videos = cached[offset:offset + limit]
            context["bvids"].update(video["bvid"] for video in videos if video.get("bvid"))
            return {"ok": True, "data": {"videos": videos, "total": len(cached),
                    "next_offset": offset + limit if offset + limit < len(cached) else None},
                    "output": f"投稿 {len(cached)} 条，当前返回第 {offset + 1} 至 {offset + len(videos)} 条。"}
    if name in {"download", "convert", "album"}:
        args.setdefault("output_dir", str(library))
        ensure_volume_mounted(Path(args["output_dir"]).expanduser())
    if name == "repair":
        args.setdefault("root", str(library))
    if name == "bili_download":
        args.setdefault("output_dir", str(staging))
    if name in {"bili_rename", "bili_copy"}:
        args.setdefault("source", str(staging))
    if name == "bili_copy":
        args.setdefault("dest", str(library))
    if name.startswith("bili_"):
        args.setdefault("cookies", settings.get("bilibili_cookies", ""))
    for field in ("source", "root"):
        if args.get(field):
            source = Path(args[field]).expanduser().resolve()
            ensure_volume_mounted(source)
            if not source.exists():
                raise ValueError(f"来源路径不存在：{source}")
    if name == "album":
        source = Path(args["source"]).expanduser().resolve()
        if source in {Path("/"), Path.home(), Path.home() / "Music", Path.home() / "Downloads", library.resolve(), Path("/Volumes")}:
            raise ValueError("专辑归档需要具体的音乐暂存目录。请把本次歌曲下载到独立目录后再归档，避免搬走整个曲库")
    if name in {"fill_lyrics", "embed_cover"}:
        target = Path(args.get("target") or library).expanduser().resolve()
        ensure_volume_mounted(target)
        if not target.exists():
            raise ValueError(f"音乐路径不存在：{target}")
        command = _python_script_command(PROJECT_ROOT / "tools" / f"{name}.py", str(target))
        if args.get("recursive", True):
            command.append("--recursive")
        process = run_local_process(command, timeout=3600, cancel_event=cancel_event, on_progress=on_progress)
        output = "\n".join(part.strip() for part in (process.stdout, process.stderr) if part.strip())
        return {"ok": process.returncode == 0, "code": process.returncode, "output": output[-20000:]}
    environment = {
        "OPENAI_API_KEY": settings["api_key"], "OPENAI_BASE_URL": settings["api_base_url"],
    }
    args["_translation_model"] = settings["model"]
    with tempfile.TemporaryDirectory(prefix="walkman-agent-") as directory:
        result_path = Path(directory) / "result.json"
        if name in {"bili_list", "bili_fetch"}:
            args["_result_path"] = str(result_path)
        if name == "bili_rename" and args.get("translations"):
            translations = args["translations"]
            if not all(re.fullmatch(r"BV[A-Za-z0-9]+", key) for key in translations):
                raise ValueError("译名表的键必须是 BV 号")
            translation_path = Path(directory) / "translations.json"
            translation_path.write_text(json.dumps(translations, ensure_ascii=False), encoding="utf-8")
            args["translations"] = str(translation_path)
        result = run_action(name, args, apply=True, cancel_event=cancel_event, on_progress=on_progress, environment=environment)
        if name == "search" and result.get("ok"):
            context["track_ids"].update(str(track["id"]) for track in result.get("data", {}).get("tracks", []) if "id" in track)
        if name in {"bili_list", "bili_fetch"} and result_path.is_file():
            videos = json.loads(result_path.read_text(encoding="utf-8"))
            offset, limit = (args.get("offset", 0), args.get("limit", 100)) if name == "bili_list" else (0, 200)
            selected = videos[offset:offset + limit]
            result["data"] = {"videos": selected, "total": len(videos),
                              "next_offset": offset + limit if offset + limit < len(videos) else None}
            context["bvids"].update(video["bvid"] for video in selected if video.get("bvid") and video.get("title") != "__FAILED__")
            if name == "bili_list" and result["ok"]:
                context.setdefault("bili_lists", {})[(args["uid"], args.get("cookies", ""))] = videos
            result["output"] = f"已查询 {len(videos)} 个视频。" + (result["output"][-3000:] if not result["ok"] else "")
        return result


AGENT = AgentManager(load_settings, call_chat_message, run_agent_tool)


class WalkmanHandler(BaseHTTPRequestHandler):
    server_version = "WalkmanMusicManager/1.0"

    def _json(self, data: dict[str, Any], status: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, message: str, status: int = 400) -> None:
        self._json({"ok": False, "error": message}, status)

    def _read_json(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("请求长度无效") from exc
        if length <= 0 or length > MAX_REQUEST_BYTES:
            raise ValueError("请求为空或超过大小限制")
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("请求 JSON 格式无效") from exc
        if not isinstance(data, dict):
            raise ValueError("请求内容必须是 JSON 对象")
        return data

    def _is_local_request(self) -> bool:
        host_header = self.headers.get("Host", "")
        host = (urlsplit(f"//{host_header}").hostname or "").lower()
        if host not in {"127.0.0.1", "localhost", "::1"}:
            return False
        origin = self.headers.get("Origin")
        if not origin:
            return True
        parsed = urlsplit(origin)
        origin_host = (parsed.hostname or "").lower()
        try:
            origin_port = parsed.port
            request_port = urlsplit(f"//{host_header}").port
        except ValueError:
            return False
        return (
            parsed.scheme in {"http", "https"}
            and origin_host in {"127.0.0.1", "localhost", "::1"}
            and (origin_port or (443 if parsed.scheme == "https" else 80)) == (request_port or 80)
        )

    def do_GET(self) -> None:
        request_url = urlsplit(self.path)
        if request_url.path == "/api/usage":
            if not self._is_local_request():
                self._error("仅允许本机页面访问此服务", 403)
                return
            self._json({"ok": True, "usage": usage_snapshot()})
            return
        if request_url.path.startswith("/api/ai/jobs/"):
            if not self._is_local_request():
                self._error("仅允许本机页面访问此服务", 403)
                return
            try:
                after = int(parse_qs(request_url.query).get("after", ["0"])[0])
                self._json({"ok": True, "job": AGENT.snapshot(request_url.path.rsplit("/", 1)[-1], after=max(0, after))})
            except ValueError as exc:
                self._error(str(exc), 404)
            return
        if self.path == "/api/ai/tools":
            self._json({"ok": True, "tools": [{"name": name, "label": label} for name, label, *_ in TOOL_SPECS if name != "ask_user"]})
            return
        if self.path == "/api/health":
            settings = load_settings()
            self._json(
                {
                    "ok": True,
                    "tools": {
                        "neteasecli": bool(shutil.which("neteasecli")),
                        "ncmdump": bool(shutil.which("ncmdump")),
                        "ffmpeg": bool(shutil.which("ffmpeg")),
                    },
                    "api_key_configured": bool(settings["api_key"]),
                    "ai_configured": public_settings(settings)["ai_configured"],
                }
            )
            return
        if self.path == "/api/settings":
            self._json(public_settings(load_settings()))
            return
        paths = {
            "/": WEB_ROOT / "index.html",
            "/index.html": WEB_ROOT / "index.html",
            "/styles.css": WEB_ROOT / "styles.css",
            "/app.js": WEB_ROOT / "app.js",
            "/agent.js": WEB_ROOT / "agent.js",
        }
        path = paths.get(self.path)
        if path is None or not path.is_file():
            self.send_error(404)
            return
        mime = "text/html; charset=utf-8" if path.suffix == ".html" else "text/css; charset=utf-8" if path.suffix == ".css" else "text/javascript; charset=utf-8"
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        if not self._is_local_request():
            self._error("仅允许本机页面访问此服务", 403)
            return
        try:
            payload = self._read_json()
            if self.path == "/api/settings":
                self._save_settings(payload)
            elif self.path == "/api/ai/run":
                prompt = _as_text(payload.get("prompt"), limit=8000)
                session_id = payload.get("session_id")
                if not prompt:
                    raise ValueError("先写下你想完成的音乐任务")
                if session_id is not None and not isinstance(session_id, str):
                    raise ValueError("会话参数无效")
                if not public_settings(load_settings())["ai_configured"]:
                    raise ValueError("请先在设置中连接 AI 服务")
                self._json({"ok": True, "job": AGENT.start(prompt, session_id)}, 202)
            elif self.path == "/api/ai/cancel":
                job_id = payload.get("job_id")
                if not isinstance(job_id, str):
                    raise ValueError("任务参数无效")
                self._json({"ok": True, "job": AGENT.cancel(job_id)})
            elif self.path == "/api/ai/test":
                settings = load_settings()
                if "api_base_url" in payload:
                    base_url = _as_text(payload.get("api_base_url"), limit=2048)
                    model = _as_text(payload.get("model"), limit=200)
                    api_key = payload.get("api_key", "")
                    if not base_url or not model or not isinstance(api_key, str) or len(api_key) > 1000:
                        raise ValueError("请检查 API 地址、模型名称和 API Key")
                    settings["api_base_url"] = validate_base_url(base_url)
                    settings["model"] = model
                    if payload.get("clear_api_key") is True:
                        settings["api_key"] = ""
                    elif api_key.strip():
                        settings["api_key"] = api_key.strip()
                message = call_chat_message(
                    settings,
                    [
                        {"role": "system", "content": "You are a connection check. Reply with exactly: OK"},
                        {"role": "user", "content": "Reply OK."},
                    ],
                    timeout=30,
                    tools=TOOLS[:1],
                )
                content = message.get("content") or ("OK" if message.get("tool_calls") else "")
                if not isinstance(content, str) or not content.strip():
                    raise RuntimeError("AI 服务返回了空响应")
                self._json({"ok": True, "message": content[:100]})
            elif self.path == "/api/search":
                result = run_search(_as_text(payload.get("query"), limit=300), payload.get("limit", 20))
                self._json({"ok": True, "data": result})
            elif self.path in {"/api/action/preview", "/api/action/execute"}:
                action = payload.get("action")
                if not isinstance(action, str) or action not in {"download", "convert", "repair", "album", "search", "bili_list", "bili_fetch", "bili_download", "bili_rename", "bili_copy"}:
                    raise ValueError("不支持的操作")
                args = payload.get("args")
                if not isinstance(args, dict):
                    raise ValueError("操作参数无效")
                result = run_action(action, args, apply=self.path.endswith("execute"))
                self._json(result, 200 if result["ok"] else 400)
            else:
                self._error("未知 API 路径", 404)
        except ValueError as exc:
            self._error(str(exc), 400)
        except subprocess.TimeoutExpired as exc:
            self._error(f"操作超时（{exc.timeout} 秒），请查看命令行是否仍有进程运行", 504)
        except (RuntimeError, OSError) as exc:
            self._error(str(exc), 502)

    def _save_settings(self, payload: dict[str, Any]) -> None:
        current = load_settings()
        base_url = _as_text(payload.get("api_base_url"), limit=2048)
        model = _as_text(payload.get("model"), limit=200)
        library_dir = _as_text(payload.get("library_dir"), limit=4096)
        new_key = payload.get("api_key", "")
        if not isinstance(new_key, str) or len(new_key) > 1000:
            raise ValueError("API Key 格式无效")
        if not base_url or not model or not library_dir:
            raise ValueError("请填写 API 地址、模型名称和默认音乐文件夹")
        current["api_base_url"] = validate_base_url(base_url)
        current["model"] = model
        current["library_dir"] = library_dir
        if not Path(library_dir).expanduser().is_absolute():
            raise ValueError("默认音乐文件夹必须是绝对路径或 ~/ 路径")
        current["library_dir"] = str(Path(library_dir).expanduser())
        if "bilibili_cookies" in payload:
            current["bilibili_cookies"] = _as_text(payload.get("bilibili_cookies"), limit=4096)
            if current["bilibili_cookies"]:
                cookie_path = Path(current["bilibili_cookies"]).expanduser()
                if not cookie_path.is_absolute():
                    raise ValueError("Cookie 文件必须是绝对路径或 ~/ 路径")
                current["bilibili_cookies"] = str(cookie_path)
        if payload.get("clear_api_key") is True:
            current["api_key"] = ""
        elif new_key.strip():
            current["api_key"] = new_key.strip()
        save_settings(current)
        self._json({"ok": True, "settings": public_settings(current)})

    def log_message(self, format_string: str, *args: Any) -> None:
        # Keep the console quiet during normal browsing; errors remain visible.
        if len(args) < 2:
            return
        try:
            status = int(args[1])
        except (TypeError, ValueError):
            return
        if status >= 400:
            super().log_message(format_string, *args)


def main() -> int:
    if getattr(sys, "frozen", False):
        _prepare_gui_path()

    parser = argparse.ArgumentParser(description="Start the local Walkman Music Manager web app.")
    parser.add_argument("--host", default="127.0.0.1", help="Local bind address")
    parser.add_argument("--port", type=int, default=8765, help="Local port")
    parser.add_argument(
        "--ready-file",
        type=Path,
        help="Write the local URL here after the server has started",
    )
    args = parser.parse_args()
    if args.host not in {"127.0.0.1", "localhost", "::1"}:
        parser.error("为保护本机 API 配置，只允许绑定到 loopback 地址")
    try:
        server = ThreadingHTTPServer((args.host, args.port), WalkmanHandler)
    except OSError:
        if not getattr(sys, "frozen", False) or args.port == 0:
            raise
        # A terminal-launched development server may already use the default port.
        server = ThreadingHTTPServer((args.host, 0), WalkmanHandler)
    display_host = "localhost" if args.host in {"localhost", "::1"} else "127.0.0.1"
    url = f"http://{display_host}:{server.server_port}"
    if args.ready_file:
        args.ready_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.ready_file.with_name(f".{args.ready_file.name}.tmp")
        temporary.write_text(url, encoding="utf-8")
        temporary.replace(args.ready_file)
    def stop_server(signum, frame):
        raise KeyboardInterrupt()

    signal.signal(signal.SIGTERM, stop_server)
    if sys.stdout is not None:
        print(f"Walkman Music Manager 已启动：{url}")
        print("按 Ctrl+C 停止。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        if sys.stdout is not None:
            print("\n服务已停止。")
    finally:
        AGENT.cancel_all()
        with PROCESS_LOCK:
            active = list(ACTIVE_PROCESSES)
        for process in active:
            stop_local_process(process)
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
