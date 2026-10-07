#!/usr/bin/env python3
"""Console helper used by the frozen macOS app to run existing CLI commands."""

from __future__ import annotations

import runpy
import sys
from pathlib import Path


ALLOWED_SCRIPTS = {"search_music.py", "walkman.py"}


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] not in ALLOWED_SCRIPTS:
        print("用法：WalkmanCLI <search_music.py|walkman.py> [参数…]", file=sys.stderr)
        return 2

    project_root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    script = project_root / sys.argv[1]
    if not script.is_file():
        print(f"应用中缺少脚本：{script.name}", file=sys.stderr)
        return 2

    sys.path.insert(0, str(project_root))
    sys.argv = [str(script), *sys.argv[2:]]
    runpy.run_path(str(script), run_name="__main__")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
