#!/usr/bin/env bash
# scripts/vault.sh with docker (and make) replaced by recording stubs on PATH.
# Covers argument checks, add's files and modes, the scope all refusal,
# passwords kept out of every argv and all output, requests --json, init's
# order and resume from local/state, the make up hook, secrets-sync's exit
# codes, and backup/restore (when the host has sqlite3). Bash 3.2 compatible;
# needs no host python3.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
vault="${ROOT}/scripts/vault.sh"
tmp="$(mktemp -d)"
# Some cases leave folders unreadable on purpose.
trap 'chmod -R u+rwx "$tmp" 2>/dev/null; rm -rf "$tmp"' EXIT

export ENV_FILE="$tmp/.env"
export VAULT_ACCOUNTS_DIR="$tmp/accounts"
export VAULT_HERMES_DATA_DIR="$tmp/hermes-data"
export BITWARDEN_HOST_DATA_DIR="$tmp/data"
export VAULT_BROKER_WAIT=0
export STUB="$tmp/stub"
export STUB_DATA="$BITWARDEN_HOST_DATA_DIR"
mkdir -p "$tmp/bin" "$STUB"
: > "$ENV_FILE"

# docker: one log line per call; canned answers from files in $STUB.
# bitwarden-setup with --entrypoint runs that command here, with its
# /accounts, /local and /secrets paths under $STUB/cview, the container's view.
cat > "$tmp/bin/docker" <<'SH'
#!/bin/sh
printf '%s\n' "$*" >> "$STUB/log"
rc_of() { if [ -f "$STUB/rc.$1" ]; then cat "$STUB/rc.$1"; else echo 0; fi; }
case " $* " in
  *" --entrypoint "*)
    ep=""; take=""; seen=""
    for a do
      shift
      if [ -n "$seen" ]; then
        case "$a" in /accounts/* | /local/* | /secrets/*) a="$STUB/cview$a" ;; esac
        set -- "$@" "$a"
      elif [ "$take" = 1 ]; then ep="$a"; take=""
      elif [ "$a" = --entrypoint ]; then take=1
      elif [ "$a" = bitwarden-setup ]; then seen=1
      fi
    done
    exec "$ep" "$@" ;;
esac
case " $* " in
  *" info "*) exit 0 ;;
  *" ps "*bitwarden-broker*) [ -f "$STUB/broker-running" ] && echo cid-broker; exit 0 ;;
  *" ps "*" hermes "*) [ -f "$STUB/hermes-running" ] && echo cid-hermes; exit 0 ;;
  *" exec -T hermes "*) cat > /dev/null; cat "$STUB/reload-out" 2>/dev/null; exit "$(rc_of reload)" ;;
  *" ps "*) exit 0 ;;
  *" up "*)
    if [ -d "$STUB_DATA/local/tls/server" ] && [ -d "$STUB_DATA/local/vaultwarden" ] \
      && [ -d "$STUB_DATA/secrets/local/tls" ] && [ -d "$STUB_DATA/vaultwarden" ]; then
      echo "stub: dirs ready at up" >> "$STUB/log"
    fi
    exit 0 ;;
  *" broker.cli accounts "*) cat "$STUB/accounts" 2>/dev/null; exit "$(rc_of accounts)" ;;
  *" broker.cli requests --json "*) cat "$STUB/requests.json"; exit 0 ;;
  *" broker.cli requests --tsv "*) cat "$STUB/requests.tsv" 2>/dev/null; exit 0 ;;
  *" broker.cli requests "*) echo "No pending access requests."; exit 0 ;;
  *" broker.cli sync "*) echo "Synced: personal"; exit "$(rc_of sync)" ;;
  *" broker.unlock "*)
    got="$(cat)"
    [ "$got" = "$(cat "$STUB/expect-password" 2>/dev/null)" ] && echo "stub: password on stdin" >> "$STUB/log"
    exit "$(rc_of unlock)" ;;
esac
next=""
for a in "$@"; do
  if [ "$next" = 1 ]; then
    [ -d "$VAULT_ACCOUNTS_DIR" ] && echo "stub: accounts dir at setup-$a" >> "$STUB/log"
    exit "$(rc_of "setup-$a")"
  fi
  [ "$a" = bitwarden-setup ] && next=1
  [ "$a" = bitwarden-secrets-sync ] && exit "$(rc_of secrets-sync)"
done
exit 0
SH
cat > "$tmp/bin/make" <<'SH'
#!/bin/sh
printf 'make %s\n' "$*" >> "$STUB/log"
SH
chmod +x "$tmp/bin/docker" "$tmp/bin/make"
export PATH="$tmp/bin:$PATH"

fail() {
  printf 'FAIL %s\n' "$1" >&2
  [ -f "$out" ] && sed 's/^/  | /' "$out" >&2
  exit 1
}
out="$tmp/out"
run() { "$vault" "$@" < /dev/null > "$out" 2>&1; }
# Runs with stdin from a string (the masked prompts read it).
run_in() { local input="$1"; shift; printf '%s' "$input" | "$vault" "$@" > "$out" 2>&1; }
reset_stub() { rm -f "$STUB"/* 2>/dev/null || true; }
mode_of() { stat -f %Lp "$1" 2>/dev/null || stat -c %a "$1"; }
logged() { grep -qF -- "$1" "$STUB/log" 2>/dev/null; }
said() { grep -qF -- "$1" "$out"; }
PW="syn-master-$$-Zq9"
SECRET="syn-secret-$$-Kx4"
no_secret() {
  if grep -qF -e "$PW" -e "$SECRET" "$out" "$STUB/log" 2>/dev/null; then fail "$1: a password or secret reached argv or output"; fi
}

# --- Argument checks -------------------------------------------------------
run frobnicate && fail "unknown subcommand must fail"
said "Usage: scripts/vault.sh" || fail "unknown subcommand prints usage"
run add --mode cloud && fail "add without --slug"
said "--slug must use lowercase letters, digits and hyphens" || fail "add without --slug: message"
run add --slug Bad_1 --mode cloud && fail "bad slug accepted"
run add --slug personal && fail "add without --mode"
said "--mode must be cloud or selfhosted" || fail "missing --mode: message"
run add --slug personal --mode local && fail "add --mode local accepted"
said "make vault-init" || fail "add --mode local points at make vault-init"
run add --slug local --mode cloud && fail "add --slug local accepted"
run add --slug personal --mode cloud --region mars && fail "bad region accepted"
said "--region must be us or eu" || fail "bad region: message"
run add --slug home --mode selfhosted && fail "selfhosted without --url"
said "--url must be an https:// URL" || fail "selfhosted without --url: message"
run add --slug home --mode selfhosted --url http://vault.example.com && fail "http URL accepted"
run add --slug home --mode selfhosted --url 'https://vault.example.com/?x=1' && fail "URL with a query accepted"
run add --slug personal --mode cloud --scope bogus && fail "bad scope accepted"
said "--scope must be folder:<name>, collection:<id> or all" || fail "bad scope: message"
run add --slug personal --mode cloud --scope 'collection:-x' && fail "collection id starting with - accepted"
run add --slug personal --mode cloud --unlock auto && fail "bad unlock accepted"
said "--unlock must be stored or manual" || fail "bad unlock: message"
run add --slug personal --mode cloud --frob && fail "unknown add option accepted"
run add --mode cloud --slug && fail "--slug with no value accepted"
said "--slug needs a value" || fail "an option without its value: message"
run unlock && fail "unlock without --slug"
run lock --slug Nope && fail "lock with a bad slug"
run remove --slug local && fail "remove --slug local accepted"
run restore && fail "restore without a directory"
run backup && fail "backup without a directory"
[ ! -e "$VAULT_ACCOUNTS_DIR/personal.env" ] || fail "a refused add wrote a file"
[ ! -s "$STUB/log" ] || fail "argument checks ran docker: $(cat "$STUB/log")"

# --- add: files, modes, no password in argv --------------------------------
reset_stub
run_in "user.syn-client"$'\n'"$SECRET"$'\n'"$PW"$'\n' add --slug personal --mode cloud --region eu \
  || fail "add cloud eu"
no_secret add
f="$VAULT_ACCOUNTS_DIR/personal.env"
[ -f "$f" ] || fail "add writes personal.env"
[ "$(mode_of "$f")" = 600 ] || fail "account file is mode 600"
grep -qx 'BW_MODE=cloud' "$f" || fail "BW_MODE"
grep -qx 'BW_SERVER=https://vault.bitwarden.eu' "$f" || fail "REGION=eu picks vault.bitwarden.eu"
grep -qx 'BW_SCOPE=folder:Agent' "$f" || fail "default scope folder:Agent"
grep -qx 'BW_UNLOCK=stored' "$f" || fail "default unlock stored"
grep -qx 'BW_CLIENTID=user.syn-client' "$f" || fail "client id"
if grep -qF -e "$SECRET" -e "$PW" "$f"; then fail "a credential went into the account file"; fi
s="$BITWARDEN_HOST_DATA_DIR/secrets/personal"
[ "$(cat "$s/client_secret")" = "$SECRET" ] || fail "client_secret content"
[ "$(cat "$s/master_password")" = "$PW" ] || fail "master_password content"
[ "$(mode_of "$s/client_secret")" = 600 ] || fail "client_secret mode"
[ "$(mode_of "$s/master_password")" = 600 ] || fail "master_password mode"
[ "$(mode_of "$s")" = 700 ] || fail "secrets/<slug> is mode 700"
[ "$(mode_of "$BITWARDEN_HOST_DATA_DIR/secrets")" = 711 ] || fail "secrets/ is mode 711"
said "opens the whole vault" || fail "stored with a folder: scope prints the Unlock warning"
grep -qx 'BITWARDEN_ENABLED=1' "$ENV_FILE" || fail "add turns on BITWARDEN_ENABLED"
said "bitwarden-setup" || fail "add says how to create and seed the folder"

run_in "user.x"$'\n'"x"$'\n'"x"$'\n' add --slug personal --mode cloud && fail "add must not overwrite"
said "--force" || fail "the overwrite refusal mentions --force"

# manual unlock: no master_password; selfhosted URL kept without its slash.
run_in "user.home"$'\n'"$SECRET"$'\n' add --slug home --mode selfhosted --url https://vault.example.com/ \
  --scope collection:c-1a2b --unlock manual || fail "add selfhosted manual"
no_secret "add manual"
grep -qx 'BW_SERVER=https://vault.example.com' "$VAULT_ACCOUNTS_DIR/home.env" || fail "selfhosted URL"
grep -qx 'BW_SCOPE=collection:c-1a2b' "$VAULT_ACCOUNTS_DIR/home.env" || fail "collection scope"
grep -qx 'BW_UNLOCK=manual' "$VAULT_ACCOUNTS_DIR/home.env" || fail "manual unlock"
[ ! -e "$BITWARDEN_HOST_DATA_DIR/secrets/home/master_password" ] || fail "manual unlock stores no master password"
said "opens the whole vault" && fail "the warning is for stored folder: scopes only"

# A folder name with " #" is quoted, so the broker's parser keeps it whole.
run_in "user.y"$'\n'"$SECRET"$'\n' add --slug hashy --mode cloud --scope 'folder:Agent #2' --unlock manual \
  || fail "add with # in the folder name"
grep -qx 'BW_SCOPE="folder:Agent #2"' "$VAULT_ACCOUNTS_DIR/hashy.env" || fail "folder name with # quoted"

# --scope all: refused unless CONFIRM_SCOPE_ALL=1, even with SCOPE=all exported
# (make vault-add SCOPE=all exports it, so it can't be the confirmation).
run_in "user.z"$'\n'"$SECRET"$'\n' add --slug wide --mode cloud --scope all --unlock manual \
  && fail "--scope all accepted without CONFIRM_SCOPE_ALL"
said "CONFIRM_SCOPE_ALL=1" || fail "the all refusal names CONFIRM_SCOPE_ALL=1"
[ ! -e "$VAULT_ACCOUNTS_DIR/wide.env" ] || fail "a refused --scope all wrote a file"
printf 'user.z\n%s\n' "$SECRET" | SCOPE=all "$vault" add --slug wide --mode cloud --scope all --unlock manual \
  > "$out" 2>&1 && fail "--scope all accepted with SCOPE=all alone"
grep -q "CONFIRM_SCOPE_ALL=1" "$out" || fail "SCOPE=all alone is refused naming CONFIRM_SCOPE_ALL=1"
[ ! -e "$VAULT_ACCOUNTS_DIR/wide.env" ] || fail "SCOPE=all alone wrote a file"
printf 'user.z\n%s\n' "$SECRET" | SCOPE=all CONFIRM_SCOPE_ALL=1 "$vault" add --slug wide --mode cloud --scope all \
  --unlock manual > "$out" 2>&1 || fail "--scope all with CONFIRM_SCOPE_ALL=1"
grep -qx 'BW_SCOPE=all' "$VAULT_ACCOUNTS_DIR/wide.env" || fail "scope all written"
rm -f "$VAULT_ACCOUNTS_DIR/wide.env" "$VAULT_ACCOUNTS_DIR/hashy.env"

# With the broker running, add restarts it and reads the account's state;
# an auth error removes what add wrote.
reset_stub
touch "$STUB/broker-running"
printf 'checked\tcloud\tfolder\tunlocked\t-\nhome\tselfhosted\tcollection\tlocked\t-\n' > "$STUB/accounts"
run_in "user.c"$'\n'"$SECRET"$'\n'"$PW"$'\n' add --slug checked --mode cloud || fail "add with the broker up"
logged "--profile bitwarden up -d --no-deps --force-recreate bitwarden-broker" || fail "add recreates the broker"
logged "restart bitwarden-broker" && fail "add must not docker compose restart the broker (it fails after a hermes recreate)"
said "checked: signed in" || fail "add reports the broker's unlock"
said "home is locked after the restart: make vault-unlock SLUG=home" || fail "add lists manual accounts to unlock again"
no_secret "add checked"
printf 'refused\tcloud\tfolder\terror\tauth\n' > "$STUB/accounts"
run_in "user.r"$'\n'"$SECRET"$'\n'"$PW"$'\n' add --slug refused --mode cloud && fail "an auth error must fail add"
said "auth" || fail "the auth failure names the error class"
[ ! -e "$VAULT_ACCOUNTS_DIR/refused.env" ] || fail "an auth error keeps the account file"
[ ! -e "$BITWARDEN_HOST_DATA_DIR/secrets/refused" ] || fail "an auth error keeps the secrets"

# --force keeps the old file and secrets as .bak until the check passes, and
# puts them back on an auth error.
printf 'checked\tcloud\tfolder\terror\tauth\n' > "$STUB/accounts"
cp "$VAULT_ACCOUNTS_DIR/checked.env" "$tmp/checked.before"
run_in "user.new"$'\n'"new-$SECRET"$'\n'"new-$PW"$'\n' add --slug checked --mode cloud --force \
  && fail "add --force with an auth error must fail"
cmp -s "$VAULT_ACCOUNTS_DIR/checked.env" "$tmp/checked.before" || fail "auth error after --force: old account file not restored"
cs="$BITWARDEN_HOST_DATA_DIR/secrets/checked"
[ "$(cat "$cs/client_secret")" = "$SECRET" ] || fail "auth error after --force: old client_secret not restored"
[ "$(cat "$cs/master_password")" = "$PW" ] || fail "auth error after --force: old master_password not restored"
[ "$(mode_of "$cs/client_secret")" = 600 ] || fail "restored client_secret mode"
{ [ ! -e "$VAULT_ACCOUNTS_DIR/checked.env.bak" ] && [ ! -e "$cs/client_secret.bak" ]; } || fail "auth error after --force left .bak files"
said "previous account file and secrets are back" || fail "the --force auth error says what was restored"
printf 'checked\tcloud\tfolder\tunlocked\t-\n' > "$STUB/accounts"
run_in "user.new"$'\n'"new-$SECRET"$'\n' add --slug checked --mode cloud --unlock manual --force \
  || fail "add --force that passes the check"
grep -qx 'BW_CLIENTID=user.new' "$VAULT_ACCOUNTS_DIR/checked.env" || fail "--force writes the new file"
[ ! -e "$cs/master_password" ] || fail "--force to manual removes the stored master password"
{ [ ! -e "$VAULT_ACCOUNTS_DIR/checked.env.bak" ] && [ ! -e "$cs/client_secret.bak" ] && [ ! -e "$cs/master_password.bak" ]; } \
  || fail "a passed check leaves .bak files"
rm -f "$VAULT_ACCOUNTS_DIR/checked.env"
rm -rf "$cs"

# A run stopped before the account file is written leaves no new secrets folder or temp file.
rm -f "$STUB/broker-running"
chmod 500 "$VAULT_ACCOUNTS_DIR"
run_in "user.s"$'\n'"$SECRET"$'\n'"$PW"$'\n' add --slug stopped --mode cloud && fail "add into an unwritable accounts folder"
chmod 700 "$VAULT_ACCOUNTS_DIR"
[ ! -e "$BITWARDEN_HOST_DATA_DIR/secrets/stopped" ] || fail "a stopped add left secrets/stopped/"
for left in "$VAULT_ACCOUNTS_DIR"/.stopped.*; do [ ! -e "$left" ] || fail "a stopped add left a temp file"; done

# --- unlock and lock ---------------------------------------------------------
reset_stub
run unlock --slug home && fail "unlock with the broker down must fail"
said "not running" || fail "unlock says the broker is not running"
touch "$STUB/broker-running"
printf '%s' "$PW" > "$STUB/expect-password"
run_in "$PW"$'\n' unlock --slug home || fail "unlock"
logged "exec -T bitwarden-broker python3 -m broker.unlock home" || fail "unlock runs broker.unlock"
logged "stub: password on stdin" || fail "unlock passes the password on stdin"
no_secret unlock
echo 2 > "$STUB/rc.unlock"
run_in "wrong"$'\n' unlock --slug home && fail "a wrong password must fail"
said "Wrong master password" || fail "wrong password message"
run_in "$PW"$'\n' unlock --slug personal && fail "unlock of a stored account"
said "stored" || fail "unlock of a stored account says why"
run unlock --slug missing && fail "unlock of a missing account"
rm -f "$STUB/rc.unlock"
run lock --slug home || fail "lock"
logged "broker.unlock --lock home" || fail "lock runs broker.unlock --lock"

# --- requests, sync and grant -----------------------------------------------
reset_stub
touch "$STUB/broker-running"
printf '{"pending": [{"request_id": "r1", "account": "personal", "site": "www.example.com", "reason": "sign in", "created": 1, "status": "pending", "done_at": null}], "recent": []}\n' \
  > "$STUB/requests.json"
"$vault" requests --json < /dev/null > "$tmp/req.json" 2> "$tmp/req.err" || fail "requests --json"
cmp -s "$tmp/req.json" "$STUB/requests.json" || fail "requests --json adds to the broker's JSON"
if command -v python3 >/dev/null 2>&1; then
  python3 -c 'import json,sys; json.load(open(sys.argv[1]))' "$tmp/req.json" || fail "requests --json is not JSON"
fi
run requests || fail "requests"
run sync || fail "sync"
logged "broker.cli sync" || fail "sync calls the broker"
run grant --site www.example.com --slug personal --email owner@example.com || fail "grant --site"
logged "run --rm bitwarden-setup grant --slug personal --email owner@example.com --site www.example.com" \
  || fail "grant runs bitwarden-setup grant"
[ "$(grep -c 'broker.cli sync' "$STUB/log")" = 2 ] || fail "grant syncs the broker afterwards"
printf 'r1\tpersonal\twww.example.com\tsign in\n' > "$STUB/requests.tsv"
run grant && fail "grant with no --site and no terminal must fail"
said "www.example.com" || fail "grant lists pending requests"
said "--site" || fail "grant without a terminal says to pass --site"
echo 1 > "$STUB/rc.sync"
run grant --site www.example.com --slug personal --email owner@example.com || fail "grant with a failed sync"
said "did not sync" || fail "grant says when the sync failed"
said "The broker synced" && fail "grant claims a sync that failed"
rm -f "$STUB/rc.sync"
echo 1 > "$STUB/rc.setup-grant"
run grant --site www.example.com --slug personal --email owner@example.com && fail "a failed grant must fail"
said "stays pending" || fail "a failed grant says the request stays pending"

# --- remove ------------------------------------------------------------------
reset_stub
run remove --slug home || fail "remove"
[ ! -e "$VAULT_ACCOUNTS_DIR/home.env" ] || fail "remove deletes the account file"
[ ! -e "$BITWARDEN_HOST_DATA_DIR/secrets/home" ] || fail "remove deletes the secrets"
run remove --slug home && fail "removing a missing account must fail"

# --- init: order, resume from local/state ------------------------------------
reset_stub
run init && fail "init without an owner and no terminal"
said "VAULT_OWNER_EMAIL" || fail "init without an owner names VAULT_OWNER_EMAIL"
reset_stub
VAULT_ACCOUNTS_DIR="$tmp/accounts-init" run init --owner owner@example.com || fail "init from scratch"
logged "stub: accounts dir at setup-tls" || fail "init creates the accounts folder before its first container"
[ "$(mode_of "$tmp/accounts-init")" = 711 ] || fail "init's accounts folder is mode 711"
for d in bw broker secrets/local/tls local/tls/server local/vaultwarden vaultwarden; do
  [ -d "$BITWARDEN_HOST_DATA_DIR/$d" ] || fail "init creates $d"
done
[ "$(mode_of "$BITWARDEN_HOST_DATA_DIR/local")" = 700 ] || fail "local/ is mode 700"
[ "$(mode_of "$BITWARDEN_HOST_DATA_DIR/secrets")" = 711 ] || fail "secrets/ stays mode 711"
order="$(grep -nE 'bitwarden-setup (tls|admin-token|local-init|role)|up -d|dirs ready' "$STUB/log" \
  | sed -E 's/^[0-9]+://; s/.*bitwarden-setup ([a-z-]+).*/\1/; s/.* up -d.*/up/; s/stub: dirs ready at up/dirs/' | tr '\n' ' ')"
