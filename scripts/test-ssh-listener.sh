#!/bin/sh
# The SSH listener's own tests (compose/ssh-listener/test): allowed and refused
# clients, allow and revoke without a restart, the stored key pair, and its
# command line, against a local HyperDHT testnet, plus an ssh2 client signing in
# to a real OpenSSH sshd through the listener. They run in the image's test
# stage, which has sshd and ssh2, so the host needs Docker but not Node.
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
if ! docker info > /dev/null 2>&1; then
  echo "SKIP ssh-listener: no Docker daemon"
  exit 77
fi
docker build --target test "${ROOT}/compose/ssh-listener"
