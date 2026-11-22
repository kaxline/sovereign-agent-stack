#!/usr/bin/env bash
# scripts/backup.sh create and restore on a synthetic stack, with a fake docker
# and the host's tar (BACKUP_TAR=host): the parts and what each leaves out,
# the running-stack refusal and --stop, and a restore that moves data aside
# and puts back what create left out.
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=lib/testkit.sh
. "${ROOT}/scripts/lib/testkit.sh"

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
stack="${tmp}/stack"
mkdir -p "${stack}/scripts/lib" "${tmp}/bin"
cp "${ROOT}/scripts/backup.sh" "${stack}/scripts/"
cp "${ROOT}/scripts/lib/env.sh" "${ROOT}/scripts/lib/data-root.sh" \
  "${ROOT}/scripts/lib/python.sh" "${ROOT}/scripts/lib/json-lines.sh" "${stack}/scripts/lib/"

# Synthetic data: a Hermes state file, a Postgres dir, things create leaves
# out, a data root and a *DATA_DIR outside the stack.
mkdir -p "${stack}/data/hermes" "${stack}/data/db" "${stack}/data/mail" \
  "${stack}/data/hermes.bak-old" "${stack}/data/worktree-backups"
printf 'state-v1\n' > "${stack}/data/hermes/state.txt"
printf 'pg-v1\n' > "${stack}/data/db/PG_VERSION"
printf 'index\n' > "${stack}/data/mail/index.txt"
printf 'old\n' > "${stack}/data/hermes.bak-old/x"
mkdir -p "${tmp}/userdata/memory" "${tmp}/signal" "${tmp}/vikunja-files" "${tmp}/mail-index" \
  "${tmp}/userdata/coding-roots/r1" "${stack}/data/coding-roots/r2"
printf 'memory-v1\n' > "${tmp}/userdata/memory/m.md"
printf 'synced\n' > "${tmp}/userdata/coding-roots/r1/f"
printf 'synced\n' > "${stack}/data/coding-roots/r2/f"
printf 'acct\n' > "${tmp}/signal/account"
printf 'attachment\n' > "${tmp}/vikunja-files/1"
printf 'index\n' > "${tmp}/mail-index/i"
cat > "${stack}/.env" << EOF
ASSISTANT_DATA_ROOT=${tmp}/userdata
SIGNAL_CLI_DATA_DIR=${tmp}/signal
TWENTY_DATA_DIR=./data/twenty
VIKUNJA_FILES_DIR=${tmp}/vikunja-files
MAIL_INDEX_DIR=${tmp}/mail-index
LIGHTRAG_KEY=${tmp}/vikunja-files
SECRET_TOKEN=s3cret
EOF
# Bind sources from .env: VIKUNJA_FILES_DIR is found here though its name is no
# *DATA_DIR; MAIL_INDEX_DIR is on the skip list; an environment entry is no
# bind mount.
mkdir -p "${stack}/compose/vikunja"
cat > "${stack}/docker-compose.yml" << 'EOF'
services:
  mail:
    volumes:
      - ${MAIL_INDEX_DIR:-./data/mail}:/data/mail
    environment:
      - ${LIGHTRAG_KEY:-x}
EOF
cat > "${stack}/compose/vikunja/compose.yml" << 'EOF'
services:
  vikunja:
    volumes:
      - "${VIKUNJA_FILES_DIR:-./data/vikunja/files}:/app/vikunja/files"
EOF
printf 'local override\n' > "${stack}/docker-compose.override.yml"

# Fake docker: volumes are folders under $FAKE/volumes, running services a
# file, and every compose call is logged.
FAKE="${tmp}/fake"
mkdir -p "${FAKE}/volumes/proj_n8n_storage" "${FAKE}/volumes/proj_neo4j_data" \
  "${FAKE}/volumes/proj_email_index" "${FAKE}/volumes/proj_search_gate_data" \
  "${FAKE}/volumes/proj_hermes_uv_cache"
