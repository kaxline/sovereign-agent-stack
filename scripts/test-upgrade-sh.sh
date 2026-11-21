#!/usr/bin/env bash
# scripts/upgrade.sh: which hooks run, the directives, the start it asks
# scripts/up.sh for, the --json result, and the shipped hooks on a synthetic
# stack. scripts/up.sh is replaced by a stub that records its arguments.
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=lib/testkit.sh
. "${ROOT}/scripts/lib/testkit.sh"

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

new_stack() {
  stack="${tmp}/$1"
  mkdir -p "${stack}/scripts/lib" "${stack}/upgrades"
  cp "${ROOT}/scripts/upgrade.sh" "${stack}/scripts/"
  cp "${ROOT}/scripts/lib/env.sh" "${ROOT}/scripts/lib/json-lines.sh" "${ROOT}/scripts/lib/data-root.sh" \
    "${stack}/scripts/lib/"
  cp "${ROOT}/upgrades/lib.sh" "${stack}/upgrades/"
  # shellcheck disable=SC2016  # expanded by the stub
  printf '#!/bin/sh\necho "$*" > "$(dirname "$0")/../up.args"\n' > "${stack}/scripts/up.sh"
  chmod +x "${stack}/scripts/up.sh"
  : > "${stack}/.env"
}
run() { (cd "$stack" && bash scripts/upgrade.sh "$@"); }
hook() {
  # shellcheck disable=SC2016  # expanded by the hook
  printf '#!/usr/bin/env bash\nset -eu\nsource "$(dirname "$0")/lib.sh"\necho "$STACK_FROM>$STACK_TO" >> "$(dirname "$0")/../ran"\n%s\n' "$2" \
    > "${stack}/upgrades/$1.sh"
}

# --- selection and directives ------------------------------------------------
new_stack sel
hook v0.3.9 'notice "nine"'
hook v0.3.10 'rebuild broker; needs_user ten-id "ten: do a thing"'
hook v0.3.11 'rebuild broker; reload_hermes; notice "eleven"'
hook v0.4.0 'notice "four"'
printf 'v0.3.11\n' > "${stack}/.stack-version"

out="$(run --from v0.3.9 --json)" || fail "upgrade failed: $out"
[ "$(cat "${stack}/ran")" = "$(printf 'v0.3.9>v0.3.11\nv0.3.9>v0.3.11')" ] \
  || fail "hooks run: $(cat "${stack}/ran")"
[ "$(cat "${stack}/up.args")" = "--build broker --reload-hermes" ] \
  || fail "up.sh args: $(cat "${stack}/up.args")"
done_line="$(printf '%s\n' "$out" | tail -1)"
python3 - "$done_line" << 'EOF' || fail "done line: $done_line"
import json, sys
d = json.loads(sys.argv[1])
assert d["phase"] == "done", d
assert d["hooks"] == ["v0.3.10", "v0.3.11"], d  # numeric order, v0.3.9 excluded
assert d["needs_user"] == [{"id": "ten-id", "message": "ten: do a thing", "version": "v0.3.10"}], d
assert [n["message"] for n in d["notices"]] == ["eleven"], d
EOF
printf '%s\n' "$out" | grep -q '"step":"start","status":"done"' || fail "no start step"

# Same version: no hooks, still starts. --no-start doesn't.
rm -f "${stack}/ran" "${stack}/up.args"
run --from v0.3.11 --json > /dev/null
[ ! -e "${stack}/ran" ] || fail "hooks ran for an unchanged version"
[ -e "${stack}/up.args" ] || fail "no start for an unchanged version"
rm -f "${stack}/up.args"
run --from v0.3.9 --to v0.3.10 --no-start --json > /dev/null
[ ! -e "${stack}/up.args" ] || fail "--no-start started"

# A git describe suffix runs that tag's hooks.
rm -f "${stack}/ran"
run --from v0.3.10 --to v0.3.11-4-gabc1234 --no-start > /dev/null 2>&1
[ "$(wc -l < "${stack}/ran" | tr -d ' ')" = 1 ] || fail "describe suffix"

expect_error() {
  want_rc="$1" want="$2"
  shift 2
  set +e
  out="$(run "$@" --json 2> /dev/null)"
  rc=$?
  set -e
  [ "$rc" = "$want_rc" ] || fail "$* exit $rc, want $want_rc"
  case "$out" in *"\"error\":\"${want}\""*) ;; *) fail "$*: $out" ;; esac
}
expect_error 2 bad_input --from v0.4.0 --to v0.3.11
expect_error 2 bad_input --from main
expect_error 2 bad_input --to v0.3.11

