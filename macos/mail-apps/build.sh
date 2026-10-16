#!/usr/bin/env bash
# Build the two mail helper apps. See docs/email.md.
#
#   macos/mail-apps/build.sh [out-dir]        # default: build/mail-apps
#
# Each app carries only its own helper's scripts, sealed by its code
# signature, plus a small launcher that runs them with an isolated Python:
#
#   Assistant Mail Reader.app   scripts/mail-index.py and the indexer library
#   Assistant Mail Drafts.app   scripts/mail-drafts.py and the drafts library
#
# Environment:
#   MAIL_APPS_PYTHON         Python 3.11+ to run the helpers (default: python3 on PATH)
#   MAIL_APPS_BUNDLE_PREFIX  bundle id prefix (default: local.assistant)
#   MAIL_APPS_SIGN_IDENTITY  codesign identity (default: - for ad hoc). macOS
#                            keys permissions to the signature, so with ad hoc
#                            signing every rebuild asks for them again.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
HERE="${ROOT}/macos/mail-apps"
OUT="${1:-${ROOT}/build/mail-apps}"
PREFIX="${MAIL_APPS_BUNDLE_PREFIX:-local.assistant}"
IDENTITY="${MAIL_APPS_SIGN_IDENTITY:--}"
VERSION="$(git -C "$ROOT" describe --tags --always --dirty 2>/dev/null || echo dev)"

die() { printf 'build: %s\n' "$1" >&2; exit 1; }

[[ "$(uname -s)" == "Darwin" ]] || die "the mail apps are macOS only"
command -v swiftc >/dev/null || die "swiftc not found; install the Xcode command line tools"

PYTHON="${MAIL_APPS_PYTHON:-$(command -v python3 || true)}"
[[ -n "$PYTHON" ]] || die "no python3 found; set MAIL_APPS_PYTHON"
# Record the path the interpreter reports for itself: past any shim, and for
# Homebrew the opt/ link, which survives patch upgrades.
PYTHON="$("$PYTHON" -I -c 'import sys; print(sys.executable)')"
"$PYTHON" -I -c 'import sys; sys.exit(sys.version_info < (3, 11))' \
  || die "${PYTHON} is older than 3.11; set MAIL_APPS_PYTHON"

WORK="$(mktemp -d "${TMPDIR:-/tmp}/mail-apps.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT
swiftc -O -o "${WORK}/launcher" "${HERE}/Launcher.swift"
mkdir -p "$OUT"

# make_app <name> <id-suffix> <exe-name> <entry> <usage> <src:dest>...
make_app() {
  local name="$1" suffix="$2" exe="$3" entry="$4" usage="$5"
  shift 5
  local app="${OUT}/${name}.app"
  rm -rf "$app"
  mkdir -p "${app}/Contents/MacOS" "${app}/Contents/Resources/scripts/lib"
  cp "${WORK}/launcher" "${app}/Contents/MacOS/${exe}"
  local pair
  for pair in "$@"; do
    cp "${ROOT}/${pair%%:*}" "${app}/Contents/Resources/scripts/${pair#*:}"
  done
  printf '{"python": "%s", "entry": "%s", "keychain_service": "%s"}\n' "$PYTHON" "$entry" "${PREFIX}.mail-imap" \
    > "${app}/Contents/Resources/launcher.json"
  cat > "${app}/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleIdentifier</key><string>${PREFIX}.${suffix}</string>
  <key>CFBundleName</key><string>${name}</string>
  <key>CFBundleDisplayName</key><string>${name}</string>
  <key>CFBundleExecutable</key><string>${exe}</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>${VERSION}</string>
  <key>CFBundleVersion</key><string>${VERSION}</string>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
  <key>LSUIElement</key><true/>
  <key>NSAppleEventsUsageDescription</key><string>${usage}</string>
</dict>
</plist>
PLIST
  codesign --force --sign "$IDENTITY" --options runtime --timestamp=none \
    --entitlements "${HERE}/entitlements.plist" "$app" 2>&1 | { grep -v 'replacing existing signature' >&2 || true; }
  codesign --verify --strict "$app"
  printf 'built %s (%s.%s)\n' "$app" "$PREFIX" "$suffix"
}

make_app "Assistant Mail Reader" mail-reader assistant-mail-reader mail-index.py \
  "Reads the mailboxes you chose in mail-index.toml so your assistant can search them. It never changes or sends mail." \
  scripts/mail-index.py:mail-index.py \
  scripts/lib/mail_index.py:lib/mail_index.py \
  scripts/lib/mail_script.py:lib/mail_script.py \
  scripts/lib/mail_script.js:lib/mail_script.js \
  scripts/lib/mail_imap.py:lib/mail_imap.py \
  scripts/lib/mail_imap_conn.py:lib/mail_imap_conn.py \
  scripts/lib/mail_credentials.py:lib/mail_credentials.py \
  scripts/lib/jxa.py:lib/jxa.py

make_app "Assistant Mail Drafts" mail-drafts assistant-mail-drafts mail-drafts.py \
  "Saves drafts your assistant wrote to your Drafts for you to review. It never sends mail." \
  scripts/mail-drafts.py:mail-drafts.py \
  scripts/lib/mail_drafts.py:lib/mail_drafts.py \
  scripts/lib/mail_drafts.js:lib/mail_drafts.js \
  scripts/lib/mail_imap_drafts.py:lib/mail_imap_drafts.py \
  scripts/lib/mail_imap_conn.py:lib/mail_imap_conn.py \
  scripts/lib/mail_credentials.py:lib/mail_credentials.py \
  scripts/lib/jxa.py:lib/jxa.py \
  compose/mail-drafts-mcp/drafts.py:lib/drafts.py

printf 'python: %s\nsigned: %s\n' "$PYTHON" "$( [[ "$IDENTITY" == "-" ]] && echo "ad hoc" || echo "$IDENTITY")"
