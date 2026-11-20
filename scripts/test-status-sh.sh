#!/usr/bin/env bash
# scripts/status.sh with a fake docker: stopped, running, engine down, the
# Hermes image missing (host python3, or awk without it), a tarball install's
# .stack-version, and the removed overlay flags refused.
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=lib/testkit.sh
. "${ROOT}/scripts/lib/testkit.sh"

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
stack="${tmp}/stack"
mkdir -p "${stack}/scripts/lib" "${tmp}/bin"
cp "${ROOT}/scripts/status.sh" "${stack}/scripts/"
cp "${ROOT}/scripts/lib/env.sh" "${ROOT}/scripts/lib/python.sh" \
  "${ROOT}/scripts/lib/stack_status.py" "${ROOT}/scripts/lib/stack_status.awk" \
  "${stack}/scripts/lib/"
cp "${ROOT}/docker-compose.yml" "${stack}/"
printf 'HERMES_AGENT_IMAGE_TAG=v9.9.9\n' > "${stack}/.env"

# The fake docker answers from files under $FAKE.
FAKE="${tmp}/fake"
mkdir -p "$FAKE"
cat > "${tmp}/bin/docker" << 'EOF'
#!/bin/sh
case "$1 $2" in
  "info "*) [ -f "${FAKE}/engine-down" ] && exit 1; exit 0 ;;
  "compose config") cat "${FAKE}/config.json" ;;
  "compose ps") [ -f "${FAKE}/engine-down" ] && exit 1; cat "${FAKE}/ps.json" ;;
  "image inspect") [ -f "${FAKE}/image-pulled" ] && exit 0; exit 1 ;;
  *) exit 1 ;;
esac
EOF
chmod +x "${tmp}/bin/docker"
export FAKE PATH="${tmp}/bin:${PATH}" STACK_PYTHON=host

cat > "${FAKE}/config.json" << 'EOF'
{"name":"boundary-assistant","services":{
 "hermes":{"image":"x","environment":{"API_SERVER_KEY":"do-not-print"},
   "ports":[{"mode":"ingress","host_ip":"127.0.0.1","target":8642,"published":"18642","protocol":"tcp"}]},
 "hermes-api-bootstrap":{"image":"x"},
 "searxng":{"image":"y","ports":[{"host_ip":"127.0.0.1","target":8080,"published":"8080","protocol":"tcp"}]}}}
EOF
: > "${FAKE}/ps.json"

run() { (cd "$stack" && bash scripts/status.sh "$@"); }
field() { python3 -c "import json,sys; d=json.load(sys.stdin); print(eval(sys.argv[1]))" "$1"; }

# Stopped: every service stopped, ports from the config, no secret in the output.
out="$(run --json)" || fail "status failed: $out"
case "$out" in *do-not-print*) fail "status printed an environment value" ;; esac
[ "$(printf '%s' "$out" | field 'd["engine"]')" = True ] || fail "engine: $out"
[ "$(printf '%s' "$out" | field 'd["project"]')" = boundary-assistant ] || fail "project: $out"
[ "$(printf '%s' "$out" | field 'd["images"]["hermes_agent"]')" = v9.9.9 ] || fail "agent pin: $out"
[ "$(printf '%s' "$out" | field '[s["state"] for s in d["services"]]')" = "['stopped', 'stopped', 'stopped']" ] \
  || fail "stopped states: $out"
[ "$(printf '%s' "$out" | field 'd["services"][0]["ports"]')" \
  = "[{'host_ip': '127.0.0.1', 'host': 18642, 'container': 8642, 'protocol': 'tcp'}]" ] \
  || fail "config ports: $out"
[ "$(printf '%s' "$out" | field 'd["version"]')" = unknown ] || fail "version without git: $out"

# Running (JSON lines, Compose >= 2.21): ports from the publishers, deduplicated.
cat > "${FAKE}/ps.json" << 'EOF'
{"Service":"hermes","State":"running","Health":"healthy","Publishers":[{"URL":"127.0.0.1","TargetPort":8642,"PublishedPort":18642,"Protocol":"tcp"},{"URL":"127.0.0.1","TargetPort":8642,"PublishedPort":18642,"Protocol":"tcp"}]}
{"Service":"hermes-api-bootstrap","State":"exited","Publishers":[]}
EOF
out="$(run --json)"
[ "$(printf '%s' "$out" | field '[(s["name"], s["state"]) for s in d["services"]]')" \
  = "[('hermes', 'running'), ('hermes-api-bootstrap', 'stopped'), ('searxng', 'stopped')]" ] \
  || fail "running states: $out"
[ "$(printf '%s' "$out" | field 'len(d["services"][0]["ports"])')" = 1 ] || fail "dedupe: $out"
[ "$(printf '%s' "$out" | field 'd["services"][0]["health"]')" = healthy ] || fail "health: $out"

# Older Compose prints one array.
printf '[{"Service":"searxng","State":"running","Publishers":[{"URL":"127.0.0.1","TargetPort":8080,"PublishedPort":8080,"Protocol":"tcp"}]}]' \
  > "${FAKE}/ps.json"
