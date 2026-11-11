#!/usr/bin/env bash
# shellcheck disable=SC2015  # "A && B || fail" is the check: fail runs when either test is false
# scripts/vault.sh with docker (and make) replaced by recording stubs on PATH.
# Covers argument checks, add's files and modes, the scope all refusal,
# passwords kept out of every argv and all output, requests --json, init's
# order and resume from local/state, the make up hook, the removed
# secrets-sync command, and backup/restore (when the host has sqlite3). Bash 3.2
# compatible. vault.sh runs with STACK_PYTHON=host, so the instance file writes
# (scripts/lib/addon_store.py) and the JSON checks use host python3.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
vault="${ROOT}/scripts/vault.sh"
tmp="$(mktemp -d)"
# Some cases leave folders unreadable on purpose.
trap 'chmod -R u+rwx "$tmp" 2>/dev/null; rm -rf "$tmp"' EXIT

export ENV_FILE="$tmp/.env"
# The add-on store: <data root>/addons/bitwarden/instances.
export ASSISTANT_DATA_ROOT="$tmp/root"
export INST="$ASSISTANT_DATA_ROOT/addons/bitwarden/instances"
export STACK_PYTHON=host
export VAULT_HERMES_DATA_DIR="$tmp/hermes-data"
export BITWARDEN_HOST_DATA_DIR="$tmp/data"
export VAULT_BROKER_WAIT=0
export STUB="$tmp/stub"
export STUB_DATA="$BITWARDEN_HOST_DATA_DIR"
mkdir -p "$tmp/bin" "$STUB"
: > "$ENV_FILE"

# docker: one log line per call; canned answers from files in $STUB.
# bitwarden-setup with --entrypoint runs that command here, with its
# /broker, /local and /secrets paths under $STUB/cview, the container's view.
cat > "$tmp/bin/docker" <<'SH'
#!/bin/sh
printf '%s\n' "$*" >> "$STUB/log"
rc_of() { if [ -f "$STUB/rc.$1" ]; then cat "$STUB/rc.$1"; else echo 0; fi; }
# bw/ may be mode 000 (Linux: it belongs to uid 10001); the broker's side of
# the stub opens it for one step and puts the mode back.
bw_open() { bwm=""; [ -d "$STUB_DATA/bw" ] || return 0; bwm="$(stat -c %a "$STUB_DATA/bw" 2>/dev/null || stat -f %Lp "$STUB_DATA/bw")"; chmod u+rwx "$STUB_DATA/bw"; }
bw_close() { [ -z "$bwm" ] || chmod "$bwm" "$STUB_DATA/bw"; }
case " $* " in
  *" --entrypoint "*)
    ep=""; take=""; seen=""
    for a do
      shift
      if [ -n "$seen" ]; then
        case "$a" in /broker/* | /local/* | /secrets/*) a="$STUB/cview$a" ;; esac
        set -- "$@" "$a"
      elif [ "$take" = 1 ]; then ep="$a"; take=""
      elif [ "$a" = --entrypoint ]; then take=1
      elif [ "$a" = bitwarden-setup ]; then seen=1
      fi
    done
    exec "$ep" "$@" ;;
esac
case " $* " in
  *" exec -T bitwarden-broker sh -c "*)
    for a do last="$a"; done
    [ -f "$STUB/rc.bwrm" ] && exit "$(cat "$STUB/rc.bwrm")"
    bw_open
    rm -rf "$STUB_DATA/bw/${last#/data/bw/}"; [ ! -e "$STUB_DATA/bw/${last#/data/bw/}" ]; r=$?
    bw_close
    exit "$r" ;;
  *" info "*) exit 0 ;;
  *" ps "*bitwarden-broker*) [ -f "$STUB/broker-running" ] && echo cid-broker; exit 0 ;;
  *" ps "*" hermes "*) [ -f "$STUB/hermes-running" ] && echo cid-hermes; exit 0 ;;
  *" exec -T hermes "*) cat > /dev/null; cat "$STUB/reload-out" 2>/dev/null; exit "$(rc_of reload)" ;;
  *" ps "*) exit 0 ;;
  *" up "*)
    for f in "$ASSISTANT_DATA_ROOT"/addons/bitwarden/instances/*.json; do
      [ -e "$f" ] && echo "stub: instance ${f##*/} at up" >> "$STUB/log"
    done
    bw_open
    [ -e "$STUB_DATA/bw/jm/stale" ] && echo "stub: stale bw login at up" >> "$STUB/log"
    # The broker writing a sign-in into bw/<slug>/ while it is replaced.
    if [ -f "$STUB/bw-at-up" ]; then mkdir -p "$STUB_DATA/bw/$(cat "$STUB/bw-at-up")" && touch "$STUB_DATA/bw/$(cat "$STUB/bw-at-up")/login"; fi
    bw_close
    if [ -d "$STUB_DATA/local/tls/server" ] && [ -d "$STUB_DATA/local/vaultwarden" ] \
      && [ -d "$STUB_DATA/secrets/local/tls" ] && [ -d "$STUB_DATA/vaultwarden" ]; then
      echo "stub: dirs ready at up" >> "$STUB/log"
    fi
    exit "$(rc_of up)" ;;
  *" broker.cli accounts "*)
    [ -f "$STUB/record-ps" ] && { ps -ax -o args= >> "$STUB/ps" 2>/dev/null || true; }
    cat "$STUB/accounts" 2>/dev/null; exit "$(rc_of accounts)" ;;
  *" broker.cli requests --json "*) cat "$STUB/requests.json"; exit 0 ;;
  *" broker.cli requests "*) echo "No pending access requests."; exit 0 ;;
  *" broker.cli sync --json "*) cat "$STUB/sync.json"; exit "$(rc_of sync)" ;;
  *" broker.cli sync "*) echo "Synced: personal"; exit "$(rc_of sync)" ;;
  *" broker.unlock "*)
    got="$(cat)"
    ps -ax -o args= >> "$STUB/ps" 2>/dev/null || true
    [ "$got" = "$(cat "$STUB/expect-password" 2>/dev/null)" ] && echo "stub: password on stdin" >> "$STUB/log"
    exit "$(rc_of unlock)" ;;
esac
next=""
for a in "$@"; do
  if [ "$next" = 1 ]; then
    [ -d "$ASSISTANT_DATA_ROOT/addons/bitwarden/instances" ] && echo "stub: store dir at setup-$a" >> "$STUB/log"
    exit "$(rc_of "setup-$a")"
  fi
  [ "$a" = bitwarden-setup ] && next=1
