#!/bin/sh
# Start OpenCode as OPENCODE_UID:OPENCODE_GID instead of root.
#
# Compose sets both to HERMES_UID/HERMES_GID, which setup.sh sets to the
# installing user on Linux. Files the coding agent writes into a coding root
# are then owned by that user, like the ones hermes-worker writes, and the user
# (or a client syncing over SFTP) can change and delete them. As root they were
# owned by root on a Linux host.
#
# The container starts as root only to give the user a passwd entry (git and
# ssh look it up) and to hand it the state folders under HOME. Those are named
# volumes, so chown works on Docker Desktop too; nothing bind-mounted is touched.
set -eu

TAG="[opencode-entrypoint]"
uid="${OPENCODE_UID:-}"
gid="${OPENCODE_GID:-}"

valid_id() {
    case "$1" in
        '' | *[!0-9]*) return 1 ;;
        *) [ "$1" -ge 1 ] && [ "$1" -le 65534 ] ;;
    esac
}

if [ "$(id -u)" != 0 ]; then
    exec opencode "$@"
fi

if ! valid_id "$uid" || ! valid_id "$gid"; then
    echo "$TAG OPENCODE_UID and OPENCODE_GID must be 1-65534 (got '${uid}' and '${gid}')" >&2
    exit 1
fi

if ! getent group "$gid" >/dev/null; then
    echo "opencode:x:${gid}:" >> /etc/group
fi
if ! getent passwd "$uid" >/dev/null; then
    echo "opencode:x:${uid}:${gid}:opencode:${HOME}:/bin/sh" >> /etc/passwd
fi

# The parents are not recursive: only the volumes hold earlier state.
for dir in "$HOME" "$HOME/.config" "$HOME/.local" "$HOME/.local/share" "$HOME/.local/state" "$HOME/.cache"; do
    mkdir -p "$dir"
    chown "${uid}:${gid}" "$dir"
done
for dir in "$HOME/.config/opencode" "$HOME/.local/share/opencode" "$HOME/.local/state/opencode" "$HOME/.cache/opencode"; do
    if [ ! -d "$dir" ]; then
        mkdir -p "$dir"
        chown "${uid}:${gid}" "$dir"
    # A volume a root OpenCode wrote to is handed over once.
    elif [ "$(stat -c %u "$dir")" != "$uid" ] || [ "$(stat -c %g "$dir")" != "$gid" ]; then
        echo "$TAG giving ${dir} to ${uid}:${gid}"
        chown -R "${uid}:${gid}" "$dir"
    fi
done

exec su-exec "${uid}:${gid}" opencode "$@"
