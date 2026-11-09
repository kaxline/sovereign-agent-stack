# make doctor's CRM (Twenty) block. Sourced by scripts/doctor.sh, which
# provides ok, warn, bad, env_get and has_profile and runs from the repo root;
# scripts/test-twenty-doctor.sh runs it against stubs.
# shellcheck shell=bash

# A string field from an instance file, without a JSON parser.
twenty_json_get() {
  sed -n "s/.*\"${2}\": *\"\\([^\"]*\\)\".*/\\1/p" "$1" 2>/dev/null | head -1 || true
}

# A value from a legacy workspace file, read as crm.sh's ws_get reads it: last
# match, CR and surrounding whitespace dropped, then one pair of quotes.
twenty_ws_get() {
  local v
  v="$(grep -E "^${2}=" "$1" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '\r' \
    | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' || true)"
  v="${v%\"}"; v="${v#\"}"
  v="${v%\'}"; v="${v#\'}"
  printf '%s' "$v"
}

# Why a legacy workspace file cannot migrate, or nothing. Mirrors the checks in
# crm.sh migrate_workspaces (valid_slug, scheme, has_userinfo, key_ok, access,
# json_safe), in the same order. Names the rule, never a value.
twenty_unmigratable() {
  local f="$1" slug url access v name
  slug="$(basename "$f" .env)"
  if ! [[ "$slug" =~ ^[a-z][a-z0-9-]{0,30}$ ]]; then
    printf '%s' "the name must start with a letter and be at most 31 lowercase letters, digits and hyphens"
    return 0
  fi
  url="$(twenty_ws_get "$f" TWENTY_URL)"
  url="${url%/}"
  if ! [[ "$url" =~ ^https?:// ]]; then
    printf '%s' "TWENTY_URL is not an http(s) URL"
    return 0
  fi
  if [[ "$url" =~ ^[A-Za-z]+://[^/?#]*@ ]]; then
    printf '%s' "the URL carries user:password@"
    return 0
  fi
  for name in TWENTY_KEY_RO TWENTY_KEY_RW; do
    v="$(twenty_ws_get "$f" "$name")"
    if [[ "$v" == *[[:space:][:cntrl:]]* ]]; then
      printf '%s' "a key holds whitespace or a control character"
      return 0
    fi
  done
  access="$(twenty_ws_get "$f" TWENTY_ACCESS)"
  case "${access:-write}" in read | write) ;; *) printf '%s' "TWENTY_ACCESS must be read or write"; return 0 ;; esac
  for name in TWENTY_URL TWENTY_WORKSPACE_URL TWENTY_WORKSPACE_ID TWENTY_KEY_RO TWENTY_KEY_RW; do
    v="$(twenty_ws_get "$f" "$name")"
    case "$v" in
      *\"* | *\\*) printf '%s' "a value holds a quote or backslash"; return 0 ;;
    esac
    if [[ "$v" == *[[:cntrl:]]* ]]; then
      printf '%s' "a value holds a control character"
      return 0
    fi
  done
  return 0
}

doctor_twenty() {
  local twenty_local twenty_mcp twenty_port data_root store f slug leftovers=0 pair
  twenty_local="$(env_get TWENTY_LOCAL_ENABLED 0)"
  case "$twenty_local" in 1 | true | TRUE | yes | YES) twenty_local=1 ;; *) twenty_local=0 ;; esac
  twenty_mcp="$(env_get TWENTY_MCP_ENABLED 0)"
  case "$twenty_mcp" in 1 | true | TRUE | yes | YES) twenty_mcp=1 ;; *) twenty_mcp=0 ;; esac
  data_root="$(env_get ASSISTANT_DATA_ROOT ./data)"
  [[ "$data_root" == /* ]] || data_root="${ROOT}/${data_root#./}"
  store="${data_root}/addons/twenty"
  for f in compose/twenty/workspaces/*.env; do
    [[ -f "$f" ]] && leftovers=$((leftovers + 1))
  done

  if has_profile crm || [[ "$twenty_local" -eq 1 || "$twenty_mcp" -eq 1 || "$leftovers" -gt 0 ]]; then
    echo
    echo "--- CRM (Twenty) ---"
  else
    return 0
  fi

  for f in compose/twenty/workspaces/*.env; do
    [[ -f "$f" ]] || continue
    local why
    why="$(twenty_unmigratable "$f")"
    if [[ -f "${store}/instances/$(basename "$f" .env).json" ]]; then
      warn "${f}: ${store} already has this workspace, so migrate keeps the stored one and leaves the file. Delete the file once you've checked the store"
    elif [[ -n "$why" ]]; then
      warn "${f} was not migrated to ${store}, and can't be: ${why}. Fix the file, then run ./scripts/crm.sh migrate"
    else
      warn "${f} was not migrated to ${store} — run ./scripts/crm.sh migrate"
    fi
  done

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
    local twenty_count=0 url access ro_f rw_f has_ro has_rw key kind k probe hdr code label cfg env_key rw
    for f in "$store/instances"/*.json; do
      [[ -f "$f" ]] || continue
      slug="$(basename "$f" .json)"
      twenty_count=$((twenty_count + 1))
      url="$(twenty_json_get "$f" url)"
      access="$(twenty_json_get "$f" access)"
      access="${access:-write}"
      ro_f="${store}/secrets/${slug}.key_ro"
      rw_f="${store}/secrets/${slug}.key_rw"
      has_ro=0; has_rw=0
      [[ -s "$ro_f" ]] && has_ro=1
      [[ -s "$rw_f" ]] && has_rw=1
      if [[ "$twenty_local" -eq 0 && "${url%/}" =~ ^http://twenty-server:3000(/mcp)?$ ]]; then
        if grep -qsE "^  crm-${slug}:" data/hermes/config.yaml; then
          warn "crm-${slug}: the local instance is off but Hermes still registers it — run make hermes-reload"
        else
          ok "crm-${slug}: not registered while the local instance is off (TWENTY_LOCAL_ENABLED=0)"
        fi
        continue
      fi
      if [[ "$access" != read && "$access" != write ]]; then
        bad "crm-${slug}: access is ${access} in ${f}; use read or write"
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
      [[ "$probe" == http://twenty-server:3000* ]] && probe="http://127.0.0.1:${twenty_port}"
      probe="${probe%/mcp}/mcp"
      for kind in rw ro; do
        k="${store}/secrets/${slug}.key_${kind}"
        [[ -s "$k" ]] || continue
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