[ "$order" = "tls admin-token up dirs local-init role " ] || fail "init order: ${order}"
logged "local-init --owner owner@example.com --signup-url http://localhost:8222" || fail "local-init arguments"
logged "role --slug local --email owner@example.com" || fail "role arguments"
logged "--profile vault up -d --wait vaultwarden vaultwarden-tls" || fail "init starts the vault profile"
grep -qx 'BITWARDEN_LOCAL_ENABLED=1' "$ENV_FILE" || fail "init sets BITWARDEN_LOCAL_ENABLED=1"
said "nothing else has a copy unless you back it up" || fail "init's closing message says nothing else has a copy without a backup"

# The owner has not registered yet: local-init exits 4, init stops before role.
reset_stub
echo 4 > "$STUB/rc.setup-local-init"
run init --owner owner@example.com && fail "init must fail while the owner has not registered"
logged "bitwarden-setup role" && fail "init ran role before the owner was confirmed"
said "make vault-init again" || fail "init says to re-run"

# Recorded step confirmed: no tls, admin-token or local-init; role with the recorded owner.
reset_stub
printf 'STEP=confirmed\nOWNER_EMAIL=owner@example.com\nORG_ID=org-1\n' > "$BITWARDEN_HOST_DATA_DIR/local/state"
chmod 600 "$BITWARDEN_HOST_DATA_DIR/local/state"
run init || fail "init resume at confirmed"
logged "bitwarden-setup local-init" && fail "init re-ran local-init after confirmed"
logged "bitwarden-setup tls" && fail "init re-ran tls after confirmed"
logged "role --slug local --email owner@example.com" || fail "init resumes with role for the recorded owner"
run init --owner other@example.com && fail "init with a different owner than recorded"
said "owner@example.com" || fail "the owner mismatch names the recorded owner"

