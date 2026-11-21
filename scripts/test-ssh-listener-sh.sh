#!/usr/bin/env bash
# scripts/ssh-listener.sh with a fake docker: the engine checks, the docker
# commands each subcommand runs on Docker Engine and Docker Desktop, that setup
# keeps the volume, the refusals, and scripts/release-ssh-listener.sh writing
# the image and version into the release copy. The listener's own commands are
# tested in compose/ssh-listener/test (scripts/test-ssh-listener.sh).
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=lib/testkit.sh
. "${ROOT}/scripts/lib/testkit.sh"

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
CALLS="${tmp}/calls"
FAKE="${tmp}/fake"
mkdir -p "${tmp}/bin" "$FAKE" "${tmp}/home"
export CALLS FAKE PATH="${tmp}/bin:${PATH}" HOME="${tmp}/home"
unset SSH_LISTENER_IMAGE

KEY="$(printf 'a%.0s' $(seq 64))"

# shellcheck disable=SC2016  # expanded by the fake
cat > "${tmp}/bin/docker" << 'EOF'
#!/bin/sh
echo "$*" >> "$CALLS"
case "$1" in
  --version) if [ -f "$FAKE/podman" ]; then echo "podman version 5"; else echo "Docker version 27"; fi ;;
  info)
    [ ! -f "$FAKE/down" ] || exit 1
    if [ -f "$FAKE/desktop" ]; then echo "Docker Desktop"; else echo "Ubuntu 24.04.1 LTS"; fi ;;
  container)
    [ -f "$FAKE/container" ] || exit 1
    case "$4" in
      *Running*) if [ "$(cat "$FAKE/container")" = running ]; then echo true; else echo false; fi ;;
      *Image*) echo img:old ;;
    esac ;;
  volume)
    case "$2" in
      inspect) [ -f "$FAKE/volume" ] || exit 1 ;;
      rm) rm -f "$FAKE/volume" ;;
    esac ;;
  image) [ -f "$FAKE/image" ] || exit 1 ;;
  pull) touch "$FAKE/image" ;;
  rm) rm -f "$FAKE/container" ;;
  stop) echo stopped > "$FAKE/container" ;;
  start) echo running > "$FAKE/container" ;;
  run | exec)
    touch "$FAKE/volume"
    case " $* " in *" -d "*) echo running > "$FAKE/container"; echo cid; exit 0 ;; esac
    running=false
    for a in "$@"; do
      if [ "${prev:-}" = --running ]; then running="$a"; fi
      prev="$a"
    done
    for a in "$@"; do
      case "$a" in
        init)
          if [ -f "$FAKE/init-fails" ]; then echo '{"error":"bad_input","message":"--sshd x is not HOST:PORT"}'; exit 2; fi
          echo '{"publicKey":"pk","created":true,"sshd":"x"}'; exit 0 ;;
        status) echo "{\"running\":${running},\"setUp\":true,\"publicKey\":\"pk\",\"warnings\":[]}"; exit 0 ;;
        allow) echo '{"key":"k","alreadyAllowed":false}'; exit 0 ;;
        revoke) echo '{"key":"k","revoked":true}'; exit 0 ;;
        list) echo '{"keys":[]}'; exit 0 ;;
      esac
    done
    echo "fake docker: no listener command in $*" >&2; exit 1 ;;
  *) echo "unexpected docker $*" >&2; exit 1 ;;
esac
EOF
chmod +x "${tmp}/bin/docker"

L() { bash "${ROOT}/scripts/ssh-listener.sh" "$@"; }
reset() {
  rm -rf "${FAKE:?}" "${tmp:?}/home"
  mkdir -p "$FAKE" "${tmp}/home"
  : > "$CALLS"
}
called() { grep -qxF -- "$1" "$CALLS" || fail "no docker call '$1' in: $(cat "$CALLS")"; }
not_called() { if grep -q -- "$1" "$CALLS"; then fail "unexpected docker call matching '$1': $(cat "$CALLS")"; fi; }

# expect RC ERROR ARGS...: the run exits RC and prints {"error":ERROR,...}.
expect() {
  want_rc="$1" want="$2"
  shift 2
  set +e
  out="$(L "$@" --json 2> /dev/null)"
  rc=$?
  set -e
  [ "$rc" = "$want_rc" ] || fail "$* exit $rc, want $want_rc: $out"
  case "$out" in *"\"error\":\"${want}\""*) ;; *) fail "$*: want ${want}, got $out" ;; esac
}

# --- refusals -------------------------------------------------------------------
reset
set +e
L > /dev/null 2>&1
rc=$?
set -e
[ "$rc" = 2 ] || fail "no command: exit $rc, want 2"
expect 2 bad_input frobnicate
not_called info
touch "$FAKE/down"
expect 1 unsupported_host status
rm "$FAKE/down"
touch "$FAKE/podman"
expect 1 unsupported_host status
rm "$FAKE/podman"
expect 2 bad_input setup
expect 2 bad_input allow abc
expect 2 bad_input allow
expect 2 bad_input revoke "$KEY" --name x
expect 2 bad_input status extra
expect 1 not_set_up allow "$KEY"
expect 1 not_set_up start
expect 2 bad_input remove
not_called '^exec \|^run \|^rm '

# --- status before setup --------------------------------------------------------
out="$(L status --json)"
case "$out" in *'"running":false,"setUp":false'*'"version":null'*) ;; *) fail "status before setup: $out" ;; esac
not_called '^run \|^exec '

