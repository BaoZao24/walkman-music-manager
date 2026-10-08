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
import subprocess
import sys
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from download_music import ensure_volume_mounted


PROJECT_ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)).resolve()
WEB_ROOT = PROJECT_ROOT / "web"
MAX_REQUEST_BYTES = 256_000
DEFAULT_LIBRARY_DIR = Path.home() / "Music" / "Walkman"
DEFAULT_SETTINGS = {
    "api_base_url": "https://api.openai.com/v1",
    "model": "gpt-4o-mini",
    "api_key": "",
    "library_dir": str(DEFAULT_LIBRARY_DIR),
}
QUALITIES = {"standard", "higher", "exhigh", "lossless", "hires"}
LYRICS_MODES = {"original", "translated", "bilingual"}
TRACK_ID_RE = re.compile(r"^\d+$")


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


def call_chat_api(
    settings: dict[str, str], messages: list[dict[str, str]], timeout: int = 60
) -> str:
    base_url = validate_base_url(settings["api_base_url"])
    if not settings["model"].strip():
        raise ValueError("请先设置模型名称")
    payload = json.dumps(
        {
            "model": settings["model"],
            "messages": messages,
            "temperature": 0.1,
        }
    ).encode("utf-8")
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if settings["api_key"]:
        headers["Authorization"] = f"Bearer {settings['api_key']}"
    request = Request(chat_completions_url(base_url), data=payload, headers=headers)
    try:
        with urlopen(request, timeout=timeout) as response:
            result = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read(3000).decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"AI 服务返回 HTTP {exc.code}" + (f": {detail}" if detail else "")) from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise RuntimeError(f"连接 AI 服务失败：{exc}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError("AI 服务没有返回有效 JSON") from exc

    try:
        content = result["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError("AI 服务响应中没有 choices[0].message.content") from exc
    if isinstance(content, list):
        content = "".join(
            str(part.get("text", ""))
            for part in content
            if isinstance(part, dict)
        )
    if not isinstance(content, str) or not content.strip():
        raise RuntimeError("AI 服务返回了空内容")
    return content.strip()


def extract_json_object(content: str) -> dict[str, Any]:
    candidate = content.strip()
    if candidate.startswith("```"):
        candidate = re.sub(r"^```(?:json)?\s*|\s*```$", "", candidate, flags=re.I)
    start, end = candidate.find("{"), candidate.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("AI 没有返回任务 JSON，请重试或换一种说法")
    try:
        result = json.loads(candidate[start : end + 1])
    except json.JSONDecodeError as exc:
        raise ValueError("AI 返回的任务格式无法解析，请重试") from exc
    if not isinstance(result, dict):
        raise ValueError("AI 返回的任务格式无效")
    return result


def _as_text(value: Any, *, limit: int = 4096) -> str:
    return value.strip()[:limit] if isinstance(value, str) else ""


def _as_questions(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [_as_text(item, limit=300) for item in value if _as_text(item, limit=300)][:5]


def normalize_plan(raw: dict[str, Any], library_dir: str) -> dict[str, Any]:
    action = raw.get("action")
    if not isinstance(action, str) or action not in {
        "search", "download", "convert", "repair", "album", "bili_list",
        "bili_fetch", "bili_download", "bili_rename", "bili_copy", "none",
    }:
        raise ValueError("AI 建议了不支持的操作，请换一种说法")
    raw_args = raw.get("args")
    args = raw_args if isinstance(raw_args, dict) else {}
    summary = _as_text(raw.get("summary"), limit=500) or "已生成一个待确认的音乐整理任务。"
    questions = _as_questions(raw.get("questions"))
    normalized: dict[str, Any] = {}

    if action == "search":
        try:
            limit = int(args.get("limit", 10) or 10)
        except (TypeError, ValueError):
            limit = 10
        normalized = {
            "query": _as_text(args.get("query"), limit=300),
            "limit": max(1, min(50, limit)),
        }
        if not normalized["query"]:
            questions.append("你想搜索哪首歌、哪位歌手或什么关键词？")
    elif action == "download":
        ids = args.get("track_ids", [])
        if isinstance(ids, str):
            ids = re.split(r"[\s,，]+", ids)
        if not isinstance(ids, list):
            ids = []
        normalized = {
            "track_ids": list(dict.fromkeys(str(item).strip() for item in ids if TRACK_ID_RE.fullmatch(str(item).strip())))[:200],
            "query": _as_text(args.get("query"), limit=300),
            "output_dir": _as_text(args.get("output_dir"), limit=4096) or library_dir,
            "quality": args.get("quality") if isinstance(args.get("quality"), str) and args.get("quality") in QUALITIES else "exhigh",
            "lyrics": args.get("lyrics") if isinstance(args.get("lyrics"), str) and args.get("lyrics") in LYRICS_MODES else "translated",
        }
        if not normalized["track_ids"]:
            questions.append("请先在搜索结果里选歌，或提供网易云音乐歌曲 ID。")
    elif action == "convert":
        normalized = {
            "source": _as_text(args.get("source"), limit=4096),
            "output_dir": _as_text(args.get("output_dir"), limit=4096) or library_dir,
            "flat": args.get("flat") is True,
        }
        if not normalized["source"]:
            questions.append("请提供 .ncm 文件或文件夹的本机路径。")
    elif action == "repair":
        normalized = {"root": _as_text(args.get("root"), limit=4096) or library_dir}
    elif action == "album":
        normalized = {
            "source": _as_text(args.get("source"), limit=4096),
            "output_dir": _as_text(args.get("output_dir"), limit=4096) or library_dir,
            "album_name": _as_text(args.get("album_name"), limit=300),
        }
        if not normalized["source"]:
            questions.append("请提供要整理的源文件夹路径。")
        if not normalized["album_name"]:
            questions.append("请提供目标专辑文件夹名称。")
    elif action == "bili_list":
        try:
            uid = int(args.get("uid", 0))
        except (TypeError, ValueError):
            uid = 0
        normalized = {
            "uid": uid,
            "cookies": _as_text(args.get("cookies"), limit=4096) or "www.bilibili.com_cookies.txt",
        }
        if uid <= 0:
            questions.append("请提供 B 站 UP 主的 UID。")
    elif action in {"bili_fetch", "bili_download"}:
        try:
            bvids = normalize_bvids(args.get("bvids", []))
        except ValueError:
            bvids = []
        normalized = {
            "bvids": bvids,
            "cookies": _as_text(args.get("cookies"), limit=4096) or "www.bilibili.com_cookies.txt",
        }
        if action == "bili_download":
            normalized.update({
                "output_dir": _as_text(args.get("output_dir"), limit=4096) or str(Path(library_dir) / "Bilibili-Staging"),
                "no_proxy": args.get("no_proxy") is True,
            })
        if not bvids:
            questions.append("请提供 B 站 BV 号或视频链接。")
    elif action == "bili_rename":
        normalized = {
            "source": _as_text(args.get("source"), limit=4096),
            "translations": _as_text(args.get("translations"), limit=4096),
        }
        if not normalized["source"]:
            questions.append("请提供已下载翻唱所在的暂存文件夹。")
    elif action == "bili_copy":
        normalized = {
            "source": _as_text(args.get("source"), limit=4096),
            "dest": _as_text(args.get("dest"), limit=4096) or library_dir,
        }
        if not normalized["source"]:
            questions.append("请提供翻唱暂存文件夹。")

    return {
        "action": action,
        "summary": summary,
        "args": normalized,
        "questions": list(dict.fromkeys(questions))[:5],
    }


def build_plan(prompt: str, settings: dict[str, str]) -> dict[str, Any]:
    library_dir = settings["library_dir"]
    system_message = f"""你是 Walkman Music Manager 的音乐工作流规划器。根据用户要求，把任务映射到一个本机操作。
只返回一个 JSON 对象，不要 Markdown，不要解释。结构：
{{"action":"search|download|convert|repair|album|bili_list|bili_fetch|bili_download|bili_rename|bili_copy|none","summary":"简短中文说明","args":{{}},"questions":[]}}

规则：
- 只能规划 search、download、convert、repair、album、bili_list、bili_fetch、bili_download、bili_rename、bili_copy、none；不要规划 shell、删除、覆盖、递归整理等操作。
- search 参数：query 字符串、limit 整数（默认 10）。
- download 参数：track_ids 数字 ID 数组、可选 query、output_dir、quality、lyrics。没有明确 ID 时优先用 search（如果用户说的是找歌）；没有歌曲关键词时在 questions 中要求先搜索选歌。
- convert 参数：source 本机路径、output_dir、flat 布尔值。source 未提供时在 questions 中询问，禁止编造路径。
- repair 参数：root 本机路径；用户没有给 root 时可使用默认音乐目录 {library_dir}。
- album 参数：source 本机路径、output_dir、album_name。source 或专辑名未提供时在 questions 中询问，禁止编造路径。
- bili_list 参数：uid（只能使用用户提供的数字 UID）、cookies（默认 www.bilibili.com_cookies.txt）。
- bili_fetch 参数：bvids（来自用户文字的 BV 号或视频链接数组）、cookies。
- bili_download 参数：bvids、output_dir、cookies、no_proxy。
- bili_rename 参数：source、可选 translations JSON 路径。
- bili_copy 参数：source、dest（默认音乐目录）。
- output_dir 未指定时使用默认音乐目录 {library_dir}。
- 网易云歌曲 ID 必须来自用户文字；不得猜测 ID。不要把自然语言描述当路径。
- 如果任务与这些操作无关，使用 action=none，并在 questions 中简短说明。
- 任何任务都只是建议；用户会先查看预览，再单独确认执行。"""
    content = call_chat_api(
        settings,
        [
            {"role": "system", "content": system_message},
            {"role": "user", "content": prompt[:8000]},
        ],
        timeout=90,
    )
    return normalize_plan(extract_json_object(content), library_dir)


def run_search(query: str, limit: int) -> dict[str, Any]:
    query = query.strip()
    if not query:
        raise ValueError("请输入歌曲名、歌手或关键词")
    try:
        limit = max(1, min(100, int(limit)))
    except (TypeError, ValueError) as exc:
        raise ValueError("搜索数量必须是 1 到 100 之间的整数") from exc
    process = subprocess.run(
        _python_script_command(PROJECT_ROOT / "search_music.py", query, "--limit", str(limit), "--json"),
        cwd=_process_cwd(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=90,
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
        return [str(cli), Path(script).name, *arguments]
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


def run_action(action: str, args: dict[str, Any], apply: bool) -> dict[str, Any]:
    if action == "search":
        result = run_search(str(args.get("query", "")), args.get("limit", 10))
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
        process = subprocess.run(
            command, cwd=_process_cwd(), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=600,
        )
        output = "\n".join(part.strip() for part in (process.stdout, process.stderr) if part.strip())
        return {"ok": process.returncode == 0, "code": process.returncode, "output": output[-30000:] or "查询完成。", "read_only": True}
    command = action_command(action, args, apply)
    timeout = 3600 if action == "bili_download" else 900 if action in {"download", "convert"} else 300
    process = subprocess.run(
        command,
        cwd=_process_cwd(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
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
            elif self.path == "/api/ai/plan":
                prompt = _as_text(payload.get("prompt"), limit=8000)
                if not prompt:
                    raise ValueError("先写下你想完成的音乐任务")
                plan = build_plan(prompt, load_settings())
                self._json({"ok": True, "plan": plan})
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
                content = call_chat_api(
                    settings,
                    [
                        {"role": "system", "content": "You are a connection check. Reply with exactly: OK"},
                        {"role": "user", "content": "Reply OK."},
                    ],
                    timeout=30,
                )
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
    if sys.stdout is not None:
        print(f"Walkman Music Manager 已启动：{url}")
        print("按 Ctrl+C 停止。")
    if getattr(sys, "frozen", False):
        import webbrowser

        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        if sys.stdout is not None:
            print("\n服务已停止。")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