out="$(run --json)"
[ "$(printf '%s' "$out" | field 'd["services"][2]["state"]')" = running ] || fail "array ps: $out"

# Engine down: states unknown, config ports kept.
touch "${FAKE}/engine-down"
out="$(run --json)"
[ "$(printf '%s' "$out" | field 'd["engine"]')" = False ] || fail "engine down: $out"
[ "$(printf '%s' "$out" | field '{s["state"] for s in d["services"]}')" = "{'unknown'}" ] \
  || fail "engine down states: $out"

# Without STACK_PYTHON=host: the engine down (docker run would fail too), so
# the host's python3 builds it.
out="$(STACK_PYTHON='' run --json)" || fail "engine down, host python3: $out"
[ "$(printf '%s' "$out" | field 'd["engine"]')" = False ] || fail "engine down, host python3: $out"
py_down="$out"

# No usable python3 either (a macOS stub that exits 1): awk builds the same
# JSON, from compact and from indented config.
mkdir -p "${tmp}/nopy"
printf '#!/bin/sh\nexit 1\n' > "${tmp}/nopy/python3"
chmod +x "${tmp}/nopy/python3"
nopy() { PATH="${tmp}/nopy:${PATH}" STACK_PYTHON='' run "$@"; }
out="$(nopy --json)" || fail "engine down, no python3: $out"
[ "$out" = "$py_down" ] || fail "awk output differs:
 awk: $out
 py:  $py_down"
cp "${FAKE}/config.json" "${FAKE}/config.compact"
python3 -m json.tool "${FAKE}/config.compact" > "${FAKE}/config.json"
out="$(nopy --json)" || fail "indented config, no python3: $out"
[ "$out" = "$py_down" ] || fail "awk output differs on indented config: $out"

# The awk reader against stack_status.py on edge cases: a port with no
# published port or a range is dropped, missing host_ip and protocol default,
# names sort by byte, a "ports" key outside services is ignored, and strings
# with escapes and JSON punctuation don't derail it.
cat > "${FAKE}/config.json" << 'EOF'
{"name":"p","x":{"ports":[{"published":"1","target":2}]},"services":{
 "b-svc":{"image":"a\"b\\","labels":{"k":"{[,:]}"},"ports":[
   {"target":9000,"protocol":"udp"},{"target":9001,"published":"9001-9002"},
   {"target":53,"published":5353},{"target":1,"published":"0"}]},
 "a_svc":{"command":["--ports","1"],"ports":[]},
 "B":{}}}
EOF
py_edge="$(STACK_PYTHON='' run --json)" || fail "edge config, python3: $py_edge"
out="$(nopy --json)" || fail "edge config, no python3: $out"
[ "$out" = "$py_edge" ] || fail "awk output differs on edge config:
 awk: $out
 py:  $py_edge"
[ "$(printf '%s' "$out" | field '[s["name"] for s in d["services"]]')" = "['B', 'a_svc', 'b-svc']" ] \
  || fail "edge names: $out"
mv "${FAKE}/config.compact" "${FAKE}/config.json"
rm "${FAKE}/engine-down"

# Engine up, Hermes image not pulled (docker run fails): the host's python3
# builds the normal output.
out="$(STACK_PYTHON='' run --json)" || fail "image missing, host python3: $out"
[ "$(printf '%s' "$out" | field 'd["engine"]')" = True ] || fail "image missing engine: $out"
[ "$(printf '%s' "$out" | field 'd["services"][2]["state"]')" = running ] || fail "image missing states: $out"

# Engine up, image missing, no python3: engine true, states unknown.
out="$(nopy --json)" || fail "image missing, no python3: $out"
[ "$(printf '%s' "$out" | field 'd["engine"]')" = True ] || fail "image missing, no python3 engine: $out"
[ "$(printf '%s' "$out" | field '{s["state"] for s in d["services"]}')" = "{'unknown'}" ] \
  || fail "image missing, no python3 states: $out"

# Image pulled: the image path runs it, so the fake's failing `docker run`
# shows as a failure (the engine-up path is unchanged).
touch "${FAKE}/image-pulled"
if STACK_PYTHON='' run --json > /dev/null 2>&1; then fail "image path not taken"; fi
rm "${FAKE}/image-pulled"

# A tarball install's version stamp.
printf 'v0.3.17\n' > "${stack}/.stack-version"
out="$(run --json)"
[ "$(printf '%s' "$out" | field 'd["version"]')" = v0.3.17 ] || fail "stamp version: $out"

# The overlay flags are gone: the stack runs with its default project and .env.
if run --json --project boundary-assistant > /dev/null 2>&1; then fail "--project accepted"; fi
if run --json --compose-file docker-compose.yml > /dev/null 2>&1; then fail "--compose-file accepted"; fi

# JSON only.
if run > /dev/null 2>&1; then fail "status without --json succeeded"; fi

echo "ok status.sh"