done
exit 0
SH
cat > "$tmp/bin/make" <<'SH'
#!/bin/sh
printf 'make %s\n' "$*" >> "$STUB/log"
SH
# A python3 that fails, for a store write that does not happen.
mkdir -p "$tmp/bin-nopy"
printf '#!/bin/sh\nexit 1\n' > "$tmp/bin-nopy/python3"
chmod +x "$tmp/bin/docker" "$tmp/bin/make" "$tmp/bin-nopy/python3"
export PATH="$tmp/bin:$PATH"

# jget FILE KEY: a top-level value of a JSON file, empty when missing.
jget() { python3 -c 'import json,sys; v=json.load(open(sys.argv[1])).get(sys.argv[2]); print("" if v is None else v)' "$1" "$2"; }
# inst SLUG JSON: an instance file as addon_store writes it (indent 2).
inst() { mkdir -p "$INST"; python3 -c 'import json,sys; print(json.dumps(json.loads(sys.argv[1]), indent=2))' "$2" > "$INST/$1.json"; }

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
mode_of() { stat -c %a "$1" 2>/dev/null || stat -f %Lp "$1"; }
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
said "--slug must start with a lowercase letter" || fail "add without --slug: message"
run add --slug Bad_1 --mode cloud && fail "bad slug accepted"
run add --slug 9abc --mode cloud && fail "a slug starting with a digit accepted"
run add --slug abcdefghijabcdefghijabcdefghijkl --mode cloud && fail "a 32-character slug accepted"
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
[ ! -e "$INST/personal.json" ] || fail "a refused add wrote a file"
[ ! -s "$STUB/log" ] || fail "argument checks ran docker: $(cat "$STUB/log")"

# --- add: files, modes, no password in argv --------------------------------
reset_stub
run_in "user.syn-client"$'\n'"$SECRET"$'\n'"$PW"$'\n' add --slug personal --mode cloud --region eu \
  || fail "add cloud eu"
no_secret add
f="$INST/personal.json"
[ -f "$f" ] || fail "add writes instances/personal.json"
[ "$(mode_of "$f")" = 644 ] || fail "the instance file has addon_store's mode 644"
[ "$(mode_of "$INST")" = 700 ] || fail "instances/ is mode 700"
[ "$(jget "$f" mode)" = cloud ] || fail "mode"
[ "$(jget "$f" region)" = eu ] || fail "REGION=eu is region eu"
[ "$(jget "$f" scope)" = folder:Agent ] || fail "default scope folder:Agent"
[ "$(jget "$f" unlock)" = stored ] || fail "default unlock stored"
if grep -qF -e "$SECRET" -e "$PW" -e user.syn-client "$f"; then fail "a credential went into the instance file"; fi
s="$BITWARDEN_HOST_DATA_DIR/secrets/personal"
[ "$(cat "$s/client_id")" = user.syn-client ] || fail "client_id content"
[ "$(mode_of "$s/client_id")" = 600 ] || fail "client_id mode"
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
[ "$(jget "$INST/home.json" url)" = https://vault.example.com ] || fail "selfhosted URL"
[ "$(jget "$INST/home.json" region)" = "" ] || fail "a selfhosted instance has no region"
[ "$(jget "$INST/home.json" scope)" = collection:c-1a2b ] || fail "collection scope"
[ "$(jget "$INST/home.json" unlock)" = manual ] || fail "manual unlock"
[ ! -e "$BITWARDEN_HOST_DATA_DIR/secrets/home/master_password" ] || fail "manual unlock stores no master password"
said "opens the whole vault" && fail "the warning is for stored folder: scopes only"

# A folder name with " #" stays whole in the JSON.
run_in "user.y"$'\n'"$SECRET"$'\n' add --slug hashy --mode cloud --scope 'folder:Agent #2' --unlock manual \
  || fail "add with # in the folder name"
[ "$(jget "$INST/hashy.json" scope)" = 'folder:Agent #2' ] || fail "folder name with # kept whole"

# --scope all: refused unless CONFIRM_SCOPE_ALL=1, even with SCOPE=all exported
# (make vault-add SCOPE=all exports it, so it can't be the confirmation).
run_in "user.z"$'\n'"$SECRET"$'\n' add --slug wide --mode cloud --scope all --unlock manual \
  && fail "--scope all accepted without CONFIRM_SCOPE_ALL"
said "CONFIRM_SCOPE_ALL=1" || fail "the all refusal names CONFIRM_SCOPE_ALL=1"
[ ! -e "$INST/wide.json" ] || fail "a refused --scope all wrote a file"
printf 'user.z\n%s\n' "$SECRET" | SCOPE=all "$vault" add --slug wide --mode cloud --scope all --unlock manual \
  > "$out" 2>&1 && fail "--scope all accepted with SCOPE=all alone"
grep -q "CONFIRM_SCOPE_ALL=1" "$out" || fail "SCOPE=all alone is refused naming CONFIRM_SCOPE_ALL=1"
[ ! -e "$INST/wide.json" ] || fail "SCOPE=all alone wrote a file"
printf 'user.z\n%s\n' "$SECRET" | SCOPE=all CONFIRM_SCOPE_ALL=1 "$vault" add --slug wide --mode cloud --scope all \
  --unlock manual > "$out" 2>&1 || fail "--scope all with CONFIRM_SCOPE_ALL=1"
[ "$(jget "$INST/wide.json" scope)" = all ] || fail "scope all written"
rm -f "$INST/wide.json" "$INST/hashy.json"
grep -q 'ADDON_SH' "$vault" && fail "vault.sh still calls addon.sh"

# With the broker running, add restarts it and reads the account's state;
# an auth error removes what add wrote.
reset_stub
touch "$STUB/broker-running"
printf 'checked\tcloud\tfolder\tunlocked\t-\nhome\tselfhosted\tcollection\tlocked\t-\n' > "$STUB/accounts"
run_in "user.c"$'\n'"$SECRET"$'\n'"$PW"$'\n' add --slug checked --mode cloud || fail "add with the broker up"
logged "--profile bitwarden up -d --no-deps --force-recreate bitwarden-broker" || fail "add recreates the broker"
logged "stub: instance checked.json at up" || fail "add writes the instance file before the broker restart"
logged "restart bitwarden-broker" && fail "add must not docker compose restart the broker (it fails after a hermes recreate)"
said "checked: signed in" || fail "add reports the broker's unlock"
said "home is locked after the restart: make vault-unlock SLUG=home" || fail "add lists manual accounts to unlock again"
no_secret "add checked"
printf 'refused\tcloud\tfolder\terror\tauth\n' > "$STUB/accounts"
run_in "user.r"$'\n'"$SECRET"$'\n'"$PW"$'\n' add --slug refused --mode cloud && fail "an auth error must fail add"
said "auth" || fail "the auth failure names the error class"
[ ! -e "$INST/refused.json" ] || fail "an auth error keeps the instance file"
[ ! -e "$BITWARDEN_HOST_DATA_DIR/secrets/refused" ] || fail "an auth error keeps the secrets"

# --force keeps the old file and secrets as .bak until the check passes, and
# puts them back on an auth error.
printf 'checked\tcloud\tfolder\terror\tauth\n' > "$STUB/accounts"
cp "$INST/checked.json" "$tmp/checked.before"
run_in "user.new"$'\n'"new-$SECRET"$'\n'"new-$PW"$'\n' add --slug checked --mode cloud --force \
  && fail "add --force with an auth error must fail"
cmp -s "$INST/checked.json" "$tmp/checked.before" || fail "auth error after --force: old instance file not restored"
cs="$BITWARDEN_HOST_DATA_DIR/secrets/checked"
[ "$(cat "$cs/client_id")" = user.c ] || fail "auth error after --force: old client_id not restored"
[ "$(cat "$cs/client_secret")" = "$SECRET" ] || fail "auth error after --force: old client_secret not restored"
[ "$(cat "$cs/master_password")" = "$PW" ] || fail "auth error after --force: old master_password not restored"
[ "$(mode_of "$cs/client_secret")" = 600 ] || fail "restored client_secret mode"
{ [ ! -e "$INST/.checked.json.bak" ] && [ ! -e "$cs/client_secret.bak" ] && [ ! -e "$cs/client_id.bak" ]; } \
  || fail "auth error after --force left .bak files"
said "previous instance file and secrets are back" || fail "the --force auth error says what was restored"
printf 'checked\tcloud\tfolder\tunlocked\t-\n' > "$STUB/accounts"
run_in "user.new"$'\n'"new-$SECRET"$'\n' add --slug checked --mode cloud --unlock manual --force \
  || fail "add --force that passes the check"
[ "$(cat "$cs/client_id")" = user.new ] || fail "--force writes the new client_id"
[ "$(jget "$INST/checked.json" unlock)" = manual ] || fail "--force writes the new instance file"
[ ! -e "$cs/master_password" ] || fail "--force to manual removes the stored master password"
{ [ ! -e "$INST/.checked.json.bak" ] && [ ! -e "$cs/client_secret.bak" ] && [ ! -e "$cs/master_password.bak" ]; } \
  || fail "a passed check leaves .bak files"
rm -f "$INST/checked.json"
rm -rf "$cs"

# The instance file write fails (no working python3): no new secrets folder, instance file or temp file.
rm -f "$STUB/broker-running"
printf 'user.s\n%s\n%s\n' "$SECRET" "$PW" | PATH="$tmp/bin-nopy:$PATH" "$vault" add --slug stopped --mode cloud > "$out" 2>&1 \
  && fail "add with a failing instance file write"
[ ! -e "$BITWARDEN_HOST_DATA_DIR/secrets/stopped" ] || fail "a stopped add left secrets/stopped/"
[ ! -e "$INST/stopped.json" ] || fail "a stopped add left its instance file"
for left in "$INST"/.stopped.*; do [ ! -e "$left" ] || fail "a stopped add left a temp file"; done

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

# --- requests, sync; grant is gone -----------------------------------------------
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
python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); assert set(d)=={"pending","recent"}, sorted(d)' "$tmp/req.json" \
  || fail "requests --json has keys other than pending and recent"