# A failing hook stops the upgrade before the start.
hook v0.3.12 'echo "broken step" >&2; exit 3'
rm -f "${stack}/up.args"
expect_error 4 command_failed --from v0.3.11 --to v0.3.12
case "$(printf '%s\n' "$out" | tail -1)" in *'"step":"hook"'*'v0.3.12 failed: broken step'*'"logTail":["broken step"]'*) ;; *) fail "hook failure: $out" ;; esac
[ ! -e "${stack}/up.args" ] || fail "started after a failed hook"
hook v0.3.12 'echo "bogus thing"'
expect_error 4 command_failed --from v0.3.11 --to v0.3.12

# --- the shipped hooks ---------------------------------------------------------
new_stack shipped
cp "${ROOT}"/upgrades/v*.sh "${stack}/upgrades/"
mkdir -p "${stack}/data/hermes/profiles/work" "${stack}/compose/caldav-mcp/accounts"
printf 'A=1\nVIKUNJA_MCP_TOKEN=old\n' > "${stack}/data/hermes/.env"
printf 'VIKUNJA_MCP_TOKEN=old\nTODOS_KEY=new\n' > "${stack}/data/hermes/profiles/work/.env"
printf 'URL=x\n' > "${stack}/compose/caldav-mcp/accounts/home.env"
printf 'BITWARDEN_ENABLED=1\n' > "${stack}/.env"

out="$(run --from v0.3.14 --to v0.3.16 --json)" || fail "shipped hooks failed: $out"
grep -q VIKUNJA_MCP_TOKEN "${stack}/data/hermes/.env" "${stack}/data/hermes/profiles/work/.env" \
  && fail "VIKUNJA_MCP_TOKEN left in a profile .env"
grep -q '^TODOS_KEY=new$' "${stack}/data/hermes/profiles/work/.env" || fail "TODOS_KEY lost"
[ "$(cat "${stack}/up.args")" = "--build bitwarden-broker --reload-hermes" ] \
  || fail "shipped up.sh args: $(cat "${stack}/up.args")"
python3 - "$(printf '%s\n' "$out" | tail -1)" << 'EOF' || fail "shipped done line: $out"
import json, sys
d = json.loads(sys.argv[1])
assert d["hooks"] == ["v0.3.15", "v0.3.16"], d
assert [n["id"] for n in d["needs_user"]] == ["caldav-reconnect-home", "bitwarden-in-page-fill"], d
assert len(d["notices"]) == 2, d
EOF

# Without Bitwarden, v0.3.16 asks for nothing; run again, v0.3.15 is idempotent.
printf 'BITWARDEN_ENABLED=0\n' > "${stack}/.env"
rm "${stack}/compose/caldav-mcp/accounts/home.env"
out="$(run --from v0.3.14 --to v0.3.16 --json)"
[ "$(cat "${stack}/up.args")" = "--reload-hermes" ] || fail "no-bitwarden args: $(cat "${stack}/up.args")"
case "$out" in *'"needs_user":[]'*) ;; *) fail "no-bitwarden needs_user: $out" ;; esac

# --- v0.3.17 -------------------------------------------------------------------
# A docker stub: compose config names the project, one bitwarden_signin volume
# whose options come from $VOL_OPTS.
new_stack v317
cp "${ROOT}"/upgrades/v*.sh "${stack}/upgrades/"
mkdir -p "${stack}/bin"
cat > "${stack}/bin/docker" << 'EOF'
#!/bin/sh
case "$1 $2" in
  "compose config") echo "name: proj" ;;
  "volume ls") echo proj_bitwarden_signin ;;
  "volume inspect") echo "$VOL_OPTS" ;;
esac
EOF
chmod +x "${stack}/bin/docker"
printf '%s\n' 'OPENCODE_MCP_ENABLED=1' 'BITWARDEN_ENABLED=1' \
  'NEO4J_USERNAME=neo4j                  # Neo4j requires the admin user' \
  'NEO4J_PASSWORD=pw#1   # >= 8 chars' "GPTR_MCP_URL='http://g:8000/sse # kept'" \
  'LLM_BINDING_API_KEY=local-llm' > "${stack}/.env"
