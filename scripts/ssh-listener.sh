#!/usr/bin/env bash
# Set up and manage the SSH listener: a container that lets a client reach
# this host's sshd over HyperDHT, by public key, without an open TCP port
# (docs/ssh-listener.md).
#
#   ssh-listener.sh setup [--sshd HOST:PORT] [--json]
#   ssh-listener.sh allow CLIENT_KEY [--name LABEL] [--json]
#   ssh-listener.sh revoke CLIENT_KEY [--json]
#   ssh-listener.sh list [--json]
#   ssh-listener.sh status [--json]
#   ssh-listener.sh start|stop [--json]
#   ssh-listener.sh remove --yes [--json]
#
# Self-contained: the copy attached to a release runs on a host with no stack.
# It needs bash, the Docker CLI and a running engine (Docker Engine on Linux,
# Docker Desktop on macOS). Podman is not supported.
#
# The listener runs as the container sovereign-ssh-listener with restart
# policy unless-stopped, outside any compose project, so the stack's scripts
# and `docker compose` leave it alone. Its key pair, sshd address and allowed
# keys live in the volume sovereign-ssh-listener-data.
#
# setup pulls the release's image, makes the key pair the first time, and
# (re)creates the container, keeping the key pair and allowed keys. It copies
# this script to ~/.local/bin/sovereign-ssh-listener for later commands. The
# sshd address defaults to 127.0.0.1:22 with host networking on Docker Engine,
# and host.docker.internal:22 on Docker Desktop, where the container can't
# reach the Mac's loopback; --sshd changes it and later setups keep it.
#
# allow, revoke and list work whether the listener runs or not; a change
# applies to the running listener within a second, and revoke closes that
# key's open streams. status exits 0 whenever it can tell, stopped or not.
# remove deletes the container, the volume and the copied command: a later
# setup makes a new public key.
#
# setup, stop and remove ignore SIGHUP and print only at the end, so they
# finish when run over the listener itself, whose connection they cut.
#
# --json prints one JSON object: status's fields for setup, start, stop and
# status ({"running","setUp","publicKey","sshd","sshdReachable","allowed",
# "version","natType","connections","warnings"}, setup adds "command"),
# {"key","name","added","alreadyAllowed"} for allow, {"key","revoked"} for
# revoke, {"keys":[...]} for list, {"removed":true} for remove. Errors print
# {"error":REASON,"message":TEXT}: bad_input (exit 2), command_failed (exit
# 4), unsupported_host or not_set_up (exit 1).
#
# SSH_LISTENER_IMAGE overrides the image (`make ssh-listener-image` builds
# sovereign-ssh-listener:local).
set -euo pipefail

# The release workflow writes these two lines (scripts/release-ssh-listener.sh).
RELEASE_IMAGE=""
RELEASE_VERSION=""

IMAGE="${SSH_LISTENER_IMAGE:-$RELEASE_IMAGE}"
CONTAINER=sovereign-ssh-listener
VOLUME=sovereign-ssh-listener-data
COMMAND_COPY="${HOME}/.local/bin/sovereign-ssh-listener"
JSON=0

usage() {
  sed -n '7,13p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' >&2
  exit 2
}

json_str() {
  local s="$1"
  s="${s//\\/\\\\}"
  s="${s//\"/\\\"}"
  s="${s//$'\n'/\\n}"
  s="${s//$'\r'/\\r}"
  s="${s//$'\t'/\\t}"
  printf '"%s"' "$s"
}

# fail REASON MESSAGE
fail() {
  if [[ "$JSON" == 1 ]]; then
    printf '{"error":"%s","message":%s}\n' "$1" "$(json_str "$2")"
  else
    printf 'error: %s\n' "$2" >&2
  fi
  case "$1" in
    bad_input) exit 2 ;;
    command_failed) exit 4 ;;
    *) exit 1 ;;
  esac
}

# --- the engine -----------------------------------------------------------------