run grant --site www.example.com --slug personal --email owner@example.com && fail "grant must be an unknown command"
logged "bitwarden-setup grant" && fail "grant must not reach bitwarden-setup"

# --- remove ------------------------------------------------------------------
reset_stub
run remove --slug home || fail "remove"
[ ! -e "$INST/home.json" ] || fail "remove deletes the instance file"
[ ! -e "$BITWARDEN_HOST_DATA_DIR/secrets/home" ] || fail "remove deletes the secrets"
run remove --slug home && fail "removing a missing account must fail"

# --- init: order, resume from local/state ------------------------------------
reset_stub
run init && fail "init without an owner and no terminal"
said "VAULT_OWNER_EMAIL" || fail "init without an owner names VAULT_OWNER_EMAIL"
reset_stub
ASSISTANT_DATA_ROOT="$tmp/root-init" run init --owner owner@example.com || fail "init from scratch"
logged "stub: store dir at setup-tls" || fail "init creates the store's instances/ before its first container"
[ "$(mode_of "$tmp/root-init/addons/bitwarden/instances")" = 700 ] || fail "init's instances/ is mode 700"
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

# Recorded step role, local account written: nothing left to run.
reset_stub
printf 'STEP=role\nOWNER_EMAIL=owner@example.com\n' > "$BITWARDEN_HOST_DATA_DIR/local/state"
printf '{\n  "scope": "collection:c-agent"\n}\n' > "$BITWARDEN_HOST_DATA_DIR/broker/local-account.json"
printf 'user.l\n' > "$BITWARDEN_HOST_DATA_DIR/secrets/local/client_id"
run init || fail "init when done"
logged "bitwarden-setup" && fail "init ran bitwarden-setup when every step was done"
said "already set up" || fail "init says local mode is set up"
# Set up before the local account left local.env: local-init writes it, nothing else runs.
rm -f "$BITWARDEN_HOST_DATA_DIR/broker/local-account.json" "$BITWARDEN_HOST_DATA_DIR/secrets/local/client_id"
run init || fail "init when done, without the local account"
logged "local-init --owner owner@example.com" || fail "init does not write a missing local account"
logged "bitwarden-setup role" && fail "init re-ran role to write the local account"
logged "bitwarden-setup tls" && fail "init re-ran tls to write the local account"

# --- make up hook --------------------------------------------------------------
# With no profile marked it does nothing: no store writes, no docker, no output.
reset_stub
printf 'BITWARDEN_ENABLED=0\n' > "$ENV_FILE"
run up-hook || fail "up-hook with BITWARDEN_ENABLED=0"
[ ! -s "$out" ] || fail "up-hook prints nothing while BITWARDEN_ENABLED=0"
[ ! -s "$STUB/log" ] || fail "up-hook runs docker while BITWARDEN_ENABLED=0"
printf 'BITWARDEN_ENABLED=1\n' > "$ENV_FILE"
touch "$STUB/broker-running"
printf 'local\tlocal\tcollection\tunlocked\t-\npersonal\tcloud\tfolder\tunlocked\t-\n' > "$STUB/accounts"
find "$INST" | sort > "$tmp/inst.before"
run up-hook || fail "up-hook, enabled"
{ [ ! -s "$out" ] && [ ! -s "$STUB/log" ]; } || fail "up-hook prints or runs something with no profile marked"
find "$INST" | sort | cmp -s - "$tmp/inst.before" || fail "up-hook changed the instance store"
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

