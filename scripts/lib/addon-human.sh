# addon.sh's instance commands for a person at a terminal. scripts/budget.sh
# and scripts/crm.sh run addon.sh connect, disconnect and configure for their
# make targets and print their own "[prefix]" lines instead of JSON.
#
# Needs ROOT and stack_python. ADDON_SH and VAULT_SH, when set, replace
# scripts/addon.sh and scripts/vault.sh (test hooks).
# shellcheck shell=bash

# addon_human PREFIX SID ACTION SLUG [--force]: addon.sh ACTION SID --instance
# SLUG [--force] --json with this stdin. Its JSON lines become PREFIX lines:
# "[PREFIX] Connected SLUG; Hermes entries: ..." or "[PREFIX] Disconnected
# SLUG", then the done line's message on a line of its own, or
# "[PREFIX] ERROR: MESSAGE (ERROR)" on stderr. addon.sh's stderr is shown only
# when it printed no JSON line. Returns addon.sh's exit code.
addon_human() {
  local prefix="$1" sid="$2" action="$3" slug="$4" rc=0 lines errf
  errf="$(mktemp)"
  lines="$("${ADDON_SH:-${ROOT}/scripts/addon.sh}" "$action" "$sid" --instance "$slug" ${5:+"$5"} --json 2> "$errf")" || rc=$?
  printf '%s\n' "$lines" | stack_python -c '
import json, sys
prefix, action, slug = sys.argv[1:4]
seen = 0
for line in sys.stdin:
    try:
        d = json.loads(line)
    except ValueError:
        continue
    if not isinstance(d, dict):
        continue
    if d.get("phase") == "done":
        seen = 1
        if action == "connect":
            mcp = ", ".join(m for m in d.get("mcp") or [] if isinstance(m, str))
            print("[%s] Connected %s%s" % (prefix, slug, "; Hermes entries: " + mcp if mcp else ""))
        else:
            print("[%s] Disconnected %s" % (prefix, slug))
        if isinstance(d.get("message"), str) and d["message"]:
            print("[%s] %s" % (prefix, d["message"]))
    elif d.get("phase") == "error":
        seen = 1
        print("[%s] ERROR: %s (%s)" % (prefix, d.get("message") or "addon.sh failed", d.get("error")), file=sys.stderr)
sys.exit(0 if seen else 3)
' "$prefix" "$action" "$slug" || grep -v '^[[:space:]]*$' "$errf" >&2 || true
  rm -f "$errf"
  return "$rc"
}

# hermes_reload ERRFILE: what addon.sh's reload step runs, quietly: both
# bootstraps with HERMES_BOOTSTRAP_FORCE=1, then vault.sh reload-plugins.
# Their stderr goes to ERRFILE. Returns 1 when a bootstrap failed, 2 when
# reload-plugins did.
hermes_reload() {
  docker compose run --rm -e HERMES_BOOTSTRAP_FORCE=1 hermes-api-bootstrap > /dev/null 2> "$1" < /dev/null || return 1
  docker compose run --rm --no-deps -e HERMES_BOOTSTRAP_FORCE=1 hermes-browser-bootstrap > /dev/null 2>> "$1" < /dev/null || return 1
  "${VAULT_SH:-${ROOT}/scripts/vault.sh}" reload-plugins > /dev/null 2>&1 < /dev/null || return 2
}