# Recorded step role: nothing left to run.
reset_stub
printf 'STEP=role\nOWNER_EMAIL=owner@example.com\n' > "$BITWARDEN_HOST_DATA_DIR/local/state"
run init || fail "init when done"
logged "bitwarden-setup" && fail "init ran bitwarden-setup when every step was done"
said "already set up" || fail "init says local mode is set up"

# --- make up hook --------------------------------------------------------------
reset_stub
printf 'BW_MODE=local\nBW_SERVER=https://vaultwarden.vault.internal\nBW_SCOPE=collection:c-agent\nBW_UNLOCK=stored\nBW_CLIENTID=user.l\nBW_SECRETS_ITEM_SCOPE=collection:c-stack\n' \
  > "$VAULT_ACCOUNTS_DIR/local.env"
chmod 600 "$VAULT_ACCOUNTS_DIR/local.env"
printf 'BITWARDEN_ENABLED=0\n' > "$ENV_FILE"
run up-hook || fail "up-hook with BITWARDEN_ENABLED=0"
[ ! -s "$out" ] || fail "up-hook prints nothing while BITWARDEN_ENABLED=0"
[ ! -s "$STUB/log" ] || fail "up-hook runs docker while BITWARDEN_ENABLED=0"
printf 'BITWARDEN_ENABLED=1\n' > "$ENV_FILE"
touch "$STUB/broker-running"
printf 'local\tlocal\tcollection\tlocked\t-\npersonal\tcloud\tfolder\tunlocked\t-\n' > "$STUB/accounts"
run up-hook || fail "up-hook, locked"
said "Bitwarden secrets sync skipped: local is locked" || fail "up-hook skip message"
logged "bitwarden-secrets-sync" && fail "up-hook synced while the account was locked"
printf 'local\tlocal\tcollection\tunlocked\t-\n' > "$STUB/accounts"
run up-hook || fail "up-hook, unlocked"
logged "run --rm -T bitwarden-secrets-sync" || fail "up-hook runs secrets-sync when unlocked"
grep -A5 -E '^up:' "${ROOT}/Makefile" | grep -q 'vault.sh up-hook' || fail "make up calls vault.sh up-hook"
grep -A5 -E '^up:' "${ROOT}/Makefile" | awk '/vault.sh pre-up/ { p = NR } /docker compose up -d/ { u = NR } END { exit !(p && u && p < u) }' \
  || fail "make up runs vault.sh pre-up before docker compose up"