DESKTOP=0
check_engine() {
  command -v docker > /dev/null 2>&1 || fail unsupported_host "the Docker CLI is not installed"
  if docker --version 2> /dev/null | grep -qi podman; then
    fail unsupported_host "Podman is not supported; use Docker Engine or Docker Desktop"
  fi
  local os
  os="$(docker info --format '{{.OperatingSystem}}' 2> /dev/null)" \
    || fail unsupported_host "the Docker engine is not running"
  case "$os" in *"Docker Desktop"*) DESKTOP=1 ;; esac
}

# Docker Engine: host networking, so 127.0.0.1 is the host's loopback and
# hole punching sees the host's own NAT. Docker Desktop: the default network,
# where host.docker.internal is the Mac.
net_args() {
  [[ "$DESKTOP" == 1 ]] || printf '%s\n' --network host
}

default_sshd() {
  if [[ "$DESKTOP" == 1 ]]; then echo host.docker.internal:22; else echo 127.0.0.1:22; fi
}

unreachable_hint() {
  if [[ "$DESKTOP" == 1 ]]; then
    echo "sshd does not answer: turn on Remote Login in System Settings > General > Sharing"
  else
    echo "sshd does not answer at the stored address: is the SSH server installed and running?"
  fi
}

# container_state: running, stopped or absent.
container_state() {
  local running
  if running="$(docker container inspect -f '{{.State.Running}}' "$CONTAINER" 2> /dev/null)"; then
    if [[ "$running" == true ]]; then echo running; else echo stopped; fi
  else
    echo absent
  fi
}

volume_exists() {
  docker volume inspect "$VOLUME" > /dev/null 2>&1
}

require_image() {
  [[ -n "$IMAGE" ]] || fail bad_input "no image to run: use the ssh-listener.sh attached to a release, or set SSH_LISTENER_IMAGE"
}

# cli ARGS...: the listener's own command line, in the running container, or in
# a throwaway one on the same volume and network. Prints its output; returns
# its exit status.
cli() {
  local state image
  state="$(container_state)"
  if [[ "$state" == running ]]; then
    docker exec "$CONTAINER" node /app/listener.mjs "$@"
    return
  fi
  if [[ "$state" == stopped ]]; then
    image="$(docker container inspect -f '{{.Config.Image}}' "$CONTAINER")"
  else
    require_image
    image="$IMAGE"
  fi
  # shellcheck disable=SC2046  # net_args is zero or two words
  docker run --rm $(net_args) -v "${VOLUME}:/data" "$image" "$@"
}

# pass_through OUTPUT STATUS: print the listener's output; on failure keep its
# exit code (its JSON error already says why).
pass_through() {
  if [[ "$2" == 0 ]]; then
    printf '%s\n' "$1"
    return
  fi
  if [[ "$JSON" == 1 && "$1" == *'"error"'* ]]; then
    printf '%s\n' "$1"
  else
    printf '%s\n' "$1" >&2
  fi
  exit "$2"
}

json_flag() {
  [[ "$JSON" == 1 ]] && echo --json
  return 0
}

status_out() {
  local state running=false out rc=0 extra=("$@")
  state="$(container_state)"
  [[ "$state" == running ]] && running=true
  if [[ "$state" == absent ]] && ! volume_exists; then
    local version=null
    [[ -n "$RELEASE_VERSION" ]] && version="$(json_str "$RELEASE_VERSION")"
    if [[ "$JSON" == 1 ]]; then
      printf '{"running":false,"setUp":false,"publicKey":null,"sshd":null,"sshdReachable":false,"allowed":0,"version":%s,"natType":null,"connections":0,"warnings":[]}\n' "$version"
    else
      echo "The SSH listener is not set up on this host. Run: ssh-listener.sh setup"
    fi
    return 0
  fi
  # shellcheck disable=SC2046  # json_flag is zero or one word
  out="$(cli status --running "$running" --unreachable-hint "$(unreachable_hint)" \
    ${extra[@]+"${extra[@]}"} $(json_flag))" || rc=$?
  pass_through "$out" "$rc"
}