printf 'workflow\n' > "${FAKE}/volumes/proj_n8n_storage/db.sqlite"
printf 'graph\n' > "${FAKE}/volumes/proj_neo4j_data/store"
# Compose's volume list: two to back up, three on the skip list, and one
# (postgres_storage) that was never created.
printf 'n8n_storage\nemail_index\nneo4j_data\nhermes_uv_cache\npostgres_storage\nsearch_gate_data\n' > "${FAKE}/compose-volumes"
: > "${FAKE}/running"
cat > "${tmp}/bin/docker" << 'EOF'
#!/bin/sh
echo "$*" >> "${FAKE}/log"
case "$*" in
  "compose --profile * config --volumes") [ -f "${FAKE}/compose-down" ] && exit 1; cat "${FAKE}/compose-volumes"; exit 0 ;;
esac
case "$1 $2" in
  "compose config") printf '{\n  "name": "proj",\n  "services": {}\n}\n' ;;
  "compose ps") cat "${FAKE}/running" ;;
  "compose stop") : > "${FAKE}/running" ;;
  "compose start") shift 2; printf '%s\n' "$@" > "${FAKE}/running" ;;
  "volume ls")
    want=""
    for a in "$@"; do
      case "$a" in label=com.docker.compose.volume=*) want="${a#*volume=}" ;; esac
    done
    [ -d "${FAKE}/volumes/proj_${want}" ] && echo "proj_${want}"
    exit 0 ;;
  "volume inspect") eval "v=\$$#"; echo "${FAKE}/volumes/${v}" ;;
  "volume create") eval "v=\$$#"; mkdir -p "${FAKE}/volumes/${v}"; echo "$v" ;;
  *) echo "unexpected docker $*" >&2; exit 1 ;;
esac
EOF
chmod +x "${tmp}/bin/docker"
export FAKE PATH="${tmp}/bin:${PATH}" BACKUP_TAR=host

run() { (cd "$stack" && bash scripts/backup.sh "$@"); }
listing() { tar -tzf "$1" | sed 's|^\./||; s|/$||' | sort; }

# --plan writes nothing and names every part.
out="$(run --plan --json)"
[ ! -d "${stack}/backups" ] || fail "--plan wrote a backup"
for p in stack data data-root dir-SIGNAL_CLI_DATA_DIR dir-VIKUNJA_FILES_DIR volume-n8n_storage volume-neo4j_data; do
  case "$out" in *"\"part\":\"${p}\""*) ;; *) fail "--plan lacks ${p}: $out" ;; esac
done
for p in email_index search_gate_data hermes_uv_cache postgres_storage; do
  case "$out" in *"volume-${p}"*) fail "--plan has volume-${p}: $out" ;; esac
done
case "$out" in *dir-MAIL_INDEX_DIR* | *dir-LIGHTRAG_KEY*) fail "--plan has a skipped or non-bind key: $out" ;; esac
# Without compose's volume list nothing is written.
touch "${FAKE}/compose-down"
set +e
out="$(run --plan --json 2> /dev/null)"
rc=$?
set -e
rm "${FAKE}/compose-down"
[ "$rc" = 4 ] || fail "compose config failure: exit $rc ($out)"
case "$out" in *'"error":"command_failed"'*) ;; *) fail "compose config failure: $out" ;; esac
case "$out" in *dir-TWENTY_DATA_DIR*) fail "a DATA_DIR inside data/ got its own part" ;; esac

# A running stack is refused without --stop.
printf 'hermes\npostgres-db\n' > "${FAKE}/running"
set +e
out="$(run --json 2> /dev/null)"
rc=$?
set -e
[ "$rc" = 1 ] || fail "running stack: exit $rc"
case "$out" in *'"error":"stack_running"'*) ;; *) fail "running stack: $out" ;; esac

# --stop stops it, backs up, and starts the same services again.
out="$(run --stop --json)" || fail "create failed: $out"
[ "$(sort "${FAKE}/running" | tr '\n' ' ')" = "hermes postgres-db " ] \
  || fail "services not started again: $(cat "${FAKE}/running")"