# --- reload-plugins: the running gateway after a bootstrap changed a profile ---
mkdir -p "$VAULT_HERMES_DATA_DIR/profiles/browser" "$VAULT_HERMES_DATA_DIR/profiles/api-server"
reset_stub
printf 'BITWARDEN_ENABLED=0\n' > "$ENV_FILE"
run reload-plugins || fail "reload-plugins with no marker"
{ [ ! -s "$out" ] && [ ! -s "$STUB/log" ]; } || fail "reload-plugins runs no docker and prints nothing with no marker"
touch "$VAULT_HERMES_DATA_DIR/profiles/browser/.addon-plugins-changed"
run reload-plugins || fail "reload-plugins with hermes stopped"
logged "exec" && fail "reload-plugins execs into a stopped hermes"
[ ! -s "$out" ] || fail "reload-plugins is silent with hermes stopped"
touch "$STUB/hermes-running"
printf 'ok\tbrowser\n' > "$STUB/reload-out"
run up-hook || fail "up-hook with a marker and the flag off"
logged "exec -T hermes /opt/hermes/.venv/bin/python - .addon-plugins-changed" \
  || fail "up-hook reloads plugins while BITWARDEN_ENABLED=0 (turning the add-on off)"
said "Reloaded plugins for the browser profile" || fail "reload-plugins reports the reloaded profile"
printf 'ok\tdefault\nfail\tbrowser\thome is not served by this gateway\n' > "$STUB/reload-out"
run reload-plugins || fail "reload-plugins with a refusal"
said "Reloaded plugins for the default profile" || fail "reload-plugins reports each profile"
said "The gateway did not reload plugins for browser (home is not served by this gateway); make restart" \
  || fail "reload-plugins names the refusal and the fallback"
