# make doctor's CRM (Twenty) block. Sourced by scripts/doctor.sh, which
# provides ok, warn, bad, env_get and has_profile and runs from the repo root;
# scripts/test-twenty-doctor.sh runs it against stubs.
# shellcheck shell=bash

# A string field from an instance file, without a JSON parser.
twenty_json_get() {
  sed -n "s/.*\"${2}\": *\"\\([^\"]*\\)\".*/\\1/p" "$1" 2>/dev/null | head -1 || true
}

# The local setting of an instance file: true, false, "" when absent (the
# manifest's default, false) or "bad" when it isn't a JSON bool.
twenty_json_local() {
  grep -Eq '"local":' "$1" 2>/dev/null || return 0
  if grep -Eq '"local": *true *(,|}|$)' "$1"; then
    printf true
  elif grep -Eq '"local": *false *(,|}|$)' "$1"; then
    printf false
  else
    printf bad
  fi
}

# Why the bootstrap would refuse a key file, as addon_manifest.py's
# _key_problem says it, or nothing. Never prints the value.
twenty_key_problem() {
  if [[ ! -f "$1" ]]; then
    printf missing
  elif [[ ! -s "$1" ]]; then
    printf empty
  elif [[ "$(LC_ALL=C tr -d '\000-\037\177' < "$1" | wc -c)" -ne "$(wc -c < "$1")" ]]; then
    printf 'holds a line break or control character'
  fi
}

