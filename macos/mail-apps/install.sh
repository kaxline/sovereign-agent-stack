#!/usr/bin/env bash
# Install a mail helper app and its launchd agent. See docs/email.md.
#
#   macos/mail-apps/install.sh reader --config /abs/mail-index.toml [--interval 900]
#   macos/mail-apps/install.sh drafts --dir /abs/data/mail-drafts [--open] [--max-per-hour 20]
#   macos/mail-apps/install.sh uninstall reader|drafts
#
# Add --dry-run to print the agent's plist instead of installing anything.
#
# Copies the built app (build/mail-apps, from build.sh) to MAIL_APPS_DIR
# (default ~/Applications) and loads a LaunchAgent that runs it: the reader
# every --interval seconds, the drafts helper continuously. launchd starts
# the app itself, so macOS asks for permissions on the app's behalf, not your
# terminal's. Logs (counts only) go to ~/Library/Logs/assistant-mail/.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
BUILD="${MAIL_APPS_BUILD:-${ROOT}/build/mail-apps}"
APPS="${MAIL_APPS_DIR:-${HOME}/Applications}"
PREFIX="${MAIL_APPS_BUNDLE_PREFIX:-local.assistant}"
AGENTS="${HOME}/Library/LaunchAgents"
LOGS="${HOME}/Library/Logs/assistant-mail"

die() { printf 'install: %s\n' "$1" >&2; exit 2; }
usage() { sed -n '3,7p' "$0" | sed 's/^# \{0,1\}//' >&2; exit 2; }

[[ $# -ge 1 ]] || usage
action="$1"; shift
uninstall=0
if [[ "$action" == "uninstall" ]]; then
  uninstall=1
  [[ $# -ge 1 ]] || usage
  action="$1"; shift
fi

case "$action" in
  reader) name="Assistant Mail Reader"; exe="assistant-mail-reader" ;;
  drafts) name="Assistant Mail Drafts"; exe="assistant-mail-drafts" ;;
  *) usage ;;
esac
label="${PREFIX}.mail-${action}"
plist="${AGENTS}/${label}.plist"
domain="gui/$(id -u)"

if [[ "$uninstall" -eq 1 ]]; then
  launchctl bootout "${domain}/${label}" 2>/dev/null || true
  rm -f "$plist"
  rm -rf "${APPS}/${name}.app"
  printf 'removed %s and %s\n' "$label" "${APPS}/${name}.app"
  printf 'Permissions you granted stay listed in System Settings > Privacy & Security until you remove them.\n'
  exit 0
fi

config="" dir="" interval=900 open=0 max_per_hour=20 dry_run=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --config) config="${2:-}"; shift 2 ;;
    --dir) dir="${2:-}"; shift 2 ;;
    --interval) interval="${2:-}"; shift 2 ;;
    --open) open=1; shift ;;
    --max-per-hour) max_per_hour="${2:-}"; shift 2 ;;
    --dry-run) dry_run=1; shift ;;
    *) usage ;;
  esac
done

xml() { local s="$1"; s="${s//&/&amp;}"; s="${s//</&lt;}"; s="${s//>/&gt;}"; printf '%s' "$s"; }
args=("${APPS}/${name}.app/Contents/MacOS/${exe}")
if [[ "$action" == "reader" ]]; then
  [[ "$config" == /* && -f "$config" ]] || die "--config must be the absolute path of an existing mail-index.toml"
  [[ "$interval" =~ ^[0-9]+$ && "$interval" -ge 60 ]] || die "--interval is seconds, at least 60"
  args+=(--config "$config" run)
  schedule="  <key>StartInterval</key><integer>${interval}</integer>"
else
  [[ "$dir" == /* && -d "$dir" ]] || die "--dir must be the absolute path of the drafts folder (MAIL_DRAFTS_DIR)"
  [[ "$max_per_hour" =~ ^[0-9]+$ && "$max_per_hour" -ge 1 ]] || die "--max-per-hour must be a positive number"
  args+=(--dir "$dir" --max-per-hour "$max_per_hour")
  [[ "$open" -eq 1 ]] && args+=(--open)
  args+=(watch)
  schedule="  <key>KeepAlive</key><true/>"
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
  <key>StandardOutPath</key><string>$(xml "${LOGS}/mail-${action}.log")</string>
  <key>StandardErrorPath</key><string>$(xml "${LOGS}/mail-${action}.log")</string>
</dict>
</plist>"

if [[ "$dry_run" -eq 1 ]]; then
  printf '%s\n' "$content"
  exit 0
fi

[[ -d "${BUILD}/${name}.app" ]] || die "no ${BUILD}/${name}.app; run macos/mail-apps/build.sh first"
codesign --verify --strict "${BUILD}/${name}.app" || die "${name}.app fails its signature check; rebuild it"
mkdir -p "$APPS" "$AGENTS" "$LOGS"
chmod 700 "$LOGS"
launchctl bootout "${domain}/${label}" 2>/dev/null || true
rm -rf "${APPS}/${name}.app"
ditto "${BUILD}/${name}.app" "${APPS}/${name}.app"
printf '%s\n' "$content" > "$plist"
plutil -lint "$plist" >/dev/null
launchctl bootstrap "$domain" "$plist"
printf 'installed %s and loaded %s\n' "${APPS}/${name}.app" "$label"
printf 'macOS will ask to let "%s" control Mail. Logs: %s\n' "$name" "${LOGS}/mail-${action}.log"