# --- pre-up: the store's instances/ before docker compose up --------------------
reset_stub
printf 'BITWARDEN_ENABLED=0\n' > "$ENV_FILE"
ASSISTANT_DATA_ROOT="$tmp/root-preup" run pre-up || fail "pre-up while off"
[ ! -e "$tmp/root-preup" ] || fail "pre-up creates nothing while BITWARDEN_ENABLED=0"
[ ! -s "$out" ] || fail "pre-up prints nothing while off"
printf 'BITWARDEN_ENABLED=1\n' > "$ENV_FILE"
ASSISTANT_DATA_ROOT="$tmp/root-preup" run pre-up || fail "pre-up while on"
[ "$(mode_of "$tmp/root-preup/addons/bitwarden/instances")" = 700 ] || fail "pre-up creates instances/, mode 700"
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
inst handson '{"mode":"cloud","unlock":"manual"}'
printf 'handson\tcloud\tfolder\tlocked\t-\nlocal\tlocal\tcollection\tunlocked\t-\n' > "$STUB/accounts"
(. "${ROOT}/scripts/lib/bitwarden.sh"; follow_hermes) < /dev/null > "$out" 2>&1 || fail "follow_hermes while on"
logged "--profile bitwarden up -d --no-deps bitwarden-broker" || fail "follow_hermes brings the broker up in hermes' namespace"
logged "force-recreate" && fail "follow_hermes leaves a broker in the current namespace alone (no --force-recreate)"
said "handson is locked after the restart: make vault-unlock SLUG=handson" || fail "follow_hermes lists manual accounts to unlock"
rm -f "$INST/handson.json"

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

# --- secrets-sync is gone --------------------------------------------------------
reset_stub
run secrets-sync && fail "secrets-sync must fail as an unknown command"
logged "docker" && fail "secrets-sync ran docker"
grep -q 'secrets-sync' "${ROOT}/Makefile" && fail "the Makefile still has a secrets-sync target"

# --- status --json: an unknown key in an instance file is not a refusal --------------
reset_stub
inst old '{"mode":"cloud","unlock":"manual","secrets_item_scope":"folder:Stack"}'
touch "$STUB/broker-running"
printf 'old\tcloud\tfolder\tlocked\t-\n' > "$STUB/accounts"
run status --json || fail "status --json with an unknown key"
said '"slug":"old"' || fail "status --json does not list the account with the unknown key"
said refused && fail "status --json reports the unknown key as refused"
rm -f "$INST/old.json"

# --- Linux: files that belong to the containers' user (Ruling R23) -------------
# A non-root user on Linux after the uid 10001 chown: broker/, local/ and
# secrets/<slug>/ are out of reach, so vault.sh goes through bitwarden-setup
# (the stub runs it against $STUB/cview) and says so, never reading them as empty.
# The instance store stays yours.
reset_stub
# shellcheck disable=SC2016  # expanded by the stub
printf '#!/bin/sh\nif [ "$1" = -s ]; then echo Linux; else exec /usr/bin/uname "$@"; fi\n' > "$tmp/bin/uname"
# shellcheck disable=SC2016  # expanded by the stub
printf '#!/bin/sh\ncase "$1" in -u | -g) echo 1000 ;; *) exec /usr/bin/id "$@" ;; esac\n' > "$tmp/bin/id"
chmod +x "$tmp/bin/uname" "$tmp/bin/id"
cv="$STUB/cview"
mkdir -p "$cv/broker" "$cv/local" "$cv/secrets/lin2"
touch "$STUB/broker-running"
# status --json reads broker/local-account.json through bitwarden-setup.
printf '{\n  "scope": "collection:c-lin"\n}\n' > "$cv/broker/local-account.json"
rm -f "$BITWARDEN_HOST_DATA_DIR/broker/local-account.json"
chmod 000 "$BITWARDEN_HOST_DATA_DIR/broker"
printf 'local\tlocal\tcollection\tunlocked\t-\t1\t\n' > "$STUB/accounts"
run status --json || fail "linux: status --json with broker/ out of reach"
grep -q '"slug":"local","mode":"local","scope":"collection:c-lin","unlock":"stored","state":"unlocked"' "$out" \
  || fail "linux: status did not read local-account.json through bitwarden-setup"
logged "/broker/local-account.json" || fail "linux: status did not go through bitwarden-setup"
chmod 700 "$BITWARDEN_HOST_DATA_DIR/broker"

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
[ "$(cat "$cv/secrets/lin2/client_id")" = user.lin2 ] || fail "linux: client_id not written through bitwarden-setup"
[ "$(cat "$cv/secrets/lin2/master_password")" = "$PW" ] || fail "linux: master_password not written through bitwarden-setup"
[ "$(mode_of "$cv/secrets/lin2/client_secret")" = 600 ] || fail "linux: client_secret mode"
[ -z "$(ls -A "$BITWARDEN_HOST_DATA_DIR/secrets/lin2")" ] || fail "linux: add wrote into a folder it does not own"
# Every folder existed and the instance file stays yours, so there is nothing to hand to uid 10001.
said "sudo chown" && fail "linux: add asks to chown when it made nothing new"
said "lin2: signed in" || fail "linux: add's check"
no_secret "linux add"

chmod 700 "$BITWARDEN_HOST_DATA_DIR/secrets/lin2"
rm -rf "$BITWARDEN_HOST_DATA_DIR/secrets/lin2"
rm -f "$INST/lin2.json" "$tmp/bin/uname" "$tmp/bin/id"

