"""Shared NetEase client discovery and isolated Node requests."""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path


def run_node(body: str) -> str:
    executable = shutil.which("neteasecli")
    candidates = []
    if executable:
        candidates.extend(Path(executable).resolve().parents)
    candidates.extend([
        Path("/opt/homebrew/lib/node_modules/neteasecli"),
        Path("/usr/local/lib/node_modules/neteasecli"),
    ])
    package = next((path for path in candidates if (path / "dist/api/client.js").is_file()), None)
    if package is None:
        raise RuntimeError("找不到 neteasecli 的本机 API 模块，请先安装 neteasecli")
    if not shutil.which("node"):
        raise RuntimeError("找不到 node，请先安装 Node.js")
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", suffix=".mjs", prefix="walkman-netease-", delete=False) as handle:
        script = Path(handle.name)
        handle.write(
            "import { getApiClient } from " + json.dumps((package / "dist/api/client.js").as_uri()) + ";\n"
            "const c = getApiClient();\n" + body
        )
    try:
        process = subprocess.run(["node", str(script)], capture_output=True, text=True, cwd=package, timeout=60)
        if process.returncode:
            raise RuntimeError("网易云查询失败：" + process.stderr.strip()[-1500:])
        lines = process.stdout.strip().splitlines()
        return lines[-1] if lines else ""
    finally:
        script.unlink(missing_ok=True)
