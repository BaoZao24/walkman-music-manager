"""Provider-reported token usage, shared by the app and its CLI processes."""
from __future__ import annotations

import contextlib
import contextvars
import json
import os
import tempfile
import threading
import sys
from datetime import datetime
from pathlib import Path

try:
    import fcntl
except ImportError:
    fcntl = None


TASK_ID = contextvars.ContextVar("walkman_usage_task", default="")
STORE_LOCK = threading.RLock()
STORAGE_ERROR = ""
COUNTERS = (
    "requests", "reported_requests", "unreported_requests", "partial_requests",
    "input_tokens", "output_tokens", "total_tokens", "cached_tokens", "reasoning_tokens",
)


def empty_usage():
    return dict.fromkeys(COUNTERS, 0)


def current_task_id():
    return TASK_ID.get() or os.environ.get("WALKMAN_USAGE_TASK_ID", "")


@contextlib.contextmanager
def usage_task(task_id):
    token = TASK_ID.set(task_id)
    try:
        yield
    finally:
        TASK_ID.reset(token)


def _count(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def normalize_usage(raw):
    """Keep cache/reasoning as subsets; never add them to the token total."""
    raw = raw if isinstance(raw, dict) else {}
    inputs = _count(raw.get("prompt_tokens", raw.get("input_tokens")))
    outputs = _count(raw.get("completion_tokens", raw.get("output_tokens")))
    total = _count(raw.get("total_tokens"))
    if total is None and inputs is not None and outputs is not None:
        total = inputs + outputs
    reported = any(value is not None for value in (inputs, outputs, total))
    input_details = raw.get("prompt_tokens_details") or raw.get("input_tokens_details") or {}
    output_details = raw.get("completion_tokens_details") or raw.get("output_tokens_details") or {}
    input_details = input_details if isinstance(input_details, dict) else {}
    output_details = output_details if isinstance(output_details, dict) else {}
    cached = _count(input_details.get("cached_tokens", raw.get("prompt_cache_hit_tokens")))
    reasoning = _count(output_details.get("reasoning_tokens", raw.get("reasoning_tokens")))
    return {
        "requests": 1, "reported_requests": int(reported), "unreported_requests": int(not reported),
        "partial_requests": int(reported and any(value is None for value in (inputs, outputs, total))),
        "input_tokens": inputs or 0, "output_tokens": outputs or 0,
        "total_tokens": total if total is not None else (inputs or 0) + (outputs or 0),
        "cached_tokens": min(cached or 0, inputs or 0),
        "reasoning_tokens": min(reasoning or 0, outputs or 0),
    }


def _store_path():
    folder = os.environ.get("WALKMAN_APP_CONFIG_DIR")
    return (Path(folder).expanduser() if folder else Path.home() / ".config" / "walkman-music-manager") / "token-usage.json"


@contextlib.contextmanager
def _locked(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with STORE_LOCK:
        descriptor = os.open(path.with_suffix(".lock"), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            if fcntl:
                fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            if fcntl:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)


def _load(path):
    if not path.exists():
        return {"version": 1, "since": "", "updated_at": "", "total": empty_usage(), "days": {}, "models": {}, "sources": {}, "tasks": {}}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("version") != 1:
        raise ValueError("用量记录格式无效")
    for key in ("total", "days", "models", "tasks"):
        if not isinstance(data.get(key), dict):
            raise ValueError("用量记录格式无效")
    data.setdefault("sources", {})
    if not isinstance(data["sources"], dict):
        raise ValueError("用量记录格式无效")
    for counter in [data["total"], *data["days"].values(), *data["models"].values(), *data["sources"].values(), *data["tasks"].values()]:
        if not isinstance(counter, dict) or any(_count(counter.get(key)) is None for key in COUNTERS):
            raise ValueError("用量记录计数无效")
    return data


def _save(path, data):
    descriptor, name = tempfile.mkstemp(prefix=".token-usage-", suffix=".json", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def record_usage(raw, *, model, source):
    """Persist counts only. Prompts, lyrics, keys and endpoints are never stored."""
    global STORAGE_ERROR
    usage = normalize_usage(raw)
    path = _store_path()
    now = datetime.now().astimezone()
    task_id = current_task_id()
    try:
        with _locked(path):
            data = _load(path)
            data["since"] = data["since"] or now.isoformat(timespec="seconds")
            data["updated_at"] = now.isoformat(timespec="seconds")
            counters = [data["total"], data["days"].setdefault(now.date().isoformat(), empty_usage()),
                        data["models"].setdefault(str(model)[:200], empty_usage()),
                        data["sources"].setdefault(source, empty_usage())]
            if task_id:
                counters.append(data["tasks"].setdefault(task_id, empty_usage()))
            for counter in counters:
                for key in COUNTERS:
                    counter[key] += usage[key]
            # All-time counts remain intact when old task/day detail is pruned.
            for collection, limit in (("tasks", 500), ("days", 400)):
                while len(data[collection]) > limit:
                    del data[collection][next(iter(data[collection]))]
            _save(path, data)
            STORAGE_ERROR = ""
    except (OSError, ValueError, TypeError) as exc:
        STORAGE_ERROR = usage["storage_error"] = f"用量记录未保存：{exc}"
        print(STORAGE_ERROR, file=sys.stderr)
    return usage


def usage_snapshot(task_id=""):
    path = _store_path()
    try:
        with _locked(path):
            data = _load(path)
        return {
            "total": data["total"], "today": data["days"].get(datetime.now().astimezone().date().isoformat(), empty_usage()),
            "task": data["tasks"].get(task_id, empty_usage()), "models": data["models"], "sources": data["sources"],
            "since": data["since"], "updated_at": data["updated_at"], "storage_error": STORAGE_ERROR,
        }
    except (OSError, ValueError, TypeError) as exc:
        return {"total": empty_usage(), "today": empty_usage(), "task": empty_usage(), "models": {}, "sources": {},
                "since": "", "updated_at": "", "storage_error": f"用量记录暂不可用：{exc}"}
