#!/usr/bin/env bash
# Install a mail helper app and its launchd agent. See docs/email.md.
#
#   macos/mail-apps/install.sh reader --config /abs/mail-index.toml [--interval 900]
#   macos/mail-apps/install.sh drafts --dir /abs/data/mail-drafts [--config /abs/mail-index.toml] [--no-mail-app] [--open] [--max-per-hour 20]
#   macos/mail-apps/install.sh password <account-id>
#   macos/mail-apps/install.sh uninstall reader|drafts
#
# Add --dry-run to print the agent's plist instead of installing anything.
#
# `password` stores an IMAP account's password in your login Keychain for the
# two mail apps, prompting for it (nothing is echoed or saved anywhere else).
# Install the apps first: the Keychain item trusts those copies. Run it again
# to change the password, or after rebuilding ad hoc signed apps.
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

if [[ "$action" == "password" ]]; then
  [[ $# -eq 1 && "$1" =~ ^[A-Za-z0-9._@-]+$ ]] || die "password takes one account id, as in mail-index.toml"
  service="${PREFIX}.mail-imap"
  trusted=()
  for app in "Assistant Mail Reader" "Assistant Mail Drafts"; do
    [[ -d "${APPS}/${app}.app" ]] && trusted+=(-T "${APPS}/${app}.app")
  done
  [[ ${#trusted[@]} -gt 0 ]] || die "install the reader or drafts app first, so the password can trust it"
  printf 'Password for IMAP account "%s" (an app password, if your provider offers them).\n' "$1"
  # -U updates an existing item. -w last, with no value, makes security prompt
  # for the password, so it's never in an argument or your shell history.
  security add-generic-password -U -s "$service" -a "$1" -l "Assistant mail: $1" "${trusted[@]}" -w
  printf 'saved to the Keychain as %s / %s\n' "$service" "$1"
  exit 0
fi

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

config="" dir="" interval=900 open=0 no_mail_app=0 max_per_hour=20 dry_run=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --config) config="${2:-}"; shift 2 ;;
    --dir) dir="${2:-}"; shift 2 ;;
    --interval) interval="${2:-}"; shift 2 ;;
    --open) open=1; shift ;;
    --no-mail-app) no_mail_app=1; shift ;;
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
  if [[ -n "$config" ]]; then
    [[ "$config" == /* && -f "$config" ]] || die "--config must be the absolute path of an existing mail-index.toml"
    args+=(--config "$config")
  fi
  [[ "$no_mail_app" -eq 1 ]] && { [[ -n "$config" ]] || die "--no-mail-app needs --config"; args+=(--no-mail-app); }
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
