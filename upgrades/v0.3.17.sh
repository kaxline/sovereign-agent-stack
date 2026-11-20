#!/usr/bin/env bash
# v0.3.17: opencode-mcp wait fix, bitwarden_signin volume options, .env
# inline comments. CHANGELOG.md "v0.3.17" has the detail.
set -euo pipefail
# shellcheck source=lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
cd "$UPGRADE_ROOT"

# The coding_wait_for_task fix is in opencode-mcp's locally built image.
if enabled OPENCODE_MCP_ENABLED; then
  rebuild opencode-mcp
fi

# .env.example before v0.3.17 put a comment after these values. Compose drops
# it, but env_get keeps it, so host scripts read NEO4J_USERNAME as
# "neo4j   # Neo4j requires...". Drop an unquoted " # comment" from each.
if [[ -f .env ]]; then
  for key in LLM_BINDING_API_KEY NEO4J_URI NEO4J_USERNAME NEO4J_PASSWORD GPTR_MCP_URL; do
    # The raw line: env_get strips quotes, and a quoted value has no comment.
    val="$(grep -E "^${key}=" .env | head -1 | cut -d= -f2- || true)"
    case "$val" in \"* | \'*) continue ;; esac
    # Compose's rule: the comment starts at the first whitespace before a #.
    pre="${val%%[[:space:]]#*}"
    pre="${pre%"${pre##*[![:space:]]}"}"
    if [[ "$pre" != "$val" && -n "$pre" ]]; then
      upsert_env .env "$key" "$pre"
      echo "removed the inline comment from ${key} in .env" >&2
    fi
  done
fi

# bitwarden_signin was a tmpfs with uid= and gid=, which Podman drops, leaving
# the signer unable to create its socket. Compose keeps an existing volume's
# options, so a volume made before v0.3.17 must be removed by hand.
if enabled BITWARDEN_ENABLED && command -v docker > /dev/null 2>&1; then
  project="$(docker compose config 2> /dev/null | sed -n 's/^name: *//p' | head -1 || true)"
  if [[ -n "$project" ]]; then
    vols="$(docker volume ls -q \
      --filter "label=com.docker.compose.project=${project}" \
      --filter "label=com.docker.compose.volume=bitwarden_signin" 2> /dev/null || true)"
    for vol in $vols; do
      opts="$(docker volume inspect --format '{{ index .Options "o" }}' "$vol" 2> /dev/null || true)"
      case "$opts" in
        *uid=*)
          needs_user bitwarden-signin-volume \
            "On Podman, Bitwarden sign-in needs the ${vol} volume made again with the new options: docker compose rm -sf bitwarden-signin bitwarden-broker, then docker volume rm ${vol}, then make up. It only holds the signer's socket, so nothing is lost. On Docker the old volume keeps working and this can be skipped."
          ;;
      esac
    done
  fi
fi