v317() { (cd "$stack" && PATH="${stack}/bin:${PATH}" VOL_OPTS="$1" bash scripts/upgrade.sh --from v0.3.16 --to v0.3.17 --json); }
out="$(v317 'uid=1000,gid=1000,mode=0700,size=1m')" || fail "v0.3.17 hook failed: $out"
[ "$(cat "${stack}/up.args")" = "--build opencode-mcp" ] || fail "v0.3.17 up.sh args: $(cat "${stack}/up.args")"
case "$out" in *'"needs_user":[{"id":"bitwarden-signin-volume"'*'proj_bitwarden_signin'*) ;; *) fail "v0.3.17 volume needs_user: $out" ;; esac
grep -qx 'NEO4J_USERNAME=neo4j' "${stack}/.env" || fail "NEO4J_USERNAME comment kept: $(cat "${stack}/.env")"
grep -qx 'NEO4J_PASSWORD=pw#1' "${stack}/.env" || fail "NEO4J_PASSWORD: $(cat "${stack}/.env")"
grep -qx "GPTR_MCP_URL='http://g:8000/sse # kept'" "${stack}/.env" || fail "quoted value changed: $(cat "${stack}/.env")"
grep -qx 'LLM_BINDING_API_KEY=local-llm' "${stack}/.env" || fail "plain value changed"
# The new volume options ask for nothing; a second run changes nothing.
cp "${stack}/.env" "${tmp}/env.before"
out="$(v317 'mode=1777,size=1m')"
case "$out" in *'"needs_user":[]'*) ;; *) fail "v0.3.17 new volume needs_user: $out" ;; esac
cmp -s "${stack}/.env" "${tmp}/env.before" || fail "v0.3.17 not idempotent on .env"

# --- v0.3.18 -------------------------------------------------------------------
# A docker stub backed by files in stub/: <service>.image means its container
# exists, <service>.running that it runs, n8n_storage that the volume exists.
# Every call is logged to docker.log; DUMP_FAIL=1 makes pg_dump fail.
new_stack v318
cp "${ROOT}"/upgrades/v*.sh "${stack}/upgrades/"
mkdir -p "${stack}/bin" "${stack}/stub" "${stack}/data/hermes" "${stack}/data/addons/twenty/instances"
cat > "${stack}/bin/docker" << 'EOF'
#!/bin/sh
d="$(dirname "$0")/../stub"
echo "$*" >> "$d/../docker.log"
svc="$(printf '%s\n' "$@" | sed -n 's/^label=com.docker.compose.service=//p')"
case "$*" in
  "compose config") echo "name: proj" ;;
  "volume ls"*) [ -e "$d/n8n_storage" ] && echo proj_n8n_storage ;;
  ps*status=running*) [ -e "$d/${svc}.running" ] && echo "id-${svc}" ;;
  ps*) [ -e "$d/${svc}.image" ] && echo "id-${svc}" ;;
  inspect*) cat "$d/${4#id-}.image" ;;
  "stop "*) rm -f "$d/${2#id-}.running" ;;
  compose*" exec -T twenty-db pg_dump "*) [ -z "${DUMP_FAIL:-}" ] || exit 1; echo "-- DUMP" ;;
  compose*" run "*" tar n8n "*) echo TAR ;;
esac
exit 0
EOF
chmod +x "${stack}/bin/docker"
echo state > "${stack}/data/hermes/state.txt"
echo '{"local": true}' > "${stack}/data/addons/twenty/instances/main.json"
echo nousresearch/hermes-agent:v0.23.1 > "${stack}/stub/hermes.image"
: > "${stack}/stub/hermes.running"
echo n8nio/n8n:2.47.4 > "${stack}/stub/n8n.image"
: > "${stack}/stub/n8n.running"
: > "${stack}/stub/n8n_storage"
echo twentycrm/twenty:v2.55.0 > "${stack}/stub/twenty-server.image"
printf '%s\n' 'COMPOSE_PROFILES=core,crm' 'HERMES_AGENT_IMAGE_TAG=v0.23.1' 'HERMES_WEBUI_IMAGE_TAG=0.52.544' \
  'SIGNAL_CLI_IMAGE=registry.gitlab.com/packaging/signal-cli/signal-cli-jre:v0-14-9-2' \
  'N8N_IMAGE=n8nio/n8n:2.47.4' 'OLLAMA_IMAGE=ollama/ollama:0.40.12' 'TWENTY_IMAGE=twentycrm/twenty:v2.55.0' \
  'TWENTY_LOCAL_ENABLED=1' 'AUTOMATION_ENABLED=1' 'BITWARDEN_ENABLED=1' 'OPENCODE_MCP_ENABLED=1' > "${stack}/.env"
v318() { (cd "$stack" && PATH="${stack}/bin:${PATH}" bash scripts/upgrade.sh --from v0.3.17 --to v0.3.18 --json); }
count() { find "${stack}/$1" -maxdepth 1 -name "$2" 2> /dev/null | wc -l | tr -d ' '; }