case "$out" in *'"phase":"done"'*'"parts":7'*'"skipped":[]'*) ;; *) fail "done line: $out" ;; esac
bdir="$(printf '%s' "$out" | sed -n 's/.*"path":"\([^"]*\)".*/\1/p')"
[ -f "${bdir}/manifest.json" ] || fail "no manifest in ${bdir}"
python3 -c 'import json,sys; m=json.load(open(sys.argv[1])); assert [p["name"] for p in m["parts"]]==["stack","data","data-root","dir-SIGNAL_CLI_DATA_DIR","dir-VIKUNJA_FILES_DIR","volume-n8n_storage","volume-neo4j_data"], m' \
  "${bdir}/manifest.json" || fail "manifest parts"
mode="$(stat -c %a "$bdir" 2> /dev/null || stat -f %Lp "$bdir")"
[ "$mode" = 700 ] || fail "backup dir mode $mode"

data_list="$(listing "${bdir}/data.tar.gz")"
for want in hermes/state.txt db/PG_VERSION; do
  printf '%s\n' "$data_list" | grep -qx "$want" || fail "data lacks ${want}"
done
for unwanted in mail hermes.bak-old worktree-backups coding-roots; do
  printf '%s\n' "$data_list" | grep -q "^${unwanted}" && fail "data has ${unwanted}"
done
listing "${bdir}/data-root.tar.gz" | grep -q '^coding-roots' && fail "data-root has coding-roots/"
stack_list="$(listing "${bdir}/stack.tar.gz")"
printf '%s\n' "$stack_list" | grep -qx .env || fail "stack lacks .env"
printf '%s\n' "$stack_list" | grep -qx docker-compose.override.yml || fail "stack lacks the override"
printf '%s\n' "$stack_list" | grep -q '^data' && fail "stack has data/"
printf '%s\n' "$stack_list" | grep -q '^backups' && fail "stack has backups/"
listing "${bdir}/volume-n8n_storage.tar.gz" | grep -qx db.sqlite || fail "volume part"

# Restore: change everything, then put the backup back.
printf 'state-v2\n' > "${stack}/data/hermes/state.txt"
printf 'new\n' > "${stack}/data/hermes/new.txt"
printf 'memory-v2\n' > "${tmp}/userdata/memory/m.md"
printf 'acct-v2\n' > "${tmp}/signal/account"
printf 'workflow-v2\n' > "${FAKE}/volumes/proj_n8n_storage/db.sqlite"
printf 'hermes\n' > "${FAKE}/running"
set +e
out="$(run restore --from "$bdir" --json 2> /dev/null)"
rc=$?
set -e
[ "$rc" = 1 ] || fail "restore on a running stack: exit $rc ($out)"
out="$(run restore --from "$bdir" --stop --json)" || fail "restore failed: $out"
[ ! -s "${FAKE}/running" ] || fail "restore started services"
[ "$(cat "${stack}/data/hermes/state.txt")" = state-v1 ] || fail "hermes state not restored"
[ ! -e "${stack}/data/hermes/new.txt" ] || fail "restore kept a file newer than the backup"
[ "$(cat "${tmp}/userdata/memory/m.md")" = memory-v1 ] || fail "data root not restored"
[ -f "${tmp}/userdata/coding-roots/r1/f" ] || fail "synced coding roots not moved back"
[ "$(cat "${FAKE}/volumes/proj_n8n_storage/db.sqlite")" = workflow ] || fail "volume not restored"
[ "$(cat "${stack}/data/mail/index.txt")" = index ] || fail "mail index not moved back"
[ -d "${stack}/data/hermes.bak-old" ] || fail "older backup not moved back"
aside="$(find "$stack" -maxdepth 1 -name "data.pre-restore-*" | head -1)"
{ [ -n "$aside" ] && [ "$(cat "${aside}/hermes/state.txt")" = state-v2 ]; } || fail "previous data not kept aside"
ls "${bdir}"/pre-restore-*/volume-n8n_storage.tar.gz > /dev/null 2>&1 || fail "previous volume not saved"
case "$out" in *'"phase":"done"'*'"parts":4'*) ;; *) fail "restore done line: $out" ;; esac
# A part outside the stack is left alone unless asked for.
[ "$(cat "${tmp}/signal/account")" = acct-v2 ] || fail "signal store restored without --with-dirs"
run restore --from "$bdir" --with-dirs --json > /dev/null || fail "restore --with-dirs failed"
[ "$(cat "${tmp}/signal/account")" = acct ] || fail "--with-dirs did not restore the signal store"
# ... and refused once .env names another path for it.
sed "s|^SIGNAL_CLI_DATA_DIR=.*|SIGNAL_CLI_DATA_DIR=${tmp}/elsewhere|" "${stack}/.env" > "${tmp}/env.tmp" && mv "${tmp}/env.tmp" "${stack}/.env"
set +e
out="$(run restore --from "$bdir" --with-dirs --json 2> /dev/null)"
rc=$?
set -e
[ "$rc" = 2 ] || fail "dir part with a changed .env path: exit $rc ($out)"
sed "s|^SIGNAL_CLI_DATA_DIR=.*|SIGNAL_CLI_DATA_DIR=${tmp}/signal|" "${stack}/.env" > "${tmp}/env.tmp" && mv "${tmp}/env.tmp" "${stack}/.env"

