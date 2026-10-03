#!/bin/sh
# sshd runs as root so it can bind port 22, then drops to the hermes user.
# The only environment is uid, gid, and timezone. No secrets.
set -eu

uid="${HERMES_UID:-1000}"
gid="${HERMES_GID:-1000}"

# The requested gid may already belong to an Alpine system group (macOS
# staff is 20). File ownership uses the number, so reuse that group.
group_name="$(awk -F: -v g="$gid" '$3 == g { print $1; exit }' /etc/group)"
if [ -z "$group_name" ]; then
  addgroup -g "$gid" hermes
  group_name=hermes
fi
if ! id hermes >/dev/null 2>&1; then
  if awk -F: -v u="$uid" '$3 == u { found = 1 } END { exit !found }' /etc/passwd; then
    echo "[hermes-worker] uid ${uid} already exists" >&2
    exit 1
  fi
  adduser -D -u "$uid" -G "$group_name" -h /home/hermes -s /bin/sh hermes
  # adduser locks the account (password '!'). sshd then rejects public-key
  # logins. An empty password field is unlocked and still has no password.
  passwd -u hermes >/dev/null
fi

if [ ! -s /etc/hermes-worker/authorized_keys ]; then
  echo "[hermes-worker] missing authorized_keys" >&2
  exit 1
fi
chmod 644 /etc/hermes-worker/authorized_keys 2>/dev/null || true
mkdir -p /home/hermes /run
chown hermes:"$group_name" /home/hermes

exec /usr/sbin/sshd -D -e