# --- setup on Docker Engine -----------------------------------------------------
export SSH_LISTENER_IMAGE=img:new
out="$(L setup --json)"
called "pull img:new"
called "run --rm --network host -v sovereign-ssh-listener-data:/data img:new init --default-sshd 127.0.0.1:22 --json"
called "rm -f sovereign-ssh-listener"
called "run -d --name sovereign-ssh-listener --restart unless-stopped --network host -v sovereign-ssh-listener-data:/data --label org.sovereign-agent-stack.ssh-listener=1 img:new serve"
called "exec sovereign-ssh-listener node /app/listener.mjs status --running true --unreachable-hint sshd does not answer at the stored address: is the SSH server installed and running? --json"
not_called volume\ rm
case "$out" in *'"running":true'*'"command":"'"${HOME}"'/.local/bin/sovereign-ssh-listener"}') ;; *) fail "setup output: $out" ;; esac
[ -x "${HOME}/.local/bin/sovereign-ssh-listener" ] || fail "setup did not copy the command"
cmp -s "${HOME}/.local/bin/sovereign-ssh-listener" "${ROOT}/scripts/ssh-listener.sh" || fail "the copied command differs"

# Again, with an address: the image is present, the volume stays.
: > "$CALLS"
L setup --sshd 10.0.0.2:2222 --json > /dev/null
not_called pull
called "run --rm --network host -v sovereign-ssh-listener-data:/data img:new init --default-sshd 127.0.0.1:22 --json --sshd 10.0.0.2:2222"
not_called volume\ rm

touch "$FAKE/init-fails"
expect 2 bad_input setup --sshd x
rm "$FAKE/init-fails"

# --- key commands, running and stopped ------------------------------------------
: > "$CALLS"
L allow "$KEY" --name "my laptop" --json > /dev/null
called "exec sovereign-ssh-listener node /app/listener.mjs allow ${KEY} --name my laptop --json"
L revoke "$KEY" > /dev/null
called "exec sovereign-ssh-listener node /app/listener.mjs revoke ${KEY}"

out="$(L stop --json)"
called "stop sovereign-ssh-listener"
case "$out" in *'"running":false'*) ;; *) fail "stop output: $out" ;; esac
: > "$CALLS"
L list --json > /dev/null
called "run --rm --network host -v sovereign-ssh-listener-data:/data img:old list --json"
out="$(L start --json)"
called "start sovereign-ssh-listener"
case "$out" in *'"running":true'*) ;; *) fail "start output: $out" ;; esac

# --- remove ---------------------------------------------------------------------
: > "$CALLS"
out="$(L remove --yes --json)"
[ "$out" = '{"removed":true}' ] || fail "remove output: $out"
called "rm -f sovereign-ssh-listener"
called "volume rm sovereign-ssh-listener-data"
[ ! -e "${HOME}/.local/bin/sovereign-ssh-listener" ] || fail "remove left the command copy"
case "$(L status --json)" in *'"setUp":false'*) ;; *) fail "status after remove" ;; esac

# --- Docker Desktop -------------------------------------------------------------
reset
touch "$FAKE/desktop"
settings="${HOME}/Library/Group Containers/group.com.docker/settings-store.json"
mkdir -p "$(dirname "$settings")"
printf '{\n  "AutoStart": false\n}\n' > "$settings"
out="$(L setup)"
called "run --rm -v sovereign-ssh-listener-data:/data img:new init --default-sshd host.docker.internal:22 --json"
called "run -d --name sovereign-ssh-listener --restart unless-stopped -v sovereign-ssh-listener-data:/data --label org.sovereign-agent-stack.ssh-listener=1 img:new serve"
not_called "network host"
grep -q 'status --running true --unreachable-hint sshd does not answer: turn on Remote Login.*--warning Docker Desktop does not start when you log in' "$CALLS" \
  || fail "setup on Docker Desktop: no autostart warning or Remote Login hint: $(cat "$CALLS")"
case "$out" in *"while you are logged in and Docker Desktop is running"*) ;; *) fail "setup on Docker Desktop: $out" ;; esac

# --- the release copy -----------------------------------------------------------
out_dir="${tmp}/dist"
bash "${ROOT}/scripts/release-ssh-listener.sh" v1.2.3 ghcr.io/o/r/ssh-listener@sha256:abc "$out_dir" > /dev/null
rel="${out_dir}/ssh-listener.sh"
grep -qx 'RELEASE_IMAGE="ghcr.io/o/r/ssh-listener@sha256:abc"' "$rel" || fail "release copy lacks the image"
grep -qx 'RELEASE_VERSION="v1.2.3"' "$rel" || fail "release copy lacks the version"
[ "$(diff "${ROOT}/scripts/ssh-listener.sh" "$rel" | grep -c '^>')" = 2 ] || fail "release copy changed more than two lines"
sum="$(cut -d' ' -f1 "${rel}.sha256")"
if command -v sha256sum > /dev/null 2>&1; then got="$(sha256sum "$rel" | cut -d' ' -f1)"; else got="$(shasum -a 256 "$rel" | cut -d' ' -f1)"; fi
[ "$sum" = "$got" ] || fail "release copy checksum"
reset
unset SSH_LISTENER_IMAGE
out="$(bash "$rel" status --json)"
case "$out" in *'"version":"v1.2.3"'*) ;; *) fail "release copy status: $out" ;; esac
set +e
bash "${ROOT}/scripts/release-ssh-listener.sh" v1.2.3 'bad ref' "$out_dir" > /dev/null 2>&1
rc=$?
set -e
[ "$rc" = 2 ] || fail "release-ssh-listener.sh took a ref without a digest"

echo "ok: ssh-listener.sh"