# run_listener IMAGE: start the listener container.
run_listener() {
  # shellcheck disable=SC2046  # net_args is zero or two words
  docker run -d --name "$CONTAINER" --restart unless-stopped $(net_args) \
    -v "${VOLUME}:/data" --label "org.sovereign-agent-stack.ssh-listener=1" "$1" serve
}

# --- commands -------------------------------------------------------------------

cmd_setup() {
  local sshd="" out rc=0 warnings=()
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --sshd)
        [[ $# -ge 2 ]] || fail bad_input "--sshd needs HOST:PORT"
        sshd="$2"
        shift 2
        ;;
      *) fail bad_input "setup: unknown argument $1" ;;
    esac
  done
  require_image
  trap '' HUP

  if ! docker image inspect "$IMAGE" > /dev/null 2>&1; then
    out="$(docker pull "$IMAGE" 2>&1)" || fail command_failed "pulling ${IMAGE} failed: ${out##*$'\n'}"
  fi

  local init=(init --default-sshd "$(default_sshd)" --json)
  [[ -n "$sshd" ]] && init+=(--sshd "$sshd")
  # shellcheck disable=SC2046  # net_args is zero or two words
  out="$(docker run --rm $(net_args) -v "${VOLUME}:/data" "$IMAGE" "${init[@]}")" || rc=$?
  if [[ "$rc" != 0 ]]; then
    [[ "$out" == *'"error"'* ]] || fail command_failed "setting up the key pair failed: ${out}"
    JSON=1 pass_through "$out" "$rc"
  fi

  # Removing the old container cuts a connection made through it; SIGHUP is
  # ignored above, and nothing is printed until the end.
  # If the new container doesn't start, the old image runs again, so the host
  # stays reachable.
  local old_image=""
  old_image="$(docker container inspect -f '{{.Config.Image}}' "$CONTAINER" 2> /dev/null)" || old_image=""
  docker rm -f "$CONTAINER" > /dev/null 2>&1 || true
  if ! out="$(run_listener "$IMAGE" 2>&1)"; then
    local why="${out##*$'\n'}"
    if [[ -n "$old_image" ]]; then
      docker rm -f "$CONTAINER" > /dev/null 2>&1 || true
      run_listener "$old_image" > /dev/null 2>&1 \
        && fail command_failed "starting the listener on ${IMAGE} failed, so ${old_image} runs again: ${why}"
    fi
    fail command_failed "starting the listener failed: ${why}"
  fi

  if [[ -f "${BASH_SOURCE[0]}" ]]; then
    mkdir -p "$(dirname "$COMMAND_COPY")"
    if [[ "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")" != "$COMMAND_COPY" ]]; then
      cp "${BASH_SOURCE[0]}" "${COMMAND_COPY}.tmp" && chmod 755 "${COMMAND_COPY}.tmp" && mv "${COMMAND_COPY}.tmp" "$COMMAND_COPY"
    fi
  fi

  if [[ "$DESKTOP" == 1 ]]; then
    local f
    for f in "${HOME}/Library/Group Containers/group.com.docker/settings-store.json" \
      "${HOME}/Library/Group Containers/group.com.docker/settings.json"; do
      [[ -f "$f" ]] || continue
      if grep -qiE '"autostart"[[:space:]]*:[[:space:]]*false' "$f"; then
        warnings+=(--warning "Docker Desktop does not start when you log in, so the listener stays down after a restart until you open it: turn on \"Start Docker Desktop when you sign in\" in its settings")
      fi
      break
    done
  fi

  out="$(status_out ${warnings[@]+"${warnings[@]}"})" || {
    rc=$?
    printf '%s\n' "$out"
    exit "$rc"
  }
  if [[ "$JSON" == 1 ]]; then
    printf '%s,"command":%s}\n' "${out%\}}" "$(json_str "$COMMAND_COPY")"
  else
    printf '%s\n\n' "$out"
    printf 'Enter the public key above in Boundary, then run the allow command it shows:\n'
    printf '  %s allow CLIENT_KEY --name LABEL\n' "$COMMAND_COPY"
    if [[ "$DESKTOP" == 1 ]]; then
      printf '\nOn a Mac the listener runs while you are logged in and Docker Desktop is running.\n'
    fi
  fi
}

cmd_key() {
  local command="$1" key="${2:-}" out rc=0
  shift 2 || fail bad_input "${command} needs a client key"
  [[ "$key" =~ ^[0-9a-fA-F]{64}$ ]] || fail bad_input "a client key is 32 bytes written as 64 hex characters"
  local args=("$command" "$key")
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --name)
        [[ "$command" == allow && $# -ge 2 ]] || fail bad_input "${command}: unexpected $1"
        args+=(--name "$2")
        shift 2
        ;;
      *) fail bad_input "${command}: unknown argument $1" ;;
    esac
  done
  require_set_up
  # shellcheck disable=SC2046  # json_flag is zero or one word
  out="$(cli "${args[@]}" $(json_flag))" || rc=$?
  pass_through "$out" "$rc"
}

cmd_list() {
  local out rc=0
  require_set_up
  # shellcheck disable=SC2046  # json_flag is zero or one word
  out="$(cli list $(json_flag))" || rc=$?
  pass_through "$out" "$rc"
}

require_set_up() {
  if [[ "$(container_state)" == absent ]] && ! volume_exists; then
    fail not_set_up "the SSH listener is not set up on this host; run setup first"
  fi
}

cmd_start_stop() {
  local out
  [[ "$(container_state)" != absent ]] || fail not_set_up "the SSH listener is not set up on this host; run setup first"
  if [[ "$1" == stop ]]; then
    trap '' HUP
    out="$(docker stop "$CONTAINER" 2>&1)" || fail command_failed "stopping the listener failed: ${out##*$'\n'}"
  else
    out="$(docker start "$CONTAINER" 2>&1)" || fail command_failed "starting the listener failed: ${out##*$'\n'}"
  fi
  status_out
}

cmd_remove() {
  local out
  [[ "${1:-}" == --yes ]] || fail bad_input "remove deletes the listener's key pair, so every client has to be set up again; run it with --yes"
  trap '' HUP
  docker rm -f "$CONTAINER" > /dev/null 2>&1 || true
  if volume_exists; then
    out="$(docker volume rm "$VOLUME" 2>&1)" || fail command_failed "removing ${VOLUME} failed: ${out##*$'\n'}"
  fi
  rm -f "$COMMAND_COPY"
  if [[ "$JSON" == 1 ]]; then
    echo '{"removed":true}'
  else
    echo "Removed the SSH listener, its key pair and its allowed keys."
  fi
}

# --- main -----------------------------------------------------------------------

args=()
for arg in "$@"; do
  if [[ "$arg" == --json ]]; then JSON=1; else args+=("$arg"); fi
done
set -- ${args[@]+"${args[@]}"}
[[ $# -ge 1 ]] || usage
command="$1"
shift

case "$command" in
  setup | allow | revoke | list | status | start | stop | remove) ;;
  -h | --help | help) usage ;;
  *) fail bad_input "unknown command ${command}" ;;
esac

check_engine
case "$command" in
  setup) cmd_setup "$@" ;;
  allow | revoke) cmd_key "$command" "$@" ;;
  list) [[ $# == 0 ]] || fail bad_input "list takes no arguments"; cmd_list ;;
  status) [[ $# == 0 ]] || fail bad_input "status takes no arguments"; status_out ;;
  start | stop) [[ $# == 0 ]] || fail bad_input "${command} takes no arguments"; cmd_start_stop "$command" ;;
  remove) cmd_remove "$@" ;;
esac