echo 3 > "$STUB/rc.reload"
run reload-plugins || fail "reload-plugins must not fail make up when the exec fails"
said "Could not ask the gateway to reload its plugins; make restart" || fail "reload-plugins exec failure message"
rm -f "$VAULT_HERMES_DATA_DIR/profiles/browser/.addon-plugins-changed"

# --- pre-up: the accounts folder before docker compose up ----------------------
reset_stub
printf 'BITWARDEN_ENABLED=0\n' > "$ENV_FILE"
VAULT_ACCOUNTS_DIR="$tmp/accounts-preup" run pre-up || fail "pre-up while off"
[ ! -e "$tmp/accounts-preup" ] || fail "pre-up creates nothing while BITWARDEN_ENABLED=0"
[ ! -s "$out" ] || fail "pre-up prints nothing while off"
printf 'BITWARDEN_ENABLED=1\n' > "$ENV_FILE"
VAULT_ACCOUNTS_DIR="$tmp/accounts-preup" run pre-up || fail "pre-up while on"
[ "$(mode_of "$tmp/accounts-preup")" = 711 ] || fail "pre-up creates the accounts folder, mode 711"
[ ! -s "$STUB/log" ] || fail "pre-up runs no docker"

# --- follow-hermes: after hermes is recreated or restarted ---------------------
reset_stub
printf 'BITWARDEN_ENABLED=0\n' > "$ENV_FILE"
touch "$STUB/broker-running"
run follow-hermes || fail "follow-hermes while off"
[ ! -s "$out" ] || fail "follow-hermes prints nothing while BITWARDEN_ENABLED=0"
[ ! -s "$STUB/log" ] || fail "follow-hermes runs docker while BITWARDEN_ENABLED=0"
(. "${ROOT}/scripts/lib/bitwarden.sh"; follow_hermes) < /dev/null > "$out" 2>&1 || fail "follow_hermes while off"
{ [ ! -s "$out" ] && [ ! -s "$STUB/log" ]; } || fail "the follow_hermes helper is a no-op while BITWARDEN_ENABLED=0"
printf 'BITWARDEN_ENABLED=1\n' > "$ENV_FILE"
rm -f "$STUB/broker-running"
run follow-hermes || fail "follow-hermes with no broker container"
logged " up " && fail "follow-hermes starts a broker that was never created"
reset_stub
touch "$STUB/broker-running"
printf 'BW_MODE=cloud\nBW_UNLOCK=manual\n' > "$VAULT_ACCOUNTS_DIR/handson.env"
printf 'handson\tcloud\tfolder\tlocked\t-\nlocal\tlocal\tcollection\tunlocked\t-\n' > "$STUB/accounts"
(. "${ROOT}/scripts/lib/bitwarden.sh"; follow_hermes) < /dev/null > "$out" 2>&1 || fail "follow_hermes while on"
logged "--profile bitwarden up -d --no-deps bitwarden-broker" || fail "follow_hermes brings the broker up in hermes' namespace"
logged "force-recreate" && fail "follow_hermes leaves a broker in the current namespace alone (no --force-recreate)"
said "handson is locked after the restart: make vault-unlock SLUG=handson" || fail "follow_hermes lists manual accounts to unlock"
rm -f "$VAULT_ACCOUNTS_DIR/handson.env"