# --- status --json ---------------------------------------------------------------
reset_stub
D="$BITWARDEN_HOST_DATA_DIR"
mv "$INST" "$tmp/inst-kept" 2>/dev/null || true
mkdir -p "$INST"
rm -f "$D/broker/local-account.json"
st_inst() { inst "$1" "{\"mode\":\"$2\",\"scope\":\"$3\",\"unlock\":\"$4\"}"; }
js() { python3 -c 'import json,sys
for l in sys.stdin: json.loads(l)' < "$out"; }
jfield() { sed -n "${1}p" "$out" | python3 -c "import json,sys; v=json.load(sys.stdin).get('$2','MISSING'); print('null' if v is None else v)"; }
run status && fail "status without --json is a usage error"
touch "$STUB/broker-running"
run status --json || fail "status --json with no accounts"
[ ! -s "$out" ] || fail "no accounts gives no output"
st_inst work cloud folder:Work stored
st_inst home selfhosted collection:abc manual
printf '{\n  "scope": "collection:c-agent"\n}\n' > "$D/broker/local-account.json"
st_inst bad cloud all manual
st_inst broken cloud folder:Agent manual
printf 'BW_MODE=cloud\n' > "$INST/stray.env"
printf 'work\tcloud\tfolder\tunlocked\t-\t1\t\nhome\tselfhosted\tcollection\tlocked\t-\t0\t\nlocal\tlocal\tcollection\tunlocked\t-\t1\t\nbad\tcloud\tfolder\terror\tauth\t0\t12\nrefused\tbroken.json: client_id: secrets/broken/client_id is missing; connect the account again\tmissing_client_id\n' > "$STUB/accounts"
st_inst Bad_File cloud folder:Agent manual
run status --json || fail "status --json"
js || fail "status lines parse as JSON"
[ "$(wc -l < "$out" | tr -d ' ')" = 6 ] || fail "one line per account, the .env file left out"
[ "$(jfield 1 slug)" = null ] && [ "$(jfield 1 file)" = Bad_File.json ] || fail "a file name outside the slug rule"
[ "$(jfield 1 state)" = refused ] && [ "$(jfield 1 error)" = bad_slug ] || fail "a bad slug is refused as bad_slug"
[ "$(jfield 2 slug)" = bad ] && [ "$(jfield 2 error)" = login_refused ] && [ "$(jfield 2 retry_after)" = 12 ] || fail "auth maps to login_refused with retry_after"
[ "$(jfield 3 slug)" = broken ] && [ "$(jfield 3 file)" = broken.json ] && [ "$(jfield 3 state)" = refused ] \
  && [ "$(jfield 3 error)" = missing_client_id ] || fail "the broker's refusal cause is passed on"
[ "$(jfield 4 slug)" = home ] && [ "$(jfield 4 unlock)" = manual ] && [ "$(jfield 4 state)" = locked ] && [ "$(jfield 4 error)" = null ] || fail "manual locked account"
[ "$(jfield 4 scope)" = collection:abc ] && [ "$(jfield 4 mode)" = selfhosted ] || fail "scope and mode come from the instance file"
[ "$(jfield 5 slug)" = local ] && [ "$(jfield 5 mode)" = local ] && [ "$(jfield 5 scope)" = collection:c-agent ] \
  && [ "$(jfield 5 unlock)" = stored ] || fail "the local account from broker/local-account.json"
[ "$(jfield 6 slug)" = work ] && [ "$(jfield 6 state)" = unlocked ] && [ "$(jfield 6 unlock)" = stored ] && [ "$(jfield 6 retry_after)" = null ] || fail "stored account unlocked"
rm -f "$INST/Bad_File.json" "$INST/broken.json" "$INST/stray.env"
for e in network:server_unreachable rate_limited:rate_limited unknown:unknown locked:null; do
  printf 'bad\tcloud\tfolder\terror\t%s\t0\t\n' "${e%%:*}" > "$STUB/accounts"
  run status --json || fail "status --json $e"
  [ "$(jfield 1 error)" = "${e#*:}" ] || fail "error_class ${e%%:*} mapping"
done
# A scope with quotes and non-ASCII stays valid JSON; an instance named local is refused beside the real one;
# an instance without unlock is manual.
inst esc '{"mode":"cloud","scope":"folder:Team \"2\" é"}'
st_inst local cloud folder:Agent stored
printf 'esc\tcloud\tfolder\tlocked\t-\t1\t\nlocal\tlocal\tcollection\tunlocked\t-\t1\t\n' > "$STUB/accounts"
run status --json || fail "status --json with an escaped scope"
js || fail "an escaped scope breaks the JSON"
grep '"slug":"esc"' "$out" | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d["scope"]=="folder:Team \"2\" é" and d["unlock"]=="manual", d' \
  || fail "the escaped scope or the default unlock"
grep -q '"file":"local.json","slug":null,"state":"refused","error":"bad_slug"' "$out" || fail "an instance named local is refused"
grep -q '"slug":"local","mode":"local"' "$out" || fail "the local account is listed beside a refused local.json"
rm -f "$INST/esc.json" "$INST/local.json"
st_inst fresh cloud folder:Work stored
printf 'work\tcloud\tfolder\tunlocked\t-\t1\t\n' > "$STUB/accounts"
chmod 000 "$INST/home.json"
run status --json || fail "status --json with an unreadable file and an unloaded file"
js || fail "unreadable-file lines parse as JSON"
[ "$(grep -c '^{' "$out")" = 5 ] || fail "one line per account despite an unreadable one"
grep -q '"slug":"fresh".*"state":"offline","error":"not_loaded","retry_after":null' "$out" || fail "an instance the running broker has not loaded is not_loaded"
grep -q '"file":"home.json","slug":"home","state":"refused","error":"bad_file"' "$out" || fail "an unreadable file is reported as bad_file"
chmod 644 "$INST/home.json"
rm -f "$INST/fresh.json"
rm -f "$STUB/broker-running"
run status --json || fail "status --json with the broker down exits 0"
js || fail "broker-down lines parse as JSON"
[ "$(wc -l < "$out" | tr -d ' ')" = 4 ] || fail "broker down: one line per account"
[ "$(jfield 1 state)" = offline ] && [ "$(jfield 1 error)" = broker_down ] && [ "$(jfield 1 retry_after)" = null ] || fail "broker down fields"
rm -rf "$INST"
rm -f "$D/broker/local-account.json"
[ ! -d "$tmp/inst-kept" ] || mv "$tmp/inst-kept" "$INST"

# --- unlock, lock and sync --json ------------------------------------------------
reset_stub
mv "$INST" "$tmp/inst-kept2" 2>/dev/null || true
mkdir -p "$INST"
st_agent_file() { inst "$1" "{\"mode\":\"cloud\",\"scope\":\"folder:Agent\",\"unlock\":\"$2\"}"; }
jj() { STACK_PYTHON=host "$vault" "$@" > "$out" 2> "$tmp/jerr"; }
jerrcode() { local rc=0; printf '%s' "$1" | jj "${@:2}" || rc=$?; echo "$rc"; }
jline() { python3 -c "import json,sys; d=json.loads(sys.stdin.read()); print(d.get('$1'))" < "$out"; }
st_agent_file man manual
st_agent_file sto stored
printf '%s' "$PW" > "$STUB/expect-password"
payload="{\"master_password\":\"$PW\"}"
[ "$(jerrcode "$payload" unlock --slug man --json)" = 4 ] || fail "unlock --json with the broker down exits 4"
[ "$(jline error)" = broker_down ] || fail "unlock --json broker_down"
touch "$STUB/broker-running"
[ "$(jerrcode "$payload" unlock --slug man --json)" = 0 ] || fail "unlock --json"
[ "$(wc -l < "$out" | tr -d ' ')" = 1 ] && [ "$(jline phase)" = "done" ] || fail "unlock --json ends with done"
logged "stub: password on stdin" || fail "unlock --json passes the password on stdin"
grep -qF "broker.unlock man" "$STUB/ps" || fail "the ps record missed the unlock exec"
grep -qF "$PW" "$STUB/ps" && fail "the password reached an argv"
no_secret "unlock --json"
grep -qF "$PW" "$tmp/jerr" && fail "unlock --json leaked the password to stderr"
echo 2 > "$STUB/rc.unlock"
[ "$(jerrcode "$payload" unlock --slug man --json)" = 4 ] && [ "$(jline error)" = login_refused ] || fail "unlock --json wrong password"
echo 3 > "$STUB/rc.unlock"
[ "$(jerrcode "$payload" unlock --slug man --json)" = 2 ] && [ "$(jline error)" = not_found ] || fail "unlock --json exit 3 is not_found"
echo 1 > "$STUB/rc.unlock"
[ "$(jerrcode "$payload" unlock --slug man --json)" = 4 ] && [ "$(jline error)" = command_failed ] || fail "unlock --json other exit is command_failed"
rm -f "$STUB/rc.unlock"
[ "$(jerrcode "$payload" unlock --slug sto --json)" = 2 ] && [ "$(jline error)" = bad_input ] || fail "unlock --json stored account"
[ "$(jerrcode "$payload" unlock --slug nope --json)" = 2 ] && [ "$(jline error)" = not_found ] || fail "unlock --json missing file"
[ "$(jerrcode "$payload" unlock --slug local --json)" = 2 ] && [ "$(jline error)" = bad_input ] || fail "unlock --json local"
[ "$(jerrcode "$payload" unlock --slug 9man --json)" = 2 ] && [ "$(jline error)" = bad_slug ] || fail "unlock --json bad slug"
[ "$(jerrcode "not json" unlock --slug man --json)" = 2 ] && [ "$(jline error)" = bad_input ] || fail "unlock --json bad JSON"
[ "$(jerrcode '{}' unlock --slug man --json)" = 2 ] && [ "$(jline error)" = bad_input ] || fail "unlock --json without a password"
[ "$(jerrcode "" lock --slug man --json)" = 0 ] && [ "$(jline phase)" = "done" ] && [ "$(wc -l < "$out" | tr -d ' ')" = 1 ] || fail "lock --json"
logged "broker.unlock --lock man" || fail "lock --json runs broker.unlock --lock"
[ "$(jerrcode "" lock --slug sto --json)" = 2 ] && [ "$(jline error)" = bad_input ] || fail "lock --json stored account"
echo 3 > "$STUB/rc.unlock"
[ "$(jerrcode "" lock --slug man --json)" = 2 ] && [ "$(jline error)" = not_found ] || fail "lock --json exit 3"
rm -f "$STUB/rc.unlock"
for body in '{"synced":["a"],"busy":[]}' '{"synced":[],"reason":"busy"}' '{"synced":["a"],"reason":"recent"}' '{"synced":[],"busy":["b"]}'; do
  printf '%s\n' "$body" > "$STUB/sync.json"
  rc=0; STACK_PYTHON=host "$vault" sync --json < /dev/null > "$out" 2> "$tmp/jerr" || rc=$?
  [ "$rc" = 0 ] && [ "$(cat "$out")" = "$body" ] || fail "sync --json passes $body through"
done
rm -f "$STUB/broker-running"
rc=0; STACK_PYTHON=host "$vault" sync --json < /dev/null > "$out" 2> "$tmp/jerr" || rc=$?
[ "$rc" = 1 ] && [ ! -s "$out" ] && grep -q "not running" "$tmp/jerr" || fail "sync --json with the broker down"
rm -rf "$INST"
[ ! -d "$tmp/inst-kept2" ] || mv "$tmp/inst-kept2" "$INST"

# --- add and remove --json: the connect and disconnect hooks ----------------------
reset_stub
mv "$INST" "$tmp/inst-kept3" 2>/dev/null || true
mkdir -p "$INST"
D="$BITWARDEN_HOST_DATA_DIR"
# ja PAYLOAD SLUG [ARGS...]: add --json with PAYLOAD on stdin; prints the exit code.
ja() { local rc=0; printf '%s' "$1" | STACK_PYTHON=host "$vault" add --slug "$2" --json "${@:3}" > "$out" 2> "$tmp/jerr" || rc=$?; echo "$rc"; }
jr() { local rc=0; STACK_PYTHON=host "$vault" remove "$@" < /dev/null > "$out" 2> "$tmp/jerr" || rc=$?; echo "$rc"; }
jlast() { tail -1 "$out" | python3 -c "import json,sys; v=json.load(sys.stdin).get('$1'); print('null' if v is None else v)"; }
only_json() {
  python3 -c 'import json,sys
for l in open(sys.argv[1]):
    d = json.loads(l)
    assert isinstance(d, dict) and "phase" in d, l' "$out" || fail "$1: stdout holds something other than JSON lines"
}
gone() {
  [ ! -e "$INST/$1.json" ] || fail "$2: the instance file stayed"
  [ ! -e "$D/secrets/$1" ] || fail "$2: secrets/$1 stayed"
  [ ! -e "$D/bw/$1" ] || fail "$2: bw/$1 stayed"
  for left in "$INST"/."$1".*; do [ ! -e "$left" ] || fail "$2: a temp file stayed"; done
}
no_secret_json() {
  no_secret "$1"
  if grep -qF -e "$PW" -e "$SECRET" -e "user.syn-$$" "$tmp/jerr"; then fail "$1: a secret reached stderr"; fi
}
recreates() { grep -c -- "--force-recreate bitwarden-broker" "$STUB/log" 2>/dev/null || true; }
MANUAL="{\"mode\":\"cloud\",\"client_id\":\"user.syn-$$\",\"client_secret\":\"$SECRET\"}"
STORED="{\"mode\":\"cloud\",\"unlock\":\"stored\",\"client_id\":\"user.syn-$$\",\"client_secret\":\"$SECRET\",\"master_password\":\"$PW\"}"
printf 'BITWARDEN_ENABLED=0\n' > "$ENV_FILE"
cp "$ENV_FILE" "$tmp/env.before"

# Broker down: nothing written, .env unchanged.
[ "$(ja "$MANUAL" jm)" = 4 ] && [ "$(jlast error)" = broker_down ] || fail "add --json with the broker down"
gone jm "broker down"
cmp -s "$ENV_FILE" "$tmp/env.before" || fail "add --json wrote .env"
only_json "broker down"

# Refusals before anything is written.
touch "$STUB/broker-running"
# In a variable: bash 3.2 brace-expands a double-quoted {a,b} word inside "$(...)".
ALL='{"mode":"cloud","scope":"all","client_id":"x","client_secret":"y"}'
[ "$(CONFIRM_SCOPE_ALL=1 ja "$ALL" jm)" = 2 ] \
  && [ "$(jlast error)" = bad_input ] || fail "add --json scope all must be bad_input"
[ "$(ja "$MANUAL" jm --force)" = 2 ] && [ "$(jlast error)" = bad_input ] || fail "add --json --force must be bad_input"
[ "$(ja "$MANUAL" local)" = 2 ] && [ "$(jlast error)" = bad_input ] || fail "add --json slug local"
[ "$(ja "$MANUAL" Bad_1)" = 2 ] && [ "$(jlast error)" = bad_slug ] || fail "add --json bad slug"
[ "$(ja "not json" jm)" = 2 ] && [ "$(jlast error)" = bad_input ] || fail "add --json bad JSON"
[ "$(ja '{"mode":"cloud","unlock":"stored","client_id":"x","client_secret":"y"}' jm)" = 2 ] \
  && [ "$(jlast error)" = bad_input ] || fail "add --json stored without a master password"
[ "$(ja '{"mode":"moon","client_id":"x","client_secret":"y"}' jm)" = 2 ] && [ "$(jlast error)" = bad_input ] \
  || fail "add --json bad mode"
gone jm "refusals"
[ "$(recreates)" = 0 ] || fail "a refused add --json touched the broker"

# Manual add with the broker signed in: done, the files written, a stale bw
# login emptied first, another manual account named in the message.
inst other '{"mode":"cloud","unlock":"manual"}'
printf 'jm\tcloud\tfolder\tlocked\t-\t1\t\nother\tcloud\tfolder\tlocked\t-\t0\t\n' > "$STUB/accounts"
mkdir -p "$D/bw/jm"
touch "$D/bw/jm/stale"
touch "$STUB/record-ps"
[ "$(ja "$MANUAL" jm)" = 0 ] || fail "add --json manual"
only_json "add --json"
[ "$(jlast phase)" = "done" ] && [ "$(jlast account)" = jm ] || fail "add --json ends with done for jm"
jlast message | grep -q "other" || fail "the done message names the manual account the restart locked"
grep -q '"phase":"setup","step":"write"' "$out" && grep -q '"phase":"setup","step":"check"' "$out" \
  || fail "add --json prints its write and check steps"
[ "$(jget "$INST/jm.json" unlock)" = manual ] || fail "add --json defaults to manual unlock"
[ "$(jget "$INST/jm.json" scope)" = folder:Agent ] || fail "add --json defaults to folder:Agent"
[ "$(jget "$INST/jm.json" region)" = us ] && [ "$(jget "$INST/jm.json" mode)" = cloud ] || fail "add --json mode and region"
logged "stub: instance jm.json at up" || fail "add --json writes the instance file before the broker restart"
[ "$(cat "$D/secrets/jm/client_secret")" = "$SECRET" ] || fail "add --json writes client_secret"
[ "$(cat "$D/secrets/jm/client_id")" = "user.syn-$$" ] || fail "add --json writes client_id"
grep -qF "user.syn-$$" "$INST/jm.json" && fail "add --json put the client_id in the instance file"
[ ! -e "$D/secrets/jm/master_password" ] || fail "manual add --json stores no master password"
[ ! -e "$D/bw/jm/stale" ] || fail "add --json left the stale bw login"
logged "stub: stale bw login at up" && fail "the broker started with the stale bw login"
cmp -s "$ENV_FILE" "$tmp/env.before" || fail "add --json wrote .env"
logged "bitwarden-setup create" && fail "add --json offered bitwarden-setup"
[ -s "$STUB/ps" ] || fail "the ps record is empty"
grep -qF -e "$SECRET" -e "user.syn-$$" "$STUB/ps" && fail "a secret reached an argv during add --json"
no_secret_json "add --json"
rm -f "$STUB/record-ps" "$STUB/ps"
[ "$(ja "$MANUAL" jm)" = 2 ] && [ "$(jlast error)" = exists ] || fail "add --json over an existing account"

# remove --json.
: > "$STUB/log"
[ "$(jr --slug jm --json)" = 0 ] && [ "$(jlast phase)" = "done" ] || fail "remove --json"
only_json "remove --json"
jlast message | grep -q "revoke" || fail "remove --json reminds to revoke the key"
[ "$(recreates)" = 1 ] || fail "remove --json restarts the broker"
logged "stub: instance jm.json at up" && fail "remove --json restarts the broker before it deletes the instance file"
gone jm "remove --json"
# No account file: done, with what is left removed, and no restart while the
# broker does not list the account.
printf 'other\tcloud\tfolder\tlocked\t-\t0\t\n' > "$STUB/accounts"
mkdir -p "$D/secrets/jm" "$D/bw/jm"
touch "$D/secrets/jm/client_secret" "$D/bw/jm/data.json"
: > "$STUB/log"
[ "$(jr --slug jm --json)" = 0 ] && [ "$(jlast phase)" = "done" ] || fail "remove --json of a missing account is done"
only_json "remove --json of a missing account"
jlast message | grep -q "had no instance file" || fail "remove --json of a missing account says so"
gone jm "remove --json of a missing account"
[ "$(recreates)" = 0 ] || fail "remove --json of an account the broker does not hold restarted it"

# The restart fails: command_failed, with bw/<slug>/ already removed. A retry
# restarts the broker, which still lists the account, and ends with done.
inst jf '{"mode":"cloud","unlock":"manual"}'
mkdir -p "$D/secrets/jf" "$D/bw/jf"
touch "$D/secrets/jf/client_secret" "$D/bw/jf/data.json"
printf 'jf\tcloud\tfolder\tlocked\t-\t0\t\n' > "$STUB/accounts"
echo 1 > "$STUB/rc.up"
: > "$STUB/log"
[ "$(jr --slug jf --json)" = 4 ] && [ "$(jlast error)" = command_failed ] || fail "remove --json when the restart fails"
only_json "remove --json when the restart fails"
gone jf "remove --json when the restart fails"
rm -f "$STUB/rc.up"
: > "$STUB/log"
[ "$(jr --slug jf --json)" = 0 ] && [ "$(jlast phase)" = "done" ] || fail "remove --json retry after a failed restart"
[ "$(recreates)" = 1 ] || fail "the retry did not restart the broker that still holds jf"
printf 'jm\tcloud\tfolder\tlocked\t-\t1\t\nother\tcloud\tfolder\tlocked\t-\t0\t\n' > "$STUB/accounts"
[ "$(jr --slug local --json)" = 2 ] && [ "$(jlast error)" = bad_input ] || fail "remove --json local"
rm -f "$INST/other.json"

# Failures after the write: each leaves nothing and restarts the broker again.
# check SLUG PAYLOAD ROW ERROR
check_fail() {
  : > "$STUB/log"
  printf '%s\n' "$3" > "$STUB/accounts"
  [ "$(ja "$2" "$1")" = 4 ] && [ "$(jlast error)" = "$4" ] || fail "add --json ending in $4"
  only_json "$4"
  gone "$1" "$4"
  [ "$(recreates)" = 2 ] || fail "$4: the broker was not restarted without the account"
  no_secret_json "$4"
}
check_fail jr "$MANUAL" "$(printf 'jr\tcloud\tfolder\terror\tauth\t0\t')" login_refused
check_fail jn "$STORED" "$(printf 'jn\tcloud\tfolder\toffline\tnetwork\t0\t')" server_unreachable
check_fail jl "$STORED" "$(printf 'jl\tcloud\tfolder\terror\trate_limited\t0\t120')" rate_limited
[ "$(tail -1 "$out" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("retryAfter"))')" = 120 ] \
  || fail "rate_limited carries retryAfter 120"
check_fail jt "$STORED" "$(printf 'jt\tcloud\tfolder\tlocked\t-\t1\t')" timeout
check_fail ju "$MANUAL" "$(printf 'ju\tcloud\tfolder\tlocked\t-\t0\t')" timeout
check_fail jx "$STORED" "$(printf 'jx\tcloud\tfolder\terror\tunknown\t0\t')" command_failed

# bw/<slug>/ goes after the rollback's broker restart, since the old broker
# can write its sign-in there until it is replaced.
echo jp > "$STUB/bw-at-up"
check_fail jp "$MANUAL" "$(printf 'jp\tcloud\tfolder\terror\tauth\t0\t')" login_refused
rm -f "$STUB/bw-at-up"

# bw/ the host cannot search (Linux, uid 10001, mode 700): the broker removes
# bw/<slug>/, a leftover login included.
mkdir -p "$D/bw/jm"
touch "$D/bw/jm/stale"
chmod 000 "$D/bw"
: > "$STUB/log"
printf 'jm\tcloud\tfolder\tlocked\t-\t1\t\n' > "$STUB/accounts"
[ "$(ja "$MANUAL" jm)" = 0 ] && [ "$(jlast phase)" = "done" ] || fail "add --json with an unsearchable bw/"
logged "exec -T bitwarden-broker sh -c" || fail "unsearchable bw/: the broker did not remove bw/jm"
logged "stub: stale bw login at up" && fail "unsearchable bw/: the broker started with the stale bw login"
[ "$(jr --slug jm --json)" = 0 ] || fail "remove --json with an unsearchable bw/"
chmod 700 "$D/bw"
gone jm "remove --json with an unsearchable bw/"
# A failed connect: bw/<slug>/ the broker wrote is removed through the broker.
chmod 000 "$D/bw"
echo jq > "$STUB/bw-at-up"
: > "$STUB/log"
printf 'jq\tcloud\tfolder\terror\tauth\t0\t\n' > "$STUB/accounts"
[ "$(ja "$MANUAL" jq)" = 4 ] && [ "$(jlast error)" = login_refused ] || fail "unsearchable bw/: login_refused"
rm -f "$STUB/bw-at-up"
chmod 700 "$D/bw"
gone jq "unsearchable bw/, login_refused"
# The broker cannot remove it either: command_failed before anything is written.
mkdir -p "$D/bw/jv"
chmod 000 "$D/bw"
echo 1 > "$STUB/rc.bwrm"
: > "$STUB/log"
[ "$(ja "$MANUAL" jv)" = 4 ] && [ "$(jlast error)" = command_failed ] || fail "unsearchable bw/ the broker cannot empty"
rm -f "$STUB/rc.bwrm"
chmod 700 "$D/bw"
[ ! -e "$INST/jv.json" ] && [ ! -e "$D/secrets/jv" ] || fail "an unconfirmed bw/jv still let add write"
[ "$(recreates)" = 0 ] || fail "an unconfirmed bw/jv still restarted the broker"
rm -rf "$D/bw/jv"

# The broker does not come back up.
: > "$STUB/log"
echo 1 > "$STUB/rc.up"
[ "$(ja "$MANUAL" jd)" = 4 ] && [ "$(jlast error)" = command_failed ] || fail "add --json when the restart fails"
gone jd "restart failure"
rm -f "$STUB/rc.up"

# SIGTERM (addon.sh stopping the hook) in the middle of the check.
: > "$STUB/log"
printf 'jk\tcloud\tfolder\tlocked\t-\t0\t\n' > "$STUB/accounts"
printf '%s' "$MANUAL" > "$tmp/payload"
VAULT_BROKER_WAIT=60 STACK_PYTHON=host "$vault" add --slug jk --json < "$tmp/payload" > "$out" 2> "$tmp/jerr" &
pid=$!
n=0
until grep -q "broker.cli accounts" "$STUB/log" 2>/dev/null; do
  n=$((n + 1)); [ "$n" -lt 100 ] || { kill "$pid" 2>/dev/null; fail "add --json never polled the broker"; }
  sleep 0.1
done
[ -f "$INST/jk.json" ] || fail "SIGTERM case: the instance file was not written before the check"
kill -TERM "$pid"
rc=0; wait "$pid" || rc=$?
[ "$rc" != 0 ] || fail "add --json stopped by SIGTERM exited 0"
gone jk "SIGTERM"
[ "$(recreates)" = 2 ] || fail "SIGTERM: the broker was not restarted without the account"
only_json "SIGTERM"

rm -rf "$INST"
[ ! -d "$tmp/inst-kept3" ] || mv "$tmp/inst-kept3" "$INST"

# --- backup and restore --------------------------------------------------------
if command -v sqlite3 >/dev/null 2>&1; then
  reset_stub
  vw="$BITWARDEN_HOST_DATA_DIR/vaultwarden"
  printf '{\n  "scope": "collection:c-agent"\n}\n' > "$BITWARDEN_HOST_DATA_DIR/broker/local-account.json"
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
  [ -f "$tmp/bk/broker/local-account.json" ] || fail "backup copies the local account"
  [ -f "$tmp/bk/instances/personal.json" ] || fail "backup copies the instance files"
  run backup "$tmp/bk" && fail "backup into a non-empty directory"
  run restore "$tmp/bk" && fail "restore over existing data without --force"
  said "--force" || fail "the restore refusal mentions --force"
  sqlite3 "$vw/db.sqlite3" 'DELETE FROM t'
  rm -f "$BITWARDEN_HOST_DATA_DIR/secrets/personal/master_password" "$INST/personal.json" \
    "$BITWARDEN_HOST_DATA_DIR/broker/local-account.json"
  run restore "$tmp/bk" --force || fail "restore --force"
  [ -f "$INST/personal.json" ] && [ -f "$BITWARDEN_HOST_DATA_DIR/broker/local-account.json" ] \
    || fail "restore puts the instance files and the local account back"
  [ "$(sqlite3 "$vw/db.sqlite3" 'SELECT v FROM t')" = syn-row ] || fail "restore puts the database back"
  [ "$(mode_of "$BITWARDEN_HOST_DATA_DIR/secrets/personal/master_password")" = 600 ] || fail "restore keeps modes"
else
  echo "skip: backup and restore (no sqlite3)"
fi

printf 'OK vault.sh\n'
