# shellcheck shell=bash
# The stack's config lock, held by scripts/addon.sh (configure, enable,
# disable, connect, disconnect, remove) and scripts/env.sh while they change
# .env or add-on state.
#
# The lock is a symlink, <data root>/addons/.lock, whose target names its
# holder: `PID:CLIENT:EPOCH` (a lock from before 0.3.17 is a bare PID).
# `ln -s` creates it and its holder in one atomic step. A lock whose PID no
# longer runs is taken over under a guard directory, .lock.takeover: whoever
# makes the guard re-reads the link, replaces it only if it still names the
# same dead PID, and removes the guard. A guard older than 10 seconds belongs
# to a takeover that died and is removed. Anything else (a live holder, a held
# guard, a link replaced in the meantime) is busy.
#
# STACK_CLIENT names the caller (letters, digits, `.`, `_`, `-`, up to 64;
# anything else is dropped), so a busy caller learns which client holds the
# lock. STACK_LOCK_TIMEOUT is how many seconds to keep retrying a busy lock,
# once a second, before giving up; 0 (the default) gives up at once.
#
# bash 3.2 compatible.

STACK_LOCK_PATH=""
STACK_LOCK_GUARD=""
# After a busy stack_lock: the holder's PID, client and start (UTC ISO 8601),
# each empty when unknown.
STACK_LOCK_HOLDER_PID=""
STACK_LOCK_HOLDER_CLIENT=""
STACK_LOCK_HOLDER_SINCE=""

# stack_lock_client: STACK_CLIENT cleaned.
stack_lock_client() {
  printf '%s' "${STACK_CLIENT:-}" | LC_ALL=C tr -cd 'A-Za-z0-9._-' | cut -c1-64
}

# path_age PATH: seconds since PATH's mtime.
path_age() {
  local m
  m="$(stat -c %Y "$1" 2> /dev/null || stat -f %m "$1" 2> /dev/null)" || return 1
  [[ "$m" =~ ^[0-9]+$ ]] || return 1
  echo $(($(date +%s) - m))
}

_stack_lock_iso() {
  date -u -d "@$1" +%Y-%m-%dT%H:%M:%SZ 2> /dev/null || date -u -r "$1" +%Y-%m-%dT%H:%M:%SZ 2> /dev/null || true
}

# _stack_lock_holder TARGET: sets the STACK_LOCK_HOLDER_* fields from a link
# target.
_stack_lock_holder() {
  local t="$1" rest
  STACK_LOCK_HOLDER_PID="${t%%:*}"
  STACK_LOCK_HOLDER_CLIENT=""
  STACK_LOCK_HOLDER_SINCE=""
  if [[ "$t" == *:* ]]; then
    rest="${t#*:}"
    STACK_LOCK_HOLDER_CLIENT="${rest%%:*}"
    [[ "$rest" == *:* && "${rest#*:}" =~ ^[0-9]+$ ]] \
      && STACK_LOCK_HOLDER_SINCE="$(_stack_lock_iso "${rest#*:}")"
  fi
  [[ "$STACK_LOCK_HOLDER_PID" =~ ^[0-9]+$ ]] || STACK_LOCK_HOLDER_PID=""
  return 0
}

# _stack_lock_try LOCK: 0 when this process now holds LOCK, 1 when it is busy
# (holder fields set), 2 when LOCK is something other than a lock symlink.
_stack_lock_try() {
  local lock="$1" guard="${1}.takeover" me target pid age took=0
  me="$$:$(stack_lock_client):$(date +%s)"
  # ln -s onto a directory would create the link inside it.
  [[ -e "$lock" && ! -L "$lock" ]] && return 2
  if ln -sn "$me" "$lock" 2> /dev/null; then
    STACK_LOCK_PATH="$lock"
    return 0
  fi
  target="$(readlink "$lock" 2> /dev/null || true)"
  _stack_lock_holder "$target"
  pid="$STACK_LOCK_HOLDER_PID"
  [[ -n "$pid" ]] || return 1
  kill -0 "$pid" 2> /dev/null && return 1
  if age="$(path_age "$guard")" && [[ "$age" -gt 10 ]]; then
    rmdir "$guard" 2> /dev/null || true
  fi
  mkdir "$guard" 2> /dev/null || return 1
  STACK_LOCK_GUARD="$guard"
  if [[ "$(readlink "$lock" 2> /dev/null || true)" == "$target" ]] && rm -f "$lock" \
    && ln -sn "$me" "$lock" 2> /dev/null; then
    took=1
    STACK_LOCK_PATH="$lock"
  fi
  rmdir "$guard" 2> /dev/null || true
  STACK_LOCK_GUARD=""
  [[ "$took" == 1 ]] && return 0
  _stack_lock_holder "$(readlink "$lock" 2> /dev/null || true)"
  return 1
}

# stack_lock LOCK: takes LOCK, retrying a busy lock for STACK_LOCK_TIMEOUT
# seconds. Returns 0 when held, 1 when busy (STACK_LOCK_HOLDER_* say who has
# it), 2 when LOCK is not a lock symlink.
stack_lock() {
  local lock="$1" timeout="${STACK_LOCK_TIMEOUT:-0}" waited=0 rc
  [[ "$timeout" =~ ^[0-9]+$ ]] || timeout=0
  mkdir -p "$(dirname "$lock")"
  while :; do
    rc=0
    _stack_lock_try "$lock" || rc=$?
    [[ "$rc" == 1 && "$waited" -lt "$timeout" ]] || return "$rc"
    sleep 1
    waited=$((waited + 1))
  done
}

# stack_lock_busy_text: "pid P, client C, since T" from the holder fields,
# leaving out what is unknown.
stack_lock_busy_text() {
  local out=""
  [[ -n "$STACK_LOCK_HOLDER_PID" ]] && out="pid ${STACK_LOCK_HOLDER_PID}"
  [[ -n "$STACK_LOCK_HOLDER_CLIENT" ]] && out="${out:+${out}, }client ${STACK_LOCK_HOLDER_CLIENT}"
  [[ -n "$STACK_LOCK_HOLDER_SINCE" ]] && out="${out:+${out}, }since ${STACK_LOCK_HOLDER_SINCE}"
  printf '%s' "$out"
}

# stack_lock_holder_json: the holder as a JSON object with null for unknowns.
stack_lock_holder_json() {
  local pid=null client=null since=null
  [[ -n "$STACK_LOCK_HOLDER_PID" ]] && pid="$STACK_LOCK_HOLDER_PID"
  [[ -n "$STACK_LOCK_HOLDER_CLIENT" ]] && client="\"${STACK_LOCK_HOLDER_CLIENT}\""
  [[ -n "$STACK_LOCK_HOLDER_SINCE" ]] && since="\"${STACK_LOCK_HOLDER_SINCE}\""
  printf '{"pid":%s,"client":%s,"since":%s}' "$pid" "$client" "$since"
}

# stack_unlock: removes the lock while it still names this process, and the
# guard if this process holds it. Safe to call more than once.
stack_unlock() {
  local t
  if [[ -n "$STACK_LOCK_PATH" ]]; then
    t="$(readlink "$STACK_LOCK_PATH" 2> /dev/null || true)"
    [[ "${t%%:*}" == "$$" ]] && rm -f "$STACK_LOCK_PATH"
  fi
  STACK_LOCK_PATH=""
  if [[ -n "$STACK_LOCK_GUARD" ]]; then
    rmdir "$STACK_LOCK_GUARD" 2> /dev/null || true
  fi
  STACK_LOCK_GUARD=""
}