# Every command that recreates or restarts hermes follows it with the broker.
grep -A4 -E '^hermes-reload:' "${ROOT}/Makefile" | grep -q 'vault.sh follow-hermes' || fail "make hermes-reload follows hermes"
grep -A3 -E '^hermes-recreate:' "${ROOT}/Makefile" | grep -q 'vault.sh follow-hermes' || fail "make hermes-recreate follows hermes"
grep -A3 -E '^restart:' "${ROOT}/Makefile" | grep -q 'vault.sh follow-hermes' || fail "make restart follows hermes"
for f in corpus.sh model-use.sh; do
  grep -q 'follow_hermes' "${ROOT}/scripts/$f" || fail "scripts/$f calls follow_hermes after it recreates or restarts hermes"
done
for f in agent-create.sh bootstrap-buzz.sh agent-attach-buzz.sh; do
  grep -q 'make hermes-recreate' "${ROOT}/scripts/$f" || fail "scripts/$f tells you to run make hermes-recreate"
done
# No script recreates hermes, or tells you to, without following it.
bare="$(grep -lE 'force-recreate[^|;&]* hermes( |$|")' "${ROOT}"/scripts/*.sh 2>/dev/null | grep -v '/test-' || true)"
for f in $bare; do
  grep -q 'follow_hermes' "$f" || fail "$(basename "$f") recreates hermes without follow_hermes"
done

# --- secrets-sync exit codes ---------------------------------------------------
reset_stub
echo 3 > "$STUB/rc.secrets-sync"
run secrets-sync || fail "secrets-sync exit 3"
logged "make" || fail "secrets-sync exit 3 runs make hermes-reload"
grep -q 'hermes-reload' "$STUB/log" || fail "secrets-sync exit 3 runs hermes-reload"
echo 1 > "$STUB/rc.secrets-sync"
run secrets-sync && fail "secrets-sync exit 1 must fail"
said "make up" || fail "secrets-sync exit 1 says to start the broker"

