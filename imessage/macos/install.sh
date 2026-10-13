#!/usr/bin/env bash
# Install the Messages reader app and its launchd agent. See imessage/README.md.
#
#   imessage/macos/install.sh --config /abs/messages-index.toml            # watch continuously
#   imessage/macos/install.sh --config /abs/messages-index.toml --interval 900   # index every 15 min, no events
#   imessage/macos/install.sh uninstall
#
# Add --dry-run to print the agent's plist instead of installing anything.
#
# Copies the built app (imessage/build, from build.sh) to MESSAGES_APP_DIR
# (default ~/Applications) and loads a LaunchAgent that runs it. By default
# the agent runs `watch`, which keeps the index current and raises the events
# the config's [notify] table asks for; with --interval it runs `run` on a
# timer instead. launchd starts the app itself, so the Full Disk Access you
# grant the app covers it, not your terminal. Logs (counts only) go to
# ~/Library/Logs/assistant-messages/.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
SRC="$(cd "${HERE}/.." && pwd)"
BUILD="${MESSAGES_APP_BUILD:-${SRC}/build}"
APPS="${MESSAGES_APP_DIR:-${HOME}/Applications}"
PREFIX="${MESSAGES_APP_BUNDLE_PREFIX:-local.assistant}"
AGENTS="${HOME}/Library/LaunchAgents"
LOGS="${HOME}/Library/Logs/assistant-messages"
NAME="Assistant Messages Reader"
EXE="assistant-messages-reader"
label="${PREFIX}.messages-reader"
plist="${AGENTS}/${label}.plist"
domain="gui/$(id -u)"

die() { printf 'install: %s\n' "$1" >&2; exit 2; }
usage() { sed -n '3,6p' "$0" | sed 's/^# \{0,1\}//' >&2; exit 2; }

if [[ "${1:-}" == "uninstall" ]]; then
  launchctl bootout "${domain}/${label}" 2>/dev/null || true
  rm -f "$plist"
  rm -rf "${APPS}/${NAME}.app"
  printf 'removed %s and %s\n' "$label" "${APPS}/${NAME}.app"
  printf 'Remove "%s" from System Settings > Privacy & Security > Full Disk Access too.\n' "$NAME"
  exit 0
fi

config="" interval="" dry_run=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --config) config="${2:-}"; shift 2 ;;
    --interval) interval="${2:-}"; shift 2 ;;
    --dry-run) dry_run=1; shift ;;
    *) usage ;;
  esac
done
[[ "$config" == /* && -f "$config" ]] || die "--config must be the absolute path of an existing messages-index.toml"

xml() { local s="$1"; s="${s//&/&amp;}"; s="${s//</&lt;}"; s="${s//>/&gt;}"; printf '%s' "$s"; }
args=("${APPS}/${NAME}.app/Contents/MacOS/${EXE}" --config "$config")
if [[ -n "$interval" ]]; then
  [[ "$interval" =~ ^[0-9]+$ && "$interval" -ge 60 ]] || die "--interval is seconds, at least 60"
  args+=(run)
  schedule="  <key>StartInterval</key><integer>${interval}</integer>"
else
  args+=(watch)
  schedule="  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>30</integer>"
fi

program=""
for a in "${args[@]}"; do program+="    <string>$(xml "$a")</string>"$'\n'; done
content="<?xml version=\"1.0\" encoding=\"UTF-8\"?>
<!DOCTYPE plist PUBLIC \"-//Apple//DTD PLIST 1.0//EN\" \"http://www.apple.com/DTDs/PropertyList-1.0.dtd\">
<plist version=\"1.0\">
<dict>
  <key>Label</key><string>${label}</string>
  <key>ProgramArguments</key>
  <array>
${program}  </array>
  <key>RunAtLoad</key><true/>
${schedule}
  <key>ProcessType</key><string>Background</string>
  <key>StandardOutPath</key><string>$(xml "${LOGS}/messages-reader.log")</string>
  <key>StandardErrorPath</key><string>$(xml "${LOGS}/messages-reader.log")</string>
</dict>
</plist>"

if [[ "$dry_run" -eq 1 ]]; then
  printf '%s\n' "$content"
  exit 0
fi

[[ -d "${BUILD}/${NAME}.app" ]] || die "no ${BUILD}/${NAME}.app; run imessage/macos/build.sh first"
codesign --verify --strict "${BUILD}/${NAME}.app" || die "${NAME}.app fails its signature check; rebuild it"
mkdir -p "$APPS" "$AGENTS" "$LOGS"
chmod 700 "$LOGS"
launchctl bootout "${domain}/${label}" 2>/dev/null || true
rm -rf "${APPS}/${NAME}.app"
ditto "${BUILD}/${NAME}.app" "${APPS}/${NAME}.app"
printf '%s\n' "$content" > "$plist"
plutil -lint "$plist" >/dev/null
launchctl bootstrap "$domain" "$plist"
printf 'installed %s and loaded %s\n' "${APPS}/${NAME}.app" "$label"
printf 'Now allow it: System Settings > Privacy & Security > Full Disk Access > + > %s\n' "${APPS}/${NAME}.app"
printf 'macOS does not ask for Full Disk Access; until you add the app, the log shows "permission denied".\n'
printf 'Logs: %s\n' "${LOGS}/messages-reader.log"
