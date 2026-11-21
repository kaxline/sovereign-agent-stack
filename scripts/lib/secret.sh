# Random secrets for stack scripts, read from /dev/urandom because the host
# may lack openssl. Every script that generates a secret goes through here;
# scripts/test-secret-sh.sh keeps them from calling openssl rand or reading
# /dev/urandom themselves. Safe under /bin/bash 3.2.
#
#   # shellcheck source=lib/secret.sh
#   source "${ROOT}/scripts/lib/secret.sh"

# random_value ENCODING BYTES: BYTES random bytes as lowercase hex (2*BYTES
# characters) or base64 (padded, one line), the formats of `openssl rand -hex`
# and `openssl rand -base64`, without the newline. Fails when nothing came out.
random_value() {
  local v
  case "$1" in
    hex) v="$(od -An -v -tx1 -N "$2" /dev/urandom | tr -d ' \n')" ;;
    base64) v="$(head -c "$2" /dev/urandom | base64 | tr -d '\n')" ;;
    *)
      printf 'random_value: unknown encoding %s\n' "$1" >&2
      return 2
      ;;
  esac
  [[ -n "$v" ]] || return 1
  printf '%s' "$v"
}

# secret_file_fail MODE MESSAGE: reports a failure of the functions below. In
# die mode it calls the script's own die when there is one (so its prefix and
# --json handling apply), else prints `error: MESSAGE` and exits 1. In warn
# mode it calls the script's warn when there is one, else prints
# `warning: MESSAGE`, and returns 1.
secret_file_fail() {
  if [[ "$1" == warn ]]; then
    if declare -F warn > /dev/null; then warn "$2"; else printf 'warning: %s\n' "$2" >&2; fi
    return 1
  fi
  if declare -F die > /dev/null; then die "$2"; fi
  printf 'error: %s\n' "$2" >&2
  exit 1
}

# SECRET_FILE_FIX: the caller's hint for a folder or file it can't write,
# appended to that message after "; ", e.g. "run once: sudo chown -R ...".
SECRET_FILE_FIX="${SECRET_FILE_FIX:-}"

secret_file_fix() {
  [[ -z "$SECRET_FILE_FIX" ]] || printf '; %s' "$SECRET_FILE_FIX"
}

# ensure_secret_dir DIR [warn|die]: DIR exists as a folder at mode 0700.
# Default mode die.
ensure_secret_dir() {
  local dir="$1" mode="${2:-die}"
  if [[ -e "$dir" && ! -d "$dir" ]]; then
    secret_file_fail "$mode" "${dir} exists and is not a folder"
    return 1
  fi
  if ! (umask 077 && mkdir -p "$dir") 2> /dev/null || ! chmod 700 "$dir" 2> /dev/null; then
    secret_file_fail "$mode" "could not create ${dir} at 0700$(secret_file_fix)"
    return 1
  fi
}

# write_secret_file PATH [warn|die] [newline|bare]: replaces PATH with 32
# random bytes as hex, at mode 0600 in a 0700 folder. The value goes to a
# temporary file beside PATH that is then renamed over it, so a reader never
# sees a partial token. newline (the default) ends the file with a newline,
# as `openssl rand -hex 32 > PATH` did; bare writes the 64 characters alone.
# Sets SECRET_FILE_CREATED=1 on success. Default mode die.
write_secret_file() {
  local path="$1" mode="${2:-die}" end="${3:-newline}" dir tmp value
  SECRET_FILE_CREATED=0
  dir="$(dirname "$path")"
  ensure_secret_dir "$dir" "$mode" || return 1
  if [[ -d "$path" ]]; then
    # Docker creates a missing bind-mount source as a directory.
    secret_file_fail "$mode" "${path} is a directory, so no token was written; stop the stack, remove it (sudo rmdir '${path}') and run this again"
    return 1
  fi
  if ! tmp="$(umask 077 && mktemp "${dir}/.$(basename "$path").XXXXXX" 2> /dev/null)"; then
    secret_file_fail "$mode" "could not write ${path}$(secret_file_fix)"
    return 1
  fi
  if ! value="$(random_value hex 32)"; then
    rm -f "$tmp"
    secret_file_fail "$mode" "could not read /dev/urandom for ${path}"
    return 1
  fi
  [[ "$end" == bare ]] || value="${value}"$'\n'
  if ! printf '%s' "$value" > "$tmp" || ! chmod 600 "$tmp" || ! mv -f "$tmp" "$path"; then
    rm -f "$tmp"
    secret_file_fail "$mode" "could not write ${path}$(secret_file_fix)"
    return 1
  fi
  SECRET_FILE_CREATED=1
}

# ensure_secret_file PATH [warn|die] [newline|bare]: write_secret_file unless
# PATH is already a non-empty file, which is kept (and set to 0600 where this
# user can). SECRET_FILE_CREATED says which happened. Call it in this shell,
# not in $(...).
ensure_secret_file() {
  local path="$1"
  SECRET_FILE_CREATED=0
  if [[ -f "$path" && -s "$path" ]]; then
    chmod 600 "$path" 2> /dev/null || true
    return 0
  fi
  write_secret_file "$@"
}
