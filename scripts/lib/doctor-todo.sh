# make doctor's per-profile Vikunja checks. Sourced by scripts/doctor.sh, which
# provides ok, warn, bad and env_get and runs from the repo root;
# scripts/test-todo-doctor.sh runs it against profile files.
# shellcheck shell=bash

# todo_profile_checks: for each Hermes profile with a config.yaml, whether the
# bootstrap registered mcp_servers.todos (a header that references
# ${TODOS_KEY}, and a non-empty TODOS_KEY in the profile's .env, as
# mcp.auth in addons/vikunja/addon.json has it), and a failure when api-server
# holds the read/write token.
todo_profile_checks() {
  local pair label cfg envfile rw
  for pair in "default:data/hermes/config.yaml:data/hermes/.env" \
    "api-server:data/hermes/profiles/api-server/config.yaml:data/hermes/profiles/api-server/.env" \
    "browser:data/hermes/profiles/browser/config.yaml:data/hermes/profiles/browser/.env"; do
    IFS=':' read -r label cfg envfile <<< "$pair"
    [[ -f "$cfg" ]] || continue
    # shellcheck disable=SC2016 # the literal reference the bootstrap writes
    if grep -qE '^  todos:' "$cfg" && grep -qF '${TODOS_KEY}' "$cfg" \
      && grep -qE '^TODOS_KEY=.+' "$envfile" 2>/dev/null; then
      ok "Hermes ${label} profile registers the todos MCP server"
    elif grep -qE '^  vikunja:' "$cfg"; then
      warn "Hermes ${label} profile still uses the old mcp_servers.vikunja key — run make hermes-reload"
    else
      warn "Hermes ${label} profile has no todos MCP entry — run make hermes-reload"
    fi
  done
  # The read-only token must be the one api-server holds.
  rw="$(env_get VIKUNJA_MCP_TOKEN_RW)"
  if [[ -n "$rw" ]] && grep -qsxF "TODOS_KEY=${rw}" data/hermes/profiles/api-server/.env; then
    bad "api-server holds the read/write Vikunja token — run make hermes-reload (unattended sessions must stay read-only)"
  fi
}
