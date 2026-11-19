#!/command/with-contenv sh
# OpenSSH refuses a private key that is not mode 600 and owned by the user
# running ssh. The compose mount is read-only, so copy it to a path the
# hermes user owns. That path is not under /opt/data.
#
# The owner comes from the hermes user, not HERMES_UID: the image's stage2
# hook and cont-init 005 have already remapped hermes to HERMES_UID, and a
# script without with-contenv sees no environment at all (CR 0021).
set -eu

src=/opt/hermes-ssh/id_ed25519
dest_dir=/var/lib/hermes-worker-key
dest="${dest_dir}/id_ed25519"
uid="$(id -u hermes)"
gid="$(id -g hermes)"

if [ ! -f "$src" ]; then
  echo "[worker-key] missing ${src}; terminal ssh will fail until setup creates the key"
  exit 0
fi

mkdir -p "$dest_dir"
cp "$src" "$dest"
chown "${uid}:${gid}" "$dest"
chmod 600 "$dest"

# Seed known_hosts so the first shell does not stop on a host-key prompt.
# The worker is a dependency and should already be accepting connections.
# /opt/data persists, so replace the worker's entries rather than appending a
# copy on every start. A failed scan leaves the existing entries alone, and
# entries for other hosts are never touched.
known=/opt/data/.ssh/known_hosts
mkdir -p /opt/data/.ssh
if command -v ssh-keyscan >/dev/null 2>&1; then
  scan="$(mktemp)"
  if ssh-keyscan -T 5 -p 22 hermes-worker > "$scan" 2>/dev/null && [ -s "$scan" ]; then
    if [ -f "$known" ]; then
      ssh-keygen -R hermes-worker -f "$known" >/dev/null 2>&1 || true
      rm -f "${known}.old"
    fi
    cat "$scan" >> "$known"
  fi
  rm -f "$scan"
  touch "$known"
  chown "${uid}:${gid}" /opt/data/.ssh /opt/data/.ssh/known_hosts 2>/dev/null || true
  chmod 700 /opt/data/.ssh 2>/dev/null || true
  chmod 600 /opt/data/.ssh/known_hosts 2>/dev/null || true
fi
