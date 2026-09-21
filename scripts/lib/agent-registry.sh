#!/usr/bin/env bash
# Shared agent registry + SOUL sibling-marker helpers.
# Source from repo-root scripts:  source "${ROOT}/scripts/lib/agent-registry.sh"
#
# Expects ROOT to be set to the assistant repo root.
# Uses: log, warn (optional — falls back to printf).

: "${ROOT:?ROOT must be set before sourcing agent-registry.sh}"

AGENT_REGISTRY_DIR="${ROOT}/data/hermes/agents"
AGENT_REGISTRY_FILE="${AGENT_REGISTRY_DIR}/registry.json"

_agent_reg_log() {
  # Prefer caller-defined log(); avoid colliding with /usr/bin/log on macOS.
  if declare -F log >/dev/null 2>&1; then
    local type
    type="$(type log 2>/dev/null || true)"
    if [[ "$type" == *function* ]]; then
      log "$@"
      return 0
    fi
  fi
  printf '==> %s\n' "$*"
}

_agent_reg_warn() {
  if declare -F warn >/dev/null 2>&1; then
    local type
    type="$(type warn 2>/dev/null || true)"
    if [[ "$type" == *function* ]]; then
      warn "$@"
      return 0
    fi
  fi
  printf 'warning: %s\n' "$*" >&2
}

# Upsert one agent into registry.json.
# Args: name display_name role [buzz_pubkey]
agent_registry_upsert() {
  local name="$1"
  local display_name="$2"
  local role="$3"
  local buzz_pubkey="${4:-}"

  mkdir -p "$AGENT_REGISTRY_DIR"
  NAME="$name" DISPLAY_NAME="$display_name" ROLE="$role" BUZZ_PUBKEY="$buzz_pubkey" \
    REGISTRY="$AGENT_REGISTRY_FILE" python3 - <<'PY'
import json, os, time
from pathlib import Path

path = Path(os.environ["REGISTRY"])
name = os.environ["NAME"]
display_name = os.environ["DISPLAY_NAME"]
role = os.environ["ROLE"]
buzz_pubkey = os.environ.get("BUZZ_PUBKEY", "").strip()

if path.is_file():
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError:
        data = {"agents": []}
else:
    data = {"agents": []}

if not isinstance(data, dict):
    data = {"agents": []}
agents = data.setdefault("agents", [])
if not isinstance(agents, list):
    agents = []
    data["agents"] = agents

now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
found = None
for a in agents:
    if isinstance(a, dict) and a.get("name") == name:
        found = a
        break

if found is None:
    found = {
        "name": name,
        "display_name": display_name,
        "role": role,
        "created_at": now,
        "transports": {},
    }
    agents.append(found)
else:
    found["display_name"] = display_name or found.get("display_name") or name
    if role:
        found["role"] = role

transports = found.setdefault("transports", {})
if not isinstance(transports, dict):
    transports = {}
    found["transports"] = transports
if buzz_pubkey:
    buzz = transports.setdefault("buzz", {})
    if not isinstance(buzz, dict):
        buzz = {}
        transports["buzz"] = buzz
    buzz["pubkey"] = buzz_pubkey

path.write_text(json.dumps(data, indent=2) + "\n")
PY
  _agent_reg_log "Updated agent registry: ${AGENT_REGISTRY_FILE}"
}

# Set or clear transports.buzz.pubkey for an existing agent (create entry if missing).
# Args: name buzz_pubkey
agent_registry_set_buzz_pubkey() {
  local name="$1"
  local buzz_pubkey="$2"
  mkdir -p "$AGENT_REGISTRY_DIR"
  NAME="$name" BUZZ_PUBKEY="$buzz_pubkey" REGISTRY="$AGENT_REGISTRY_FILE" python3 - <<'PY'
import json, os, time
from pathlib import Path

path = Path(os.environ["REGISTRY"])
name = os.environ["NAME"]
buzz_pubkey = os.environ["BUZZ_PUBKEY"].strip()

if path.is_file():
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError:
        data = {"agents": []}
else:
    data = {"agents": []}

agents = data.setdefault("agents", [])
found = None
for a in agents:
    if isinstance(a, dict) and a.get("name") == name:
        found = a
        break
if found is None:
    found = {
        "name": name,
        "display_name": name,
        "role": "",
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "transports": {},
    }
    agents.append(found)

transports = found.setdefault("transports", {})
buzz = transports.setdefault("buzz", {})
if buzz_pubkey:
    buzz["pubkey"] = buzz_pubkey
elif "pubkey" in buzz:
    del buzz["pubkey"]

path.write_text(json.dumps(data, indent=2) + "\n")
PY
}

# Rewrite <!-- agents:siblings --> blocks in every registered agent's SOUL.md.
agent_registry_sync_siblings() {
  if [[ ! -f "$AGENT_REGISTRY_FILE" ]]; then
    return 0
  fi
  ROOT="$ROOT" REGISTRY="$AGENT_REGISTRY_FILE" python3 - <<'PY'
import json, os, re, sys
from pathlib import Path

root = Path(os.environ["ROOT"])
reg_path = Path(os.environ["REGISTRY"])
try:
    data = json.loads(reg_path.read_text())
except (json.JSONDecodeError, OSError):
    sys.exit(0)

agents = [a for a in data.get("agents", []) if isinstance(a, dict) and a.get("name")]
if not agents:
    sys.exit(0)

start = "<!-- agents:siblings -->"
end = "<!-- /agents:siblings -->"
pattern = re.compile(
    re.escape(start) + r".*?" + re.escape(end),
    re.DOTALL,
)

for agent in agents:
    name = agent["name"]
    soul = root / "data" / "hermes" / "profiles" / name / "SOUL.md"
    if not soul.is_file():
        print(f"warning: missing SOUL for registry agent '{name}'", file=sys.stderr)
        continue

    others = [a for a in agents if a.get("name") != name]
    if others:
        lines = []
        for a in others:
            dn = a.get("display_name") or a["name"]
            role = (a.get("role") or "").strip()
            if role:
                lines.append(f"- **{dn}** (`{a['name']}`): {role}")
            else:
                lines.append(f"- **{dn}** (`{a['name']}`)")
        body = "\n".join(lines)
    else:
        body = "(none yet — other agents created with `make agent-create` will appear here)"

    block = f"{start}\n{body}\n{end}"
    text = soul.read_text()
    if pattern.search(text):
        text = pattern.sub(block, text, count=1)
    else:
        print(
            f"warning: SOUL.md for '{name}' missing sibling markers — appending block",
            file=sys.stderr,
        )
        if not text.endswith("\n"):
            text += "\n"
        text += f"\n## Sibling agents\n\n{block}\n"
    soul.write_text(text)
PY
  _agent_reg_log "Synced sibling markers in agent SOUL.md files"
}

# Return 0 if name is listed in the agent registry.
agent_registry_has() {
  local name="$1"
  [[ -f "$AGENT_REGISTRY_FILE" ]] || return 1
  NAME="$name" REGISTRY="$AGENT_REGISTRY_FILE" python3 - <<'PY'
import json, os, sys
from pathlib import Path
path = Path(os.environ["REGISTRY"])
name = os.environ["NAME"]
try:
    data = json.loads(path.read_text())
except Exception:
    sys.exit(1)
for a in data.get("agents", []):
    if isinstance(a, dict) and a.get("name") == name:
        sys.exit(0)
sys.exit(1)
PY
}
