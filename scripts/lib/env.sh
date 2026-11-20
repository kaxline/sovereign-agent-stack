# Shared .env helpers for stack scripts.
# Source from the repo root:  source "$(dirname "$0")/lib/env.sh"

# env_get KEY [DEFAULT]: KEY's value in ${ENV_FILE:-.env}. The first KEY= line
# wins, and one layer of matching quotes ("v" or 'v') is stripped; a value
# with a quote at one end only is kept as it is. An empty or missing value
# gives DEFAULT, as compose's ${KEY:-DEFAULT} does. Every stack script reads
# .env through this (install.sh keeps a self-contained copy).
env_get() {
  env_get_file "${ENV_FILE:-.env}" "$@"
}

# env_get_file FILE KEY [DEFAULT]: env_get on FILE.
env_get_file() {
  local env_file="$1" key="$2" default="${3:-}" val=""
  if [[ -f "$env_file" ]]; then
    val="$(grep -E "^${key}=" "$env_file" 2>/dev/null | head -1 | cut -d= -f2- || true)"
    if [[ ${#val} -ge 2 ]]; then
      case "$val" in
        \"*\" | \'*\') val="${val:1:${#val}-2}" ;;
      esac
    fi
  fi
  if [[ -z "$val" ]]; then
    echo "$default"
  else
    echo "$val"
  fi
}

# env_true KEY [DEFAULT]: whether KEY's value (env_get, DEFAULT 0) is 1, true,
# yes or on, in any case, ignoring spaces at either end. The rule of
# addon_manifest.truthy, so a flag means the same to the scripts and the
# add-on manifest.
env_true() {
  local v
  v="$(env_get "$1" "${2:-0}" | tr '[:upper:]' '[:lower:]')"
  v="${v#"${v%%[![:space:]]*}"}"
  v="${v%"${v##*[![:space:]]}"}"
  case "$v" in
    1 | true | yes | on) return 0 ;;
  esac
  return 1
}

# valid_slug SLUG: an add-on instance slug by addon_store.SLUG_RE's rule, a
# lowercase letter then at most 30 of a-z, 0-9 and -. A regex, not a glob:
# bash 3.2 bracket ranges follow the locale, where [a-z] also matches
# uppercase. The regex sits in a variable because bash 3.2 parses some
# unquoted ones in [[ =~ ]] differently.
ENV_SLUG_RE='^[a-z][a-z0-9-]{0,30}$'
valid_slug() {
  [[ "$1" =~ $ENV_SLUG_RE ]]
}

# env_target FILE: FILE, or what it links to when it is a symlink (a worktree
# links main's .env), so a replacement keeps the link. One level, which is all
# the stack makes; BSD readlink has no -f.
env_target() {
  local f="$1" t
  if [[ -L "$f" ]]; then
    t="$(readlink "$f")"
    [[ "$t" == /* ]] || t="$(dirname "$f")/${t}"
    f="$t"
  fi
  printf '%s\n' "$f"
}

# replace_file FILE CMD...: runs CMD with stdout to a temp file beside FILE,
# then renames it over FILE, keeping FILE's mode. The temp file is in the same
# directory so the rename is atomic: a run stopped halfway leaves FILE as it
# was. A temp file in $TMPDIR would make mv a copy wherever /tmp is another
# filesystem.
replace_file() {
  local f tmp mode rc=0
  f="$(env_target "$1")"
  shift
  tmp="$(mktemp "$(dirname "$f")/.$(basename "$f").XXXXXX")" || return 1
  "$@" > "$tmp" || rc=$?
  if [[ "$rc" != 0 ]]; then
    rm -f "$tmp"
    return "$rc"
  fi
  if [[ -e "$f" ]]; then
    mode="$(stat -c %a "$f" 2>/dev/null || stat -f %Lp "$f")"
    chmod "$mode" "$tmp"
  else
    chmod "$(printf '%o' $((0666 & ~$(umask))))" "$tmp"
  fi
  mv -f "$tmp" "$f"
}

_env_rewrite() {
  local file="$1" key="$2" value="$3" line seen=0
  if [[ -f "$file" ]]; then
    while IFS= read -r line || [[ -n "$line" ]]; do
      if [[ "$line" == "${key}="* ]]; then
        [[ "$seen" == 1 ]] || printf '%s=%s\n' "$key" "$value"
        seen=1
      else
        printf '%s\n' "$line"
      fi
    done < "$file"
  fi
  [[ "$seen" == 1 ]] || printf '%s=%s\n' "$key" "$value"
}

_env_drop() {
  grep -v -E "^$2=" "$1" || true
}

# upsert_env FILE KEY VALUE: sets KEY=VALUE, replacing the first KEY= line
# (and dropping later duplicates) or appending one. Atomic (replace_file).
upsert_env() {
  replace_file "$1" _env_rewrite "$(env_target "$1")" "$2" "$3"
}

# remove_env FILE KEY: deletes every KEY= line. Atomic (replace_file).
remove_env() {
  [[ -f "$1" ]] || return 0
  replace_file "$1" _env_drop "$(env_target "$1")" "$2"
}

has_compose_profile() {
  local profile="$1"
  local profiles_csv
  profiles_csv="$(env_get COMPOSE_PROFILES core)"
  [[ ",${profiles_csv}," == *",${profile},"* ]]
}

# docker compose with every profile COMPOSE_PROFILES enables (the caller's
# environment, else .env, else core) plus PROFILE. A --profile flag on its own
# replaces COMPOSE_PROFILES, and compose then rejects the whole project when an
# active service depends on one outside it: bitwarden-broker depends on hermes,
# which is in core.
#   compose_with_profile bitwarden ps -q bitwarden-broker
compose_with_profile() {
  local extra="$1" csv p
  shift
  local -a args=()
  csv="${COMPOSE_PROFILES:-$(env_get COMPOSE_PROFILES core)}"
  local IFS=','
  for p in $csv; do
    p="${p//[[:space:]]/}"
    [[ -z "$p" || "$p" == "$extra" ]] || args+=(--profile "$p")
  done
  args+=(--profile "$extra")
  docker compose "${args[@]}" "$@"
}

# Gateway topology: HERMES_GATEWAY_TOPOLOGY (standalone|multiplex) from the
# environment or .env; unset, HERMES_PIN_STANDALONE_GATEWAYS=0 means multiplex.
# Same rule as compose/hermes/019-pin-profile-gateways.sh.
gateway_topology() {
  local t pin
  t="${HERMES_GATEWAY_TOPOLOGY:-$(env_get HERMES_GATEWAY_TOPOLOGY)}"
  t="$(printf '%s' "$t" | tr '[:upper:]' '[:lower:]')"
  case "$t" in
    standalone | multiplex) echo "$t"; return ;;
  esac
  pin="${HERMES_PIN_STANDALONE_GATEWAYS:-$(env_get HERMES_PIN_STANDALONE_GATEWAYS 1)}"
  case "$(printf '%s' "$pin" | tr '[:upper:]' '[:lower:]')" in
    0 | false | no | off) echo multiplex ;;
    *) echo standalone ;;
  esac
}