# A missing volume is created with compose's labels.
rm -rf "${FAKE}/volumes/proj_n8n_storage"
run restore --from "$bdir" --json > /dev/null || fail "restore into a new volume failed"
[ -f "${FAKE}/volumes/proj_n8n_storage/db.sqlite" ] || fail "new volume not filled"
grep -q 'volume create --label com.docker.compose.project=proj --label com.docker.compose.volume=n8n_storage proj_n8n_storage' "${FAKE}/log" \
  || fail "volume created without compose labels"

# A backup stored inside data/ is refused before anything moves.
inner="$(run --dest data/backups --json | sed -n 's/.*"path":"\([^"]*\)".*/\1/p')"
[ -n "$inner" ] || fail "no backup inside data/"
set +e
out="$(run restore --from "$inner" --json 2> /dev/null)"
rc=$?
set -e
[ "$rc" = 2 ] || fail "backup inside data/: exit $rc ($out)"
[ -f "${stack}/data/hermes/state.txt" ] || fail "refused restore moved data/"
rm -rf "${stack}/data/backups"

# A stack moved after the backup restores into its new place.
mv "$stack" "${tmp}/moved"
stack="${tmp}/moved"
bdir="${stack}/backups/$(basename "$bdir")"
printf 'moved-v2\n' > "${stack}/data/hermes/state.txt"
run restore --from "$bdir" --json > /dev/null || fail "restore into a moved stack failed"
[ "$(cat "${stack}/data/hermes/state.txt")" = state-v1 ] || fail "moved stack's data not restored"
[ ! -d "${tmp}/stack/data" ] || fail "restore recreated the old stack path"

# --skip-volume leaves a named volume out and says so, and restore leaves that
# volume as it is. A name compose doesn't know is refused.
out="$(run --plan --skip-volume neo4j_data --json)"
case "$out" in *volume-neo4j_data*) fail "--plan still has the skipped volume: $out" ;; esac
case "$out" in *'"phase":"skip","volume":"neo4j_data"'*) ;; *) fail "--plan does not show the skip: $out" ;; esac
out="$(run --skip-volume neo4j_data --skip-volume postgres_storage --json)" || fail "create --skip-volume failed: $out"
case "$out" in *'"phase":"done"'*'"skipped":["neo4j_data","postgres_storage"]'*) ;; *) fail "skip done line: $out" ;; esac
sbdir="$(printf '%s' "$out" | sed -n 's/.*"path":"\([^"]*\)".*/\1/p')"
[ ! -e "${sbdir}/volume-neo4j_data.tar.gz" ] || fail "skipped volume was backed up"
[ -f "${sbdir}/volume-n8n_storage.tar.gz" ] || fail "--skip-volume dropped another volume"
python3 -c 'import json,sys; m=json.load(open(sys.argv[1])); assert m["skipped"]==["neo4j_data","postgres_storage"], m' \
  "${sbdir}/manifest.json" || fail "manifest skipped"
printf 'graph-v2\n' > "${FAKE}/volumes/proj_neo4j_data/store"
run restore --from "$sbdir" --json > /dev/null || fail "restore of a backup with a skipped volume failed"
[ "$(cat "${FAKE}/volumes/proj_neo4j_data/store")" = graph-v2 ] || fail "restore touched the skipped volume"
for bad in "--skip-volume nosuch" "--skip-volume"; do
  set +e
  # shellcheck disable=SC2086
  out="$(run --json $bad 2> /dev/null)"
  rc=$?
  set -e
  [ "$rc" = 2 ] || fail "${bad}: exit $rc ($out)"
  case "$out" in *'"error":"bad_input"'*) ;; *) fail "${bad}: $out" ;; esac
done

# Not a backup.
set +e
out="$(run restore --from "$tmp" --json 2> /dev/null)"
rc=$?
set -e
[ "$rc" = 2 ] || fail "bad --from: exit $rc"

echo "ok backup.sh"
