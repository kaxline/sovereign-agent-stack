#!/usr/bin/env bash
# Build the Messages reader app. See imessage/README.md.
#
#   imessage/macos/build.sh [out-dir]        # default: imessage/build
#
# The app carries only the indexer and watcher scripts, sealed by its code
# signature, plus a small launcher that runs them with an isolated Python:
#
#   Assistant Messages Reader.app   messages-index.py, messages_index.py,
#                                   messages_watch.py, typedstream.py
#
# It holds no send code and asks for no Apple events. It needs Full Disk
# Access, which you grant it in System Settings.
#
# Environment:
#   MESSAGES_APP_PYTHON         Python 3.11+ to run the helper (default: python3 on PATH)
#   MESSAGES_APP_BUNDLE_PREFIX  bundle id prefix (default: local.assistant)
#   MESSAGES_APP_SIGN_IDENTITY  codesign identity (default: - for ad hoc). macOS
#                               keys permissions to the signature, so with ad hoc
#                               signing every rebuild needs Full Disk Access again.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
SRC="$(cd "${HERE}/.." && pwd)"
OUT="${1:-${SRC}/build}"
PREFIX="${MESSAGES_APP_BUNDLE_PREFIX:-local.assistant}"
IDENTITY="${MESSAGES_APP_SIGN_IDENTITY:--}"
VERSION="$(git -C "$SRC" describe --tags --always --dirty 2>/dev/null || echo dev)"
NAME="Assistant Messages Reader"
EXE="assistant-messages-reader"

die() { printf 'build: %s\n' "$1" >&2; exit 1; }

[[ "$(uname -s)" == "Darwin" ]] || die "the Messages app is macOS only"
command -v swiftc >/dev/null || die "swiftc not found; install the Xcode command line tools"

PYTHON="${MESSAGES_APP_PYTHON:-$(command -v python3 || true)}"
[[ -n "$PYTHON" ]] || die "no python3 found; set MESSAGES_APP_PYTHON"
# Record the path the interpreter reports for itself: past any shim, and for
# Homebrew the opt/ link, which survives patch upgrades.
PYTHON="$("$PYTHON" -I -c 'import sys; print(sys.executable)')"
"$PYTHON" -I -c 'import sys; sys.exit(sys.version_info < (3, 11))' \
  || die "${PYTHON} is older than 3.11; set MESSAGES_APP_PYTHON"

WORK="$(mktemp -d "${TMPDIR:-/tmp}/messages-app.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT
swiftc -O -o "${WORK}/launcher" "${HERE}/Launcher.swift"
mkdir -p "$OUT"

app="${OUT}/${NAME}.app"
rm -rf "$app"
mkdir -p "${app}/Contents/MacOS" "${app}/Contents/Resources/scripts/lib"
cp "${WORK}/launcher" "${app}/Contents/MacOS/${EXE}"
cp "${SRC}/messages-index.py" "${app}/Contents/Resources/scripts/messages-index.py"
for lib in messages_index.py messages_watch.py typedstream.py; do
  cp "${SRC}/lib/${lib}" "${app}/Contents/Resources/scripts/lib/${lib}"
done
printf '{"python": "%s", "entry": "%s"}\n' "$PYTHON" "messages-index.py" > "${app}/Contents/Resources/launcher.json"
cat > "${app}/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleIdentifier</key><string>${PREFIX}.messages-reader</string>
  <key>CFBundleName</key><string>${NAME}</string>
  <key>CFBundleDisplayName</key><string>${NAME}</string>
  <key>CFBundleExecutable</key><string>${EXE}</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>${VERSION}</string>
  <key>CFBundleVersion</key><string>${VERSION}</string>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
  <key>LSUIElement</key><true/>
</dict>
</plist>
PLIST
codesign --force --sign "$IDENTITY" --options runtime --timestamp=none "$app" 2>&1 \
  | { grep -v 'replacing existing signature' >&2 || true; }
codesign --verify --strict "$app"
printf 'built %s (%s.messages-reader)\n' "$app" "$PREFIX"
printf 'python: %s\nsigned: %s\n' "$PYTHON" "$( [[ "$IDENTITY" == "-" ]] && echo "ad hoc" || echo "$IDENTITY")"
