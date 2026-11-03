#!/command/with-contenv sh
# Remove sockets left in data/hermes by older layouts, before the image's
# 01-hermes-setup ("007" sorts before "01").
#
# 01-hermes-setup (/opt/hermes/docker/stage2-hook.sh) runs `chown -R` over
# $HERMES_HOME/profiles on every boot while anything under it is not owned by
# hermes. A socket there owned by root cannot be chowned on Docker Desktop's
# virtiofs, so the walk ran on every boot and never finished the job. It takes
# about 5 s on a quiet machine and took 55 s while the rest of the stack was
# starting, since hermes no longer waits for its MCP sidecars.
#
# Nothing binds sockets at these paths any more: the gateway control socket is
# on /tmp (HERMES_CONTROL_SOCKET_IN_TMP, patch-control-socket-tmp.py) and so are
# the browser-harness ones (BH_RUNTIME_DIR). Every socket found there was left
# by an earlier container, and no process in this one has started yet.
set -u

TAG="[007-clear-stale-sockets]"
HOME_DIR="${HERMES_HOME:-/opt/data}"

# Homes: the default profile and every named one.
set -- "$HOME_DIR"
for dir in "$HOME_DIR"/profiles/*/; do
  [ -d "$dir" ] && set -- "$@" "${dir%/}"
done

removed=0
for home in "$@"; do
  candidates=""
  [ "${HERMES_CONTROL_SOCKET_IN_TMP:-0}" = 1 ] && candidates="${home}/gateway.sock"
  case "${BH_RUNTIME_DIR:-}" in
    ''|"$HOME_DIR"*) ;;
    *) candidates="${candidates} ${home}/home/.config/browser-harness/runtime/*.sock" ;;
  esac
  for sock in $candidates; do
    [ -S "$sock" ] || continue
    if rm -f "$sock" 2>/dev/null && [ ! -e "$sock" ]; then
      removed=$((removed + 1))
    else
      echo "$TAG warn: cannot remove stale socket ${sock#"$HOME_DIR"/}; delete it under data/hermes on the host"
    fi
  done
done
[ "$removed" -gt 0 ] && echo "$TAG removed ${removed} stale socket(s)"
exit 0
