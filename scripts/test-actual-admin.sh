#!/usr/bin/env bash
# actual-admin.mjs against a real Actual server: session-token sign-in and the
# exit codes for a wrong token, a missing budget and a dead server.
#
# Starts actualbudget/actual-server on a scratch network and runs the admin
# script from the actual-mcp image with `docker run`. Skips when Docker or
# either image is missing. Removes its containers and network on exit.
set -eu

SERVER_IMAGE="actualbudget/actual-server:26.11.0"
MCP_IMAGE="assistant-actual-mcp:local"

for img in "$SERVER_IMAGE" "$MCP_IMAGE"; do
  if ! docker image inspect "$img" >/dev/null 2>&1; then
    printf 'skip: %s not present\n' "$img"
    exit 0
  fi
done

suffix="$$-$(date +%s)"
net="actual-admin-test-${suffix}"
server="actual-admin-test-server-${suffix}"
pw="test-pw-$(date +%s)-xq"
tmp="$(mktemp -d)"

cleanup() {
  docker rm -f "$server" >/dev/null 2>&1 || true
  docker network rm "$net" >/dev/null 2>&1 || true
  rm -rf "$tmp"
}
trap cleanup EXIT

failures=0
fail() { printf 'FAIL: %s\n' "$1"; failures=$((failures + 1)); }

docker network create "$net" >/dev/null
docker run -d --name "$server" --network "$net" --network-alias actual-server "$SERVER_IMAGE" >/dev/null

# node one-liners in the MCP image, on the scratch network.
dnode() {
  docker run --rm --network "$net" -e PW --entrypoint node "$MCP_IMAGE" -e "$@"
}

ready=""
for _ in $(seq 1 60); do
  if dnode 'fetch("http://actual-server:5006/account/needs-bootstrap").then(r=>process.exit(r.ok?0:1),()=>process.exit(1))' >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 1
done
[ -n "$ready" ] || { echo "FAIL: server did not come up"; exit 1; }

PW="$pw" dnode '
(async()=>{
const r=await fetch("http://actual-server:5006/account/bootstrap",{method:"POST",headers:{"content-type":"application/json"},body:JSON.stringify({password:process.env.PW})}).then(r=>r.json());
if(r.status!=="ok")process.exit(1);
})();
' >/dev/null 2>&1 || { echo "FAIL: bootstrap"; exit 1; }

token="$(PW="$pw" dnode '
(async()=>{
const r=await fetch("http://actual-server:5006/account/login",{method:"POST",headers:{"content-type":"application/json"},body:JSON.stringify({loginMethod:"password",password:process.env.PW})}).then(r=>r.json());
process.stdout.write(r.data.token);
})();
' 2>/dev/null)"
[ -n "$token" ] || { echo "FAIL: no session token"; exit 1; }

# admin <stdout-file> <stderr-file> <env assignments...> -- <command>
# Credentials go in by name so they stay out of the process list.
admin() {
  local so="$1" se="$2"
  shift 2
  local args=() e
  for e in "$@"; do args+=(-e "${e%%=*}"); done
  # export is a builtin, so the values never appear in a process's argv.
  (
    # shellcheck disable=SC2163  # each argument is a NAME=value assignment
    export "$@"
    docker run --rm --network "$net" "${args[@]}" \
      -e ACTUAL_ADMIN_TIMEOUT_S=20 \
      --entrypoint node "$MCP_IMAGE" /opt/actual-admin.mjs "${CMD[@]}" >"$so" 2>"$se"
  )
}

run() { # run <name> <expect-exit> <expect-stdout> -- env... ; CMD set by caller
  local name="$1" want="$2" word="$3" rc=0
  shift 3
  admin "${tmp}/out" "${tmp}/err" "$@" || rc=$?
  if [ "$rc" != "$want" ]; then
    fail "${name}: exit ${rc}, want ${want} (stdout: $(tr '\n' ' ' <"${tmp}/out"), stderr: $(tail -1 "${tmp}/err"))"
  elif [ "$(tr -d '\n' <"${tmp}/out")" != "$word" ]; then
    fail "${name}: stdout '$(tr '\n' ' ' <"${tmp}/out")', want '${word}'"
  else
    printf 'ok: %s\n' "$name"
  fi
  if grep -qF -e "$pw" -e "$token" "${tmp}/out" "${tmp}/err"; then
    fail "${name}: the password or token appears in the output"
  fi
}

url="ACTUAL_SERVER_URL=http://actual-server:5006"

CMD=(create Spike)
admin "${tmp}/created" "${tmp}/err" "$url" "ACTUAL_PASSWORD=${pw}" || fail "create: $(tail -1 "${tmp}/err")"
sync_id="$(tr -d '\n' <"${tmp}/created")"
[ -n "$sync_id" ] || { fail "create printed no sync id"; sync_id="missing"; }

CMD=(check)
run "token opens the budget" 0 "" "$url" "ACTUAL_SESSION_TOKEN=${token}" "ACTUAL_BUDGET_SYNC_ID=${sync_id}"
run "password opens the budget" 0 "" "$url" "ACTUAL_PASSWORD=${pw}" "ACTUAL_BUDGET_SYNC_ID=${sync_id}"
run "bogus token" 3 login "$url" "ACTUAL_SESSION_TOKEN=tok-bogus" "ACTUAL_BUDGET_SYNC_ID=${sync_id}"
run "unknown sync id" 5 no_access "$url" "ACTUAL_SESSION_TOKEN=${token}" "ACTUAL_BUDGET_SYNC_ID=00000000-0000-4000-8000-000000000000"
run "closed port" 6 unreachable "ACTUAL_SERVER_URL=http://actual-server:5999" "ACTUAL_SESSION_TOKEN=${token}" "ACTUAL_BUDGET_SYNC_ID=${sync_id}"

CMD=(list)
admin "${tmp}/out" "${tmp}/err" "$url" "ACTUAL_SESSION_TOKEN=${token}" || fail "list with token: $(tail -1 "${tmp}/err")"
if grep -q "^${sync_id}	Spike$" "${tmp}/out"; then echo "ok: list with token"; else fail "list with token lacks the budget"; fi

[ "$failures" = 0 ] || { printf '%s failure(s)\n' "$failures"; exit 1; }
echo "all passed"
