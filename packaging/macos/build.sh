#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
APP_NAME="Walkman Music Manager"
APP_PATH="${APP_DEST:-$HOME/WalkmanBuilds/$APP_NAME.app}"
RELEASE_LINK="$ROOT/release/$APP_NAME.app"
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

mkdir -p "$BUILD_ROOT" "$(dirname "$APP_PATH")" "$ROOT/release"
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

DATA_ARGS=()
for source in "$ROOT"/*.py; do
  DATA_ARGS+=(--add-data "$source:.")
done
for source in "$ROOT/tools"/*.py; do
  DATA_ARGS+=(--add-data "$source:tools")
done

"$BUILD_PYTHON" -m PyInstaller \
  --noconfirm --clean --onefile --console \
  --name WalkmanServer \
  --distpath "$BUILD_ROOT/server-dist" \
  --workpath "$BUILD_ROOT/server-work" \
  --specpath "$BUILD_ROOT/server-spec" \
  --add-data "$ROOT/web:web" \
  "${DATA_ARGS[@]}" \
  "$ROOT/web_app.py"

"$BUILD_PYTHON" -m PyInstaller \
  --noconfirm --clean --onefile --console \
  --name WalkmanCLI \
  --distpath "$BUILD_ROOT/cli-dist" \
  --workpath "$BUILD_ROOT/cli-work" \
  --specpath "$BUILD_ROOT/cli-spec" \
  "${DATA_ARGS[@]}" \
  "$ROOT/packaging/macos/cli_entry.py"

STAGING_PATH="${APP_PATH%.app}.building-$$.app"
mkdir -p "$STAGING_PATH/Contents/MacOS" "$STAGING_PATH/Contents/Resources" "$STAGING_PATH/Contents/Helpers"
cp "$ROOT/packaging/macos/Info.plist" "$STAGING_PATH/Contents/Info.plist"
cp "$BUILD_ROOT/WalkmanIcon.icns" "$STAGING_PATH/Contents/Resources/WalkmanIcon.icns"
cp "$BUILD_ROOT/server-dist/WalkmanServer" "$STAGING_PATH/Contents/Helpers/WalkmanServer"
cp "$BUILD_ROOT/cli-dist/WalkmanCLI" "$STAGING_PATH/Contents/Helpers/WalkmanCLI"
chmod 755 "$STAGING_PATH/Contents/Helpers/WalkmanServer" "$STAGING_PATH/Contents/Helpers/WalkmanCLI"
swiftc -O -swift-version 5 -parse-as-library -framework AppKit -framework WebKit \
  "$ROOT/packaging/macos/WalkmanApp.swift" \
  -o "$STAGING_PATH/Contents/MacOS/$APP_NAME"

xattr -cr "$STAGING_PATH"
codesign --force --sign - "$STAGING_PATH/Contents/Helpers/WalkmanServer"
codesign --force --sign - "$STAGING_PATH/Contents/Helpers/WalkmanCLI"
codesign --force --deep --sign - "$STAGING_PATH"
codesign --verify --deep --strict "$STAGING_PATH"

if [[ -e "$APP_PATH" || -L "$APP_PATH" ]]; then
  BACKUP_PATH="${APP_PATH%.app}.previous-$(date +%Y%m%d-%H%M%S).app"
  mv "$APP_PATH" "$BACKUP_PATH"
  echo "Previous app preserved at: $BACKUP_PATH"
fi
mv "$STAGING_PATH" "$APP_PATH"

if [[ -e "$RELEASE_LINK" && ! -L "$RELEASE_LINK" ]]; then
  echo "Release destination already exists and is not a symlink: $RELEASE_LINK" >&2
  exit 1
fi
ln -sfn "$APP_PATH" "$RELEASE_LINK"

echo "Built native macOS app: $RELEASE_LINK -> $APP_PATH"
