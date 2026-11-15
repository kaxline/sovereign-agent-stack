#!/usr/bin/env bash
# The secrets a client app needs to reach the stack, as one JSON object.
#
#   scripts/credentials.sh --json
#
# Prints
#   {"HERMES_DASHBOARD_PASSWORD":S|null,"HERMES_DASHBOARD_HANDOFF_SECRET":S|null,
#    "API_SERVER_KEY":S|null,"LLM_PROXY_ADMIN_TOKEN":S|null}
# from .env, except API_SERVER_KEY, which comes from compose/hermes/api-server.env.
# A value that is missing, empty or still an example placeholder (change-me...)
# is null. A missing file is not an error. Read-only; the stack can be stopped.
#
# The output is every key a client holds for the dashboard, its sign-in
# handoff, the API server and the LLM proxy's admin API. Treat it like .env.
#
# Exit 2 (error bad_input) for an unknown option or without --json.
#
# bash 3.2 compatible.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
# shellcheck source=lib/env.sh
source "${ROOT}/scripts/lib/env.sh"
# shellcheck source=lib/json-lines.sh
source "${ROOT}/scripts/lib/json-lines.sh"
jl_init

while [[ $# -gt 0 ]]; do
  case "$1" in
    --json) JL_JSON=1; shift ;;
    -h | --help) sed -n '2,17p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) jl_fail bad_input "" "unknown option $1" ;;
  esac
done
[[ "$JL_JSON" == 1 ]] || jl_fail bad_input "" "credentials.sh prints JSON only; pass --json"

# secret FILE KEY: KEY's value in FILE as a JSON string, or null when it is
# missing, empty or a change-me placeholder.
secret() {
  local val
  val="$(ENV_FILE="$1" env_get "$2")"
  case "$val" in
    "" | change-me* | change_me*) printf 'null' ;;
    *) jl_str "$val" ;;
  esac
}

printf '{"HERMES_DASHBOARD_PASSWORD":%s,"HERMES_DASHBOARD_HANDOFF_SECRET":%s,"API_SERVER_KEY":%s,"LLM_PROXY_ADMIN_TOKEN":%s}\n' \
  "$(secret .env HERMES_DASHBOARD_PASSWORD)" \
  "$(secret .env HERMES_DASHBOARD_HANDOFF_SECRET)" \
  "$(secret compose/hermes/api-server.env API_SERVER_KEY)" \
  "$(secret .env LLM_PROXY_ADMIN_TOKEN)" >&3