out="$(v318)" || fail "v0.3.18 hook failed: $out"
for want in HERMES_AGENT_IMAGE_TAG=v0.23.2 HERMES_WEBUI_IMAGE_TAG=0.52.702 \
  SIGNAL_CLI_IMAGE=registry.gitlab.com/packaging/signal-cli/signal-cli-jre:v0-14-10-1 \
  N8N_IMAGE=n8nio/n8n:2.48.7 OLLAMA_IMAGE=ollama/ollama:0.40.12 TWENTY_IMAGE=twentycrm/twenty:v2.60.3; do
  grep -qx "$want" "${stack}/.env" || fail "v0.3.18 .env lacks ${want}: $(cat "${stack}/.env")"
done
grep -q MCP_SEARXNG_IMAGE "${stack}/.env" && fail "v0.3.18 added an unset pin"
[ "$(cat "${stack}"/data/hermes.bak-*-pre-v0.23.2/state.txt)" = state ] || fail "no Hermes backup"
[ "$(cat "${stack}"/data/backups/n8n-before-2.48.7-*.tar)" = TAR ] || fail "no n8n backup"
[ "$(cat "${stack}"/data/twenty/backups/twenty-before-v2.60.3-*.sql)" = "-- DUMP" ] || fail "no Twenty dump"
grep -qx 'stop id-hermes' "${stack}/docker.log" || fail "hermes not stopped for its backup"
grep -qx 'stop id-n8n' "${stack}/docker.log" || fail "n8n not stopped for its backup"
grep -q 'up -d --wait twenty-db$' "${stack}/docker.log" || fail "twenty-db not started for the dump"
[ "$(cat "${stack}/up.args")" = "--build bitwarden-broker --build bitwarden-signin --build opencode --reload-hermes" ] \
  || fail "v0.3.18 up.sh args: $(cat "${stack}/up.args")"
python3 - "$(printf '%s\n' "$out" | tail -1)" << 'EOF' || fail "v0.3.18 done line: $out"
import json, sys
d = json.loads(sys.argv[1])
assert [n["id"] for n in d["needs_user"]] == ["twenty-crm-init"], d
assert "crm-init ROTATE=1" in d["needs_user"][0]["message"], d
notes = " ".join(n["message"] for n in d["notices"])
assert "OLLAMA_IMAGE=ollama/ollama:0.40.12" in notes and "twenty-before-v2.60.3-" in notes, d
EOF

# A second run makes no new backup and leaves .env alone.
cp "${stack}/.env" "${tmp}/env.before"
: > "${stack}/stub/hermes.running"
rm "${stack}/docker.log"
out="$(v318)" || fail "v0.3.18 second run failed: $out"
cmp -s "${stack}/.env" "${tmp}/env.before" || fail "v0.3.18 not idempotent on .env"
[ "$(count data 'hermes.bak-*')" = 1 ] || fail "second Hermes backup"
grep -Eq '^(stop|compose.* (run|exec|up) )' "${stack}/docker.log" && fail "second run touched containers: $(cat "${stack}/docker.log")"

# A failed dump stops the upgrade before the start and leaves no dump behind.
rm -f "${stack}"/data/twenty/backups/*.sql "${stack}/up.args"
set +e
out="$( (cd "$stack" && PATH="${stack}/bin:${PATH}" DUMP_FAIL=1 bash scripts/upgrade.sh --from v0.3.17 --to v0.3.18 --json 2> /dev/null))"
rc=$?
set -e
[ "$rc" = 4 ] || fail "failed dump exit ${rc}: $out"
case "$out" in *'pg_dump of twenty-db failed'*) ;; *) fail "failed dump message: $out" ;; esac
[ "$(count data/twenty/backups 'twenty-*')" = 0 ] || fail "failed dump left a file"
[ ! -e "${stack}/up.args" ] || fail "started after a failed dump"

# twenty-server already on the new image: nothing to dump. Without the local
# instance or a stored local workspace there is no crm-init step.
echo twentycrm/twenty:v2.60.3 > "${stack}/stub/twenty-server.image"
out="$(cd "$stack" && PATH="${stack}/bin:${PATH}" DUMP_FAIL=1 bash scripts/upgrade.sh --from v0.3.17 --to v0.3.18 --json)" \
  || fail "v0.3.18 on a migrated Twenty: $out"
printf 'TWENTY_LOCAL_ENABLED=0\n' > "${stack}/.env"
out="$(v318)" || fail "v0.3.18 without Twenty: $out"
case "$out" in *'"needs_user":[]'*) ;; *) fail "v0.3.18 without Twenty needs_user: $out" ;; esac
[ "$(cat "${stack}/up.args")" = "--reload-hermes" ] || fail "v0.3.18 bare up.sh args: $(cat "${stack}/up.args")"

echo "ok upgrade.sh"