# --- Linux: files that belong to the containers' user (Ruling R23) -------------
# A non-root user on Linux after the uid 10001 chown: the account file, local/
# and secrets/<slug>/ are out of reach, so vault.sh goes through bitwarden-setup
# (the stub runs it against $STUB/cview) and says so, never reading them as empty.
reset_stub
# shellcheck disable=SC2016  # expanded by the stub
printf '#!/bin/sh\nif [ "$1" = -s ]; then echo Linux; else exec /usr/bin/uname "$@"; fi\n' > "$tmp/bin/uname"
# shellcheck disable=SC2016  # expanded by the stub
printf '#!/bin/sh\ncase "$1" in -u | -g) echo 1000 ;; *) exec /usr/bin/id "$@" ;; esac\n' > "$tmp/bin/id"
chmod +x "$tmp/bin/uname" "$tmp/bin/id"
cv="$STUB/cview"
mkdir -p "$cv/accounts" "$cv/local" "$cv/secrets/lin2"
lin="$VAULT_ACCOUNTS_DIR/lin.env"
printf 'BW_MODE=cloud\nBW_SERVER=https://vault.bitwarden.com\nBW_UNLOCK=manual\nBW_CLIENTID=user.lin\nBW_SECRETS_ITEM_SCOPE=folder:Stack\n' > "$lin"
cp "$lin" "$cv/accounts/lin.env"
chmod 000 "$lin"
touch "$STUB/broker-running"
printf '%s' "$PW" > "$STUB/expect-password"
run_in "$PW"$'\n' unlock --slug lin || fail "linux: unlock of an unreadable manual account"
logged "--entrypoint sh bitwarden-setup -c" || fail "linux: unlock did not read the account file through bitwarden-setup"
logged "stub: password on stdin" || fail "linux: unlock did not pass the password"
said "belongs to the containers' user" || fail "linux: no note about the container path"
no_secret "linux unlock"

printf 'lin\tcloud\tfolder\tunlocked\t-\nlocal\tlocal\tcollection\tlocked\t-\n' > "$STUB/accounts"
run up-hook || fail "linux: up-hook"
logged "/accounts/lin.env" || fail "linux: up-hook did not read lin.env through bitwarden-setup"
logged "run --rm -T bitwarden-secrets-sync" || fail "linux: up-hook skipped the sync for an unreadable account file"
said "skipped: local is locked" || fail "linux: up-hook lost the readable account"

: > "$STUB/log"
printf 'STEP=confirmed\nOWNER_EMAIL=owner@example.com\n' > "$cv/local/state"
chmod 000 "$BITWARDEN_HOST_DATA_DIR/local"
run init || fail "linux: init re-run with local/ out of reach"
logged "/local/state" || fail "linux: init did not read local/state through bitwarden-setup"
logged "role --slug local --email owner@example.com" || fail "linux: init did not resume from the container's state"
logged "bitwarden-setup tls" && fail "linux: init re-ran tls after confirmed"
chmod 700 "$BITWARDEN_HOST_DATA_DIR/local"

: > "$STUB/log"
mkdir -p "$BITWARDEN_HOST_DATA_DIR/secrets/lin2"
chmod 500 "$BITWARDEN_HOST_DATA_DIR/secrets/lin2"
printf 'lin2\tcloud\tfolder\tunlocked\t-\n' > "$STUB/accounts"
run_in "user.lin2"$'\n'"$SECRET"$'\n'"$PW"$'\n' add --slug lin2 --mode cloud || fail "linux: add into a container-owned secrets folder"
[ "$(cat "$cv/secrets/lin2/client_secret")" = "$SECRET" ] || fail "linux: client_secret not written through bitwarden-setup"
[ "$(cat "$cv/secrets/lin2/master_password")" = "$PW" ] || fail "linux: master_password not written through bitwarden-setup"
[ "$(mode_of "$cv/secrets/lin2/client_secret")" = 600 ] || fail "linux: client_secret mode"
[ -z "$(ls -A "$BITWARDEN_HOST_DATA_DIR/secrets/lin2")" ] || fail "linux: add wrote into a folder it does not own"
said "sudo chown -R 10001:10001" || fail "linux: add does not say how to hand the files to uid 10001"
said "lin2: signed in" || fail "linux: add's check"
no_secret "linux add"

chmod 700 "$BITWARDEN_HOST_DATA_DIR/secrets/lin2"
chmod 600 "$lin"
rm -rf "$BITWARDEN_HOST_DATA_DIR/secrets/lin2"
rm -f "$lin" "$VAULT_ACCOUNTS_DIR/lin2.env" "$tmp/bin/uname" "$tmp/bin/id"

