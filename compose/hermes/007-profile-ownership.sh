#!/command/with-contenv sh
# Leave nothing under $HERMES_HOME/profiles that the image's 01-hermes-setup
# would chown ("007" sorts before "01").
#
# 01-hermes-setup (/opt/hermes/docker/stage2-hook.sh) runs `chown -R` over
# profiles/ on every boot while anything under it is not owned by hermes. On
# Docker Desktop's virtiofs a socket owned by root cannot be chowned, so the
# walk failed and ran again on every boot; root-owned files reappear too (the
# cron ticker files). The walk takes about 5 s on a quiet machine and took
# 55 s while the rest of the stack was starting, since hermes no longer waits
# for its MCP sidecars.
#
# This step removes sockets left where nothing binds them any more: the
# gateway control socket is on /tmp (HERMES_CONTROL_SOCKET_IN_TMP,
# patch-control-socket-tmp.py) and so are the browser-harness ones
# (BH_RUNTIME_DIR). No process in this container has started yet, so each one
# was left by an earlier container. A socket that outlived a Docker VM restart
# is listed but cannot be stat'ed; it is removed as well. Then it chowns the
# files and directories that are not hermes's, which finds only those few.
set -u

TAG="[007-profile-ownership]"
HOME_DIR="${HERMES_HOME:-/opt/data}"
OWNER_UID="${PROFILE_OWNER_UID:-$(id -u hermes 2>/dev/null)}"
OWNER_GID="${PROFILE_OWNER_GID:-$(id -g hermes 2>/dev/null)}"

# Homes: the default profile and every named one.
set -- "$HOME_DIR"
for dir in "$HOME_DIR"/profiles/*/; do
  [ -d "$dir" ] && set -- "$@" "${dir%/}"
done

# listed <path>: the directory has an entry by that name, readable or not
# (a glob reads the directory without stat'ing its entries).
listed() {
  for _entry in "${1%/*}"/* "${1%/*}"/.[!.]*; do
    [ "${_entry##*/}" = "${1##*/}" ] && return 0
  done
  return 1
}

removed=0
for home in "$@"; do
  candidates=""
  [ "${HERMES_CONTROL_SOCKET_IN_TMP:-0}" = 1 ] && candidates="${home}/gateway.sock"
  case "${BH_RUNTIME_DIR:-}" in
    ''|"$HOME_DIR"*) ;;
    *) candidates="${candidates} ${home}/home/.config/browser-harness/runtime/*.sock" ;;
  esac
  for sock in $candidates; do
    if [ -S "$sock" ]; then
      :
    elif [ -e "$sock" ] || ! listed "$sock"; then
      continue  # a regular file, or no entry (an unmatched glob)
    fi
    if rm -f "$sock" 2>/dev/null && ! listed "$sock"; then
      removed=$((removed + 1))
    else
      echo "$TAG warn: cannot remove stale socket ${sock#"$HOME_DIR"/}; delete it under data/hermes on the host"
    fi
  done
done
[ "$removed" -gt 0 ] && echo "$TAG removed ${removed} stale socket(s)"

if [ -n "$OWNER_UID" ] && [ -n "$OWNER_GID" ] && [ -d "$HOME_DIR/profiles" ]; then
  fixed="$(find "$HOME_DIR/profiles" \( -type f -o -type d \) \
    \( ! -uid "$OWNER_UID" -o ! -gid "$OWNER_GID" \) \
    -exec chown "$OWNER_UID:$OWNER_GID" {} + -print 2>/dev/null | wc -l | tr -d ' ')"
  [ "$fixed" -gt 0 ] && echo "$TAG chowned ${fixed} file(s) under profiles/ to hermes"
fi
exit 0
