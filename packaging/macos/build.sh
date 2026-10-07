#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
APP_NAME="Walkman Music Manager"
APP_PATH="${APP_DEST:-$HOME/Applications/$APP_NAME.app}"
BUILD_ROOT="$ROOT/build/macos"
BUILD_VENV="$ROOT/.venv-build"
BUILD_PYTHON="$BUILD_VENV/bin/python"

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "This build script creates a macOS app and must run on macOS." >&2
  exit 1
fi

if [[ ! -x "$BUILD_PYTHON" ]]; then
  python3 -m venv "$BUILD_VENV"
fi
"$BUILD_PYTHON" -m pip install --disable-pip-version-check -r "$ROOT/packaging/macos/requirements.txt"

mkdir -p "$BUILD_ROOT" "$(dirname "$APP_PATH")"
swift "$ROOT/packaging/macos/make_icon.swift" "$BUILD_ROOT/WalkmanIcon.png"
ICONSET="$BUILD_ROOT/Walkman.iconset"
mkdir -p "$ICONSET"
for size in 16 32 128 256 512; do
  sips -z "$size" "$size" "$BUILD_ROOT/WalkmanIcon.png" \
    --out "$ICONSET/icon_${size}x${size}.png" >/dev/null
  doubled=$((size * 2))
  sips -z "$doubled" "$doubled" "$BUILD_ROOT/WalkmanIcon.png" \
    --out "$ICONSET/icon_${size}x${size}@2x.png" >/dev/null
done
iconutil --convert icns --output "$BUILD_ROOT/WalkmanIcon.icns" "$ICONSET"

"$BUILD_PYTHON" -m PyInstaller \
  --noconfirm --clean --windowed --onedir \
  --name "$APP_NAME" \
  --osx-bundle-identifier "com.walkman.music-manager" \
  --icon "$BUILD_ROOT/WalkmanIcon.icns" \
  --distpath "$BUILD_ROOT/app-dist" \
  --workpath "$BUILD_ROOT/app-work" \
  --specpath "$BUILD_ROOT/app-spec" \
  --add-data "$ROOT/web:web" \
  "$ROOT/web_app.py"

DATA_ARGS=()
for source in "$ROOT"/*.py; do
  DATA_ARGS+=(--add-data "$source:.")
done

"$BUILD_PYTHON" -m PyInstaller \
  --noconfirm --clean --onefile --console \
  --name WalkmanCLI \
  --distpath "$BUILD_ROOT/cli-dist" \
  --workpath "$BUILD_ROOT/cli-work" \
  --specpath "$BUILD_ROOT/cli-spec" \
  "${DATA_ARGS[@]}" \
  "$ROOT/packaging/macos/cli_entry.py"

ditto "$BUILD_ROOT/app-dist/$APP_NAME.app" "$APP_PATH"
cp "$BUILD_ROOT/cli-dist/WalkmanCLI" "$APP_PATH/Contents/MacOS/WalkmanCLI"
chmod 755 "$APP_PATH/Contents/MacOS/WalkmanCLI"
xattr -cr "$APP_PATH"
codesign --force --deep --sign - "$APP_PATH"
codesign --verify --deep --strict "$APP_PATH"

echo "Built macOS app: $APP_PATH"