doctor_twenty() {
  local twenty_local twenty_mcp twenty_port data_root store f slug pair
  twenty_local="$(env_get TWENTY_LOCAL_ENABLED 0)"
  case "$twenty_local" in 1 | true | TRUE | yes | YES) twenty_local=1 ;; *) twenty_local=0 ;; esac
  twenty_mcp="$(env_get TWENTY_MCP_ENABLED 0)"
  case "$twenty_mcp" in 1 | true | TRUE | yes | YES) twenty_mcp=1 ;; *) twenty_mcp=0 ;; esac
  data_root="$(env_get ASSISTANT_DATA_ROOT ./data)"
  [[ "$data_root" == /* ]] || data_root="${ROOT}/${data_root#./}"
  store="${data_root}/addons/twenty"

  if has_profile crm || [[ "$twenty_local" -eq 1 || "$twenty_mcp" -eq 1 ]]; then
    echo
    echo "--- CRM (Twenty) ---"
  else
    return 0
  fi

  twenty_port="$(env_get TWENTY_PORT 3000)"
  if [[ "$twenty_local" -eq 1 ]]; then
    if has_profile crm; then
      ok "COMPOSE_PROFILES includes crm"
    else
      bad "TWENTY_LOCAL_ENABLED=1 but 'crm' not in COMPOSE_PROFILES — run make ensure-local"
    fi
    for pair in TWENTY_ENCRYPTION_KEY TWENTY_DB_PASSWORD; do
      if [[ -n "$(env_get "$pair")" && -n "$(env_get "${pair}_FILE")" ]]; then
        bad "${pair} and ${pair}_FILE are both set — keep one (the containers refuse to start)"
      elif [[ -n "$(env_get "${pair}_FILE")" ]]; then
        if [[ -s "$(env_get "${pair}_FILE")" ]]; then
          ok "${pair} comes from a file"
        else
          bad "${pair}_FILE points at a missing or empty file"
        fi
      elif [[ -n "$(env_get "$pair")" ]]; then
        ok "${pair} is set"
      else
        bad "${pair} is unset — run ./scripts/setup.sh"
      fi
    done
    if docker info >/dev/null 2>&1; then
      local svc cid health
      for svc in twenty-server twenty-worker twenty-db twenty-redis; do
        cid="$(docker compose ps -q "$svc" 2>/dev/null || true)"
        health=""
        if [[ -n "$cid" ]]; then
          health="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$cid" 2>/dev/null || true)"
        fi
        case "$health" in
          healthy) ok "${svc} is healthy" ;;
          starting) warn "${svc} is starting (the first start runs Twenty's migrations)" ;;
          "") warn "${svc} not running — make up" ;;
          *) bad "${svc} is ${health} — docker compose logs ${svc}" ;;
        esac
      done
    fi
    if [[ ! -f "$(env_get TWENTY_DATA_DIR ./data/twenty)/.crm-init-done" ]]; then
      warn "no local workspace yet — after make up, run make crm-init"
    fi
  fi

  if [[ "$twenty_mcp" -eq 1 ]]; then
    ok "TWENTY_MCP_ENABLED=1"
    local twenty_count=0 url access local_ rw_f has_ro has_rw why key kind k probe hdr code label cfg env_key rw
    for f in "$store/instances"/*.json; do
      [[ -f "$f" ]] || continue
      slug="$(basename "$f" .json)"
      twenty_count=$((twenty_count + 1))
      url="$(twenty_json_get "$f" url)"
      # As instances-plan reads it: a missing access is the manifest's
      # default (read), and one that isn't a string is refused.
      access="$(twenty_json_get "$f" access)"
      if [[ -z "$access" ]] && grep -q '"access":' "$f"; then
        grep -Eq '"access": *""' "$f" || access="not a string"
      fi
      access="${access:-read}"
      local_="$(twenty_json_local "$f")"
      rw_f="${store}/secrets/${slug}.key_rw"
      has_ro=0; has_rw=0
      for kind in ro rw; do
        why="$(twenty_key_problem "${store}/secrets/${slug}.key_${kind}")"
        if [[ -z "$why" ]]; then
          if [[ "$kind" == ro ]]; then has_ro=1; else has_rw=1; fi
        elif [[ "$why" != missing ]]; then
          bad "crm-${slug}: ${slug}.key_${kind} ${why}, so Hermes won't use it — write the key on one line with no newline"
        fi
      done
      if [[ "$local_" == bad ]]; then
        bad "crm-${slug}: local must be true or false in ${f} — Hermes gets no entry for it"
        continue
      fi
      if [[ "$twenty_local" -eq 0 && "$local_" == true ]]; then
        if grep -qsE "^  crm-${slug}:" data/hermes/config.yaml; then
          warn "crm-${slug}: the local instance is off but Hermes still registers it — run make hermes-reload"
        else
          ok "crm-${slug}: not registered while the local instance is off (TWENTY_LOCAL_ENABLED=0)"
        fi
        continue
      fi
      if [[ "$access" != read && "$access" != write ]]; then
        bad "crm-${slug}: access is ${access} in ${f}; use read or write — Hermes gets no entry for it"
        continue
      fi
      if [[ "$has_ro$has_rw" == 00 ]]; then
        bad "crm-${slug}: no keys in ${store}/secrets — make crm-init finishes a local one; make crm-add FORCE=1 replaces a remote one"
        continue
      elif [[ "$access" == read && "$has_ro" -eq 0 ]] || [[ "$access" == write && "$has_rw" -eq 0 ]]; then
        bad "crm-${slug}: access is ${access} but ${slug}.key_$([[ "$access" == read ]] && echo ro || echo rw) is missing from ${store}/secrets — Hermes gets no entry for it"
        continue
      fi
      ok "crm-${slug}: access ${access}, keys present: $([[ $has_ro -eq 1 ]] && echo ro)$([[ $has_ro -eq 1 && $has_rw -eq 1 ]] && echo ' ')$([[ $has_rw -eq 1 ]] && echo rw)"
      # The local instance is checked through its host port; the key alone
      # selects the workspace, whatever the hostname.
      probe="${url%/}"
      if [[ "$local_" == true || "$probe" == http://twenty-server:3000* ]]; then
        probe="http://127.0.0.1:${twenty_port}"
      fi
      probe="${probe%/mcp}/mcp"
      for kind in rw ro; do
        k="${store}/secrets/${slug}.key_${kind}"
        [[ "$kind" == rw && "$has_rw" -eq 1 ]] || [[ "$kind" == ro && "$has_ro" -eq 1 ]] || continue
        key="$(cat "$k")"
        # The key goes in a header file, not argv, so ps never shows it.
        hdr="$(mktemp)"
        chmod 600 "$hdr"
        printf 'Authorization: Bearer %s\nContent-Type: application/json\nAccept: application/json, text/event-stream\n' "$key" > "$hdr"
        code="$(curl -s -o /dev/null -m 15 -w '%{http_code}' -H @"$hdr" -X POST "$probe" \
          --data '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"doctor","version":"0"}}}' 2>/dev/null || true)"
        rm -f "$hdr"
        key=""
        case "$code" in
          200) ok "crm-${slug}: MCP answers with the ${kind} key (${probe})" ;;
          401 | 403) bad "crm-${slug}: Twenty rejects the ${kind} key — make crm-init ROTATE=1, or new keys with make crm-add FORCE=1" ;;
          000) warn "crm-${slug}: no answer from ${probe}" ;;
          *) warn "crm-${slug}: MCP answered HTTP ${code} at ${probe}" ;;
        esac
      done
      for pair in "default:data/hermes/config.yaml:data/hermes/.env" \
        "api-server:data/hermes/profiles/api-server/config.yaml:data/hermes/profiles/api-server/.env" \
        "browser:data/hermes/profiles/browser/config.yaml:data/hermes/profiles/browser/.env"; do
        IFS=":" read -r label cfg _ <<< "$pair"
        [[ -f "$cfg" ]] || continue
        if grep -qE "^  crm-${slug}:" "$cfg"; then
          ok "Hermes ${label} profile registers crm-${slug}"
        elif [[ "$label" != api-server ]] || [[ "$has_ro" -eq 1 ]]; then
          warn "Hermes ${label} profile has no crm-${slug} entry — run make hermes-reload"
        fi
      done
      env_key="CRM_$(printf '%s' "$slug" | tr '[:lower:]-' '[:upper:]_')_KEY"
      if [[ "$has_rw" -eq 1 ]]; then
        # Through the environment, not argv, so ps never shows the key.
        rw="$(cat "$rw_f")"
        if [[ -f data/hermes/profiles/api-server/.env ]] \
          && ENV_LINE="${env_key}=${rw}" awk '$0 == ENVIRON["ENV_LINE"] { f = 1 } END { exit !f }' data/hermes/profiles/api-server/.env; then
          bad "api-server holds crm-${slug}'s read/write key — run make hermes-reload (unattended sessions must stay read-only)"
        fi
        rw=""
      fi
    done
    if [[ "$twenty_count" -eq 0 ]]; then
      warn "no workspaces in ${store}/instances — make crm-init (local) or make crm-add (remote)"
    fi
  elif [[ "$twenty_local" -eq 1 ]]; then
    warn "crm profile on but TWENTY_MCP_ENABLED is not 1 — Hermes will not get the CRM tools"
  fi
}
