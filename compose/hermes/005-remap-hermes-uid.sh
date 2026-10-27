#!/command/with-contenv sh
# Remap the hermes user to HERMES_UID/HERMES_GID before the image's
# 01-hermes-setup ("005" sorts before "01").
#
# 01-hermes-setup (/opt/hermes/docker/stage2-hook.sh, set -eu) runs
# `usermod -u "$HERMES_UID" hermes`. usermod writes /etc/passwd first and then
# chowns the home directory tree, which is /opt/data, the data/hermes bind. On
# Docker Desktop's virtiofs that chown fails, usermod exits 12, and the hook
# stops before the GID remap, API_SERVER_KEY generation and the skills sync.
# It only shows on a freshly created container: on a restart the uid in
# /etc/passwd already matches and the usermod branch is skipped.
#
# Editing /etc/passwd here satisfies that check on the first boot too. Nothing
# under /opt/data is chowned, so host ownership is unchanged; stage2's own
# targeted chowns still run after this. The group goes through groupmod, which
# touches no files. Invalid or unset ids are left for stage2 to handle.
set -u

TAG="[005-remap-hermes-uid]"

HERMES_UID="${HERMES_UID:-${PUID:-}}"
HERMES_GID="${HERMES_GID:-${PGID:-}}"

# Same rule as stage2's validate_uid_gid: digits only, 1-65534.
valid_id() {
    case "$1" in
        ''|*[!0-9]*) return 1 ;;
        *) [ "$1" -ge 1 ] && [ "$1" -le 65534 ] ;;
    esac
}

id hermes >/dev/null 2>&1 || exit 0

if valid_id "$HERMES_GID" && [ "$HERMES_GID" != "$(id -g hermes)" ]; then
    echo "$TAG hermes GID $(id -g hermes) -> $HERMES_GID"
    # -o: the GID may already belong to another group in the image.
    groupmod -o -g "$HERMES_GID" hermes || echo "$TAG warn: groupmod exited $?"
fi

if valid_id "$HERMES_UID" && [ "$HERMES_UID" != "$(id -u hermes)" ]; then
    echo "$TAG hermes UID $(id -u hermes) -> $HERMES_UID"
    tmp=/etc/passwd.005-remap
    if awk -F: -v OFS=: -v uid="$HERMES_UID" '$1 == "hermes" { $3 = uid } { print }' \
        /etc/passwd > "$tmp"; then
        # Rewrite in place so /etc/passwd keeps its inode, owner and mode.
        cat "$tmp" > /etc/passwd
    else
        echo "$TAG warn: could not rewrite /etc/passwd"
    fi
    rm -f "$tmp"
fi

# groupmod normally moves the primary gid in /etc/passwd as well; make sure.
gid="$(getent group hermes | cut -d: -f3)"
if [ -n "$gid" ] && [ "$(id -g hermes)" != "$gid" ]; then
    tmp=/etc/passwd.005-remap
    awk -F: -v OFS=: -v gid="$gid" '$1 == "hermes" { $4 = gid } { print }' \
        /etc/passwd > "$tmp" && cat "$tmp" > /etc/passwd
    rm -f "$tmp"
fi

echo "$TAG hermes is $(id -u hermes):$(id -g hermes)"
exit 0