# --- status --json ---------------------------------------------------------------
reset_stub
mv "$VAULT_ACCOUNTS_DIR" "$tmp/accounts-kept" 2>/dev/null || true
mkdir -p "$VAULT_ACCOUNTS_DIR"
st_file() { printf '# t\nBW_MODE=%s\nBW_SERVER=https://example.com\nBW_SCOPE=%s\nBW_UNLOCK=%s\nBW_CLIENTID=syn-id\n' "$2" "$3" "$4" > "$VAULT_ACCOUNTS_DIR/$1.env"; }
js() { python3 -c 'import json,sys
for l in sys.stdin: json.loads(l)' < "$out"; }
jfield() { sed -n "${1}p" "$out" | python3 -c "import json,sys; v=json.load(sys.stdin).get('$2','MISSING'); print('null' if v is None else v)"; }
run status && fail "status without --json is a usage error"
touch "$STUB/broker-running"
run status --json || fail "status --json with no accounts"
[ ! -s "$out" ] || fail "no accounts gives no output"
st_file work cloud folder:Work stored
st_file home selfhosted collection:abc manual
printf 'BW_MODE=local\nBW_UNLOCK=stored\nBW_CLIENTID=x\n' > "$VAULT_ACCOUNTS_DIR/local.env"
st_file bad cloud all manual
printf 'work\tcloud\tfolder\tunlocked\t-\t1\t\nhome\tselfhosted\tcollection\tlocked\t-\t0\t\nlocal\tlocal\tcollection\tunlocked\t-\t1\t\nbad\tcloud\tfolder\terror\tauth\t0\t12\nrefused\tBad_File.env: line 3: expected KEY=value\n' > "$STUB/accounts"
printf 'BW_MODE=\n' > "$VAULT_ACCOUNTS_DIR/Bad_File.env"
run status --json || fail "status --json"
js || fail "status lines parse as JSON"
[ "$(wc -l < "$out" | tr -d ' ')" = 5 ] || fail "one line per account file"
[ "$(jfield 1 slug)" = null ] && [ "$(jfield 1 file)" = Bad_File.env ] || fail "refused file with a non-slug stem"
[ "$(jfield 1 state)" = refused ] && [ "$(jfield 1 error)" = bad_file ] || fail "refused file state"
[ "$(jfield 2 slug)" = bad ] && [ "$(jfield 2 error)" = login_refused ] && [ "$(jfield 2 retry_after)" = 12 ] || fail "auth maps to login_refused with retry_after"
[ "$(jfield 3 slug)" = home ] && [ "$(jfield 3 unlock)" = manual ] && [ "$(jfield 3 state)" = locked ] && [ "$(jfield 3 error)" = null ] || fail "manual locked account"
[ "$(jfield 3 scope)" = collection:abc ] && [ "$(jfield 3 mode)" = selfhosted ] || fail "scope and mode come from the file"
[ "$(jfield 4 slug)" = local ] && [ "$(jfield 4 mode)" = local ] || fail "local mode reported"
[ "$(jfield 5 slug)" = work ] && [ "$(jfield 5 state)" = unlocked ] && [ "$(jfield 5 unlock)" = stored ] && [ "$(jfield 5 retry_after)" = null ] || fail "stored account unlocked"
grep -q 'syn-id' "$out" && fail "status prints no client id"
rm -f "$VAULT_ACCOUNTS_DIR/Bad_File.env"
for e in network:server_unreachable rate_limited:rate_limited unknown:unknown locked:null; do
  printf 'bad\tcloud\tfolder\terror\t%s\t0\t\n' "${e%%:*}" > "$STUB/accounts"
  run status --json || fail "status --json $e"
  [ "$(jfield 1 error)" = "${e#*:}" ] || fail "error_class ${e%%:*} mapping"
done
st_file fresh cloud folder:Work stored
printf 'work\tcloud\tfolder\tunlocked\t-\t1\t\n' > "$STUB/accounts"
chmod 000 "$VAULT_ACCOUNTS_DIR/home.env"
run status --json || fail "status --json with an unreadable file and an unloaded file"
grep '^{' "$out" | python3 -c 'import json,sys
for l in sys.stdin: json.loads(l)' || fail "unreadable-file lines parse as JSON"
[ "$(grep -c '^{' "$out")" = 5 ] || fail "one line per file despite an unreadable one"
grep -q '"slug":"fresh".*"state":"offline","error":"not_loaded","retry_after":null' "$out" || fail "a file the running broker has not loaded is not_loaded"
grep -q '"file":"home.env","slug":"home","state":"refused","error":"bad_file"' "$out" || fail "an unreadable file is reported as bad_file"
chmod 600 "$VAULT_ACCOUNTS_DIR/home.env"
rm -f "$VAULT_ACCOUNTS_DIR/fresh.env"
rm -f "$STUB/broker-running"
run status --json || fail "status --json with the broker down exits 0"
js || fail "broker-down lines parse as JSON"
[ "$(wc -l < "$out" | tr -d ' ')" = 4 ] || fail "broker down: one line per file"
[ "$(jfield 1 state)" = offline ] && [ "$(jfield 1 error)" = broker_down ] && [ "$(jfield 1 retry_after)" = null ] || fail "broker down fields"
rm -rf "$VAULT_ACCOUNTS_DIR"
[ ! -d "$tmp/accounts-kept" ] || mv "$tmp/accounts-kept" "$VAULT_ACCOUNTS_DIR"

# --- backup and restore --------------------------------------------------------
if command -v sqlite3 >/dev/null 2>&1; then
  reset_stub
  vw="$BITWARDEN_HOST_DATA_DIR/vaultwarden"
  mkdir -p "$vw/attachments/a1"
  sqlite3 "$vw/db.sqlite3" 'CREATE TABLE t (v TEXT); INSERT INTO t VALUES ("syn-row");'
  printf 'k' > "$vw/rsa_key.pem"; chmod 600 "$vw/rsa_key.pem"
  printf 'att' > "$vw/attachments/a1/file"
  run backup "$tmp/bk" || fail "backup"
  [ "$(sqlite3 "$tmp/bk/vaultwarden/db.sqlite3" 'SELECT v FROM t')" = syn-row ] || fail "backup copies the database"
  [ "$(mode_of "$tmp/bk/vaultwarden/rsa_key.pem")" = 600 ] || fail "backup keeps rsa_key modes"
  [ -f "$tmp/bk/vaultwarden/attachments/a1/file" ] || fail "backup copies attachments"
  [ "$(mode_of "$tmp/bk/secrets/personal/master_password")" = 600 ] || fail "backup keeps secret modes"
  [ -f "$tmp/bk/local/state" ] || fail "backup copies local/"
  [ -f "$tmp/bk/accounts/local.env" ] || fail "backup copies the account files"
  run backup "$tmp/bk" && fail "backup into a non-empty directory"
  run restore "$tmp/bk" && fail "restore over existing data without --force"
  said "--force" || fail "the restore refusal mentions --force"
  sqlite3 "$vw/db.sqlite3" 'DELETE FROM t'
  rm -f "$BITWARDEN_HOST_DATA_DIR/secrets/personal/master_password"
  run restore "$tmp/bk" --force || fail "restore --force"
  [ "$(sqlite3 "$vw/db.sqlite3" 'SELECT v FROM t')" = syn-row ] || fail "restore puts the database back"
  [ "$(mode_of "$BITWARDEN_HOST_DATA_DIR/secrets/personal/master_password")" = 600 ] || fail "restore keeps modes"
else
  echo "skip: backup and restore (no sqlite3)"
fi

printf 'OK vault.sh\n'
