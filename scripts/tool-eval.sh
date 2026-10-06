#!/usr/bin/env bash
# Tool-calling eval harness against the Hermes browser profile (port 8644).
#
# Usage:
#   ./scripts/tool-eval.sh reset
#   ./scripts/tool-eval.sh run [CASE|all]
#   ./scripts/tool-eval.sh score
#   ./scripts/tool-eval.sh list
#
# Traces: data/hermes/eval/traces.jsonl
# Last scripted run: data/hermes/eval/last-run.json
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

CASES_FILE="${ROOT}/eval/tool-calling/cases.yaml"
CASES_LOADER="${ROOT}/scripts/tool_eval_cases.py"
EVAL_DIR="${ROOT}/data/hermes/eval"
TRACES="${EVAL_DIR}/traces.jsonl"
LAST_RUN="${EVAL_DIR}/last-run.json"
BROWSER_ENV="${ROOT}/compose/hermes/browser.env"
BROWSER_PORT=8644
# Final answers can be huge (one web-fact run took ~36m). Override with TOOL_EVAL_TIMEOUT.
CURL_MAX_TIME="${TOOL_EVAL_TIMEOUT:-2400}"
case "${CURL_MAX_TIME}" in
  ''|*[!0-9]*) CURL_MAX_TIME=2400 ;;
esac

usage() {
  cat <<'EOF'
Usage: ./scripts/tool-eval.sh <reset|run|score|list> [CASE|all]

  reset   Truncate data/hermes/eval/traces.jsonl and last-run.json
  run     POST prompts to browser gateway (CASE=id or all)
          curl --max-time is 2400s (override with TOOL_EVAL_TIMEOUT)
  score   Score traces (+ last-run) against cases.yaml
          Cases with no JSONL and no last-run entry are SKIP (not FAIL)
  list    Print case ids and gates

WebUI path: paste the prompt from cases.yaml into a new chat at
http://localhost:8787 (keep the [eval:…] tag), then run score.
EOF
}

need_cases() {
  if [[ ! -f "$CASES_FILE" ]]; then
    echo "Missing ${CASES_FILE}" >&2
    exit 1
  fi
  if [[ ! -f "$CASES_LOADER" ]]; then
    echo "Missing ${CASES_LOADER}" >&2
    exit 1
  fi
}

ensure_eval_dir() {
  mkdir -p "$EVAL_DIR"
}

api_key() {
  if [[ ! -f "$BROWSER_ENV" ]]; then
    echo "Missing ${BROWSER_ENV} — copy browser.env.example and set API_SERVER_KEY" >&2
    exit 1
  fi
  local key
  key="$(grep -E '^API_SERVER_KEY=' "$BROWSER_ENV" | head -1 | cut -d= -f2- | tr -d '"' | tr -d "'")"
  if [[ -z "$key" || "$key" == change-me* ]]; then
    echo "API_SERVER_KEY in browser.env is unset or still a placeholder" >&2
    exit 1
  fi
  printf '%s' "$key"
}

profiles_csv() {
  local val=""
  if [[ -f .env ]]; then
    val="$(grep -E '^COMPOSE_PROFILES=' .env 2>/dev/null | head -1 | cut -d= -f2- | tr -d '"' | tr -d "'" || true)"
  fi
  echo "${val:-core}"
}

has_profile() {
  local want="$1"
  local p
  IFS=',' read -r -a arr <<< "$(profiles_csv)"
  for p in "${arr[@]}"; do
    p="$(echo "$p" | tr -d '[:space:]')"
    [[ "$p" == "$want" ]] && return 0
  done
  return 1
}

gate_ok() {
  local gate="$1"
  case "$gate" in
    core|"") return 0 ;;
    rag) has_profile rag ;;
    coding) has_profile coding ;;
    *) return 1 ;;
  esac
}

cmd_reset() {
  ensure_eval_dir
  : >"$TRACES"
  echo '[]' >"$LAST_RUN"
  echo "Reset ${TRACES} and ${LAST_RUN}"
}

cmd_list() {
  need_cases
  python3 - "$CASES_LOADER" "$CASES_FILE" <<'PY'
import sys
from pathlib import Path
sys.path.insert(0, str(Path(sys.argv[1]).parent))
from tool_eval_cases import load_cases
data = load_cases(sys.argv[2])
for c in data.get("cases") or []:
    print(f"{c.get('id')}\tgate={c.get('gate', 'core')}")
PY
}

select_cases() {
  local want="${1:-all}"
  python3 - "$CASES_LOADER" "$CASES_FILE" "$want" <<'PY'
import sys
from pathlib import Path
sys.path.insert(0, str(Path(sys.argv[1]).parent))
from tool_eval_cases import load_cases
data = load_cases(sys.argv[2])
want = sys.argv[3]
for c in data.get("cases") or []:
    cid = c.get("id") or ""
    if want != "all" and cid != want:
        continue
    print(f"{cid}\t{c.get('gate', 'core')}")
PY
}

case_prompt() {
  local cid="$1"
  python3 - "$CASES_LOADER" "$CASES_FILE" "$cid" <<'PY'
import sys
from pathlib import Path
sys.path.insert(0, str(Path(sys.argv[1]).parent))
from tool_eval_cases import load_cases
data = load_cases(sys.argv[2])
cid = sys.argv[3]
for c in data.get("cases") or []:
    if c.get("id") == cid:
        print((c.get("prompt") or "").rstrip())
        raise SystemExit(0)
raise SystemExit(2)
PY
}

case_field() {
  local cid="$1"
  local field="$2"
  python3 - "$CASES_LOADER" "$CASES_FILE" "$cid" "$field" <<'PY'
import sys
from pathlib import Path
sys.path.insert(0, str(Path(sys.argv[1]).parent))
from tool_eval_cases import load_cases
data = load_cases(sys.argv[2])
cid, field = sys.argv[3], sys.argv[4]
for c in data.get("cases") or []:
    if c.get("id") == cid:
        val = c.get(field)
        if val is None:
            raise SystemExit(0)
        print(val)
        raise SystemExit(0)
raise SystemExit(0)
PY
}

hermes_running() {
  docker compose ps --status running --services 2>/dev/null | grep -qx hermes
}

run_one() {
  local cid="$1"
  local prompt
  prompt="$(case_prompt "$cid")" || {
    echo "Unknown case: ${cid}" >&2
    return 1
  }
  local key
  key="$(api_key)"

  if ! hermes_running; then
    echo "hermes container is not running — make up first" >&2
    return 1
  fi

  local expect_file
  expect_file="$(case_field "$cid" expect_file)"
  if [[ -n "$expect_file" ]]; then
    docker compose exec -T hermes rm -f "$expect_file"
  fi

  echo "=== run ${cid} ==="
  local payload_b64
  payload_b64="$(PROMPT="$prompt" python3 - <<'PY'
import base64, json, os
body = {
    "model": "browser",
    "messages": [{"role": "user", "content": os.environ["PROMPT"]}],
    "stream": False,
}
print(base64.b64encode(json.dumps(body).encode()).decode())
PY
)"

  local start=$SECONDS
  local result
  result="$(docker compose exec -T hermes sh -c "
    echo '${payload_b64}' | base64 -d > /tmp/tool-eval-payload.json
    curl -sS -w '\\n%{http_code}' --max-time ${CURL_MAX_TIME} \
      -H 'Authorization: Bearer ${key}' \
      -H 'Content-Type: application/json' \
      -d @/tmp/tool-eval-payload.json \
      http://127.0.0.1:${BROWSER_PORT}/v1/chat/completions
  ")" || {
    echo "curl failed for ${cid}" >&2
    return 1
  }
  local elapsed=$((SECONDS - start))

  local http_code body
  http_code="$(printf '%s' "$result" | tail -n1)"
  body="$(printf '%s' "$result" | sed '$d')"

  python3 - "$LAST_RUN" "$cid" "$http_code" "$body" "$elapsed" <<'PY'
import json, sys
from pathlib import Path

path = Path(sys.argv[1])
cid, code, body = sys.argv[2], sys.argv[3], sys.argv[4]
elapsed = int(sys.argv[5]) if str(sys.argv[5]).isdigit() else None
try:
    runs = json.loads(path.read_text()) if path.is_file() else []
except Exception:
    runs = []
if not isinstance(runs, list):
    runs = []

tools = []
content_prefix = ""
content_len = 0
error = None
try:
    data = json.loads(body) if body.strip() else {}
except Exception as e:
    data = {}
    error = f"invalid json: {e}"

if isinstance(data, dict):
    if data.get("error"):
        error = str(data.get("error"))
    choices = data.get("choices") or []
    if choices:
        msg = (choices[0] or {}).get("message") or {}
        content = msg.get("content") or ""
        if isinstance(content, str):
            content_len = len(content)
            content_prefix = content.strip()[:200]
        for tc in msg.get("tool_calls") or []:
            fn = (tc.get("function") or {}) if isinstance(tc, dict) else {}
            name = fn.get("name") or tc.get("name")
            if name:
                tools.append(name)

entry = {
    "id": cid,
    "http_status": int(code) if str(code).isdigit() else code,
    "tools": tools,
    "content_prefix": content_prefix,
    "content_len": content_len,
    "duration_s": elapsed,
    "error": error,
}
runs = [r for r in runs if r.get("id") != cid]
runs.append(entry)
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(json.dumps(runs, indent=2) + "\n")
print(f"  http={code} tools={tools or '[]'} chars={content_len} duration_s={elapsed} error={error or '-'}")
PY
}

cmd_run() {
  need_cases
  ensure_eval_dir
  local want="${1:-all}"
  local cid gate
  local ran=0
  while IFS=$'\t' read -r cid gate; do
    [[ -z "$cid" ]] && continue
    if ! gate_ok "$gate"; then
      echo "=== skip ${cid} (gate=${gate} not in COMPOSE_PROFILES) ==="
      continue
    fi
    if [[ "$cid" == "kb-query" ]]; then
      # Temporary LightRAG insert; always remove, even if the case fails.
      "${ROOT}/scripts/kb-eval-fixture.sh" install || exit 1
      cleanup_kb_fixture() { "${ROOT}/scripts/kb-eval-fixture.sh" remove || true; }
      trap cleanup_kb_fixture EXIT
      local kb_rc=0
      run_one "$cid" || kb_rc=$?
      cleanup_kb_fixture
      trap - EXIT
      [[ "$kb_rc" -eq 0 ]] || exit "$kb_rc"
    else
      run_one "$cid"
    fi
    ran=$((ran + 1))
  done < <(select_cases "$want")
  if [[ "$ran" -eq 0 ]]; then
    echo "No cases ran (unknown id or all gated off)." >&2
    exit 1
  fi
  echo "Wrote ${LAST_RUN}. Score with: make tool-eval-score"
}

cmd_score() {
  need_cases
  ensure_eval_dir
  python3 - "$CASES_LOADER" "$CASES_FILE" "$TRACES" "$LAST_RUN" "$(profiles_csv)" <<'PY'
import json, sys
from pathlib import Path

sys.path.insert(0, str(Path(sys.argv[1]).parent))
from tool_eval_cases import load_cases, session_outcome

cases_loader, cases_path, traces_path, last_path, profiles = sys.argv[1:6]
profile_set = {p.strip() for p in profiles.split(",") if p.strip()}
data = load_cases(cases_path)
cases = data.get("cases") or []

traces = []
tp = Path(traces_path)
if tp.is_file() and tp.stat().st_size:
    for line in tp.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            traces.append(json.loads(line))
        except json.JSONDecodeError:
            continue

last_runs = []
lp = Path(last_path)
if lp.is_file() and lp.stat().st_size:
    try:
        last_runs = json.loads(lp.read_text()) or []
    except Exception:
        last_runs = []
last_by_id = {r.get("id"): r for r in last_runs if isinstance(r, dict)}

def tools_for(cid: str):
    names = []
    outcomes = []
    recovered = False
    rewritten = False
    bridge = []
    content_lens = []
    timestamps = []
    for ev in traces:
        if ev.get("eval_id") != cid:
            continue
        if ev.get("kind") == "web_rewrite":
            rewritten = True
            to = ev.get("to")
            if to:
                names.append(to)
            continue
        if ev.get("kind") != "assistant_turn":
            continue
        names.extend(ev.get("tool_calls") or [])
        outcomes.append(ev.get("outcome"))
        recovered = recovered or bool(ev.get("recovered"))
        bridge.extend(ev.get("bridge") or [])
        raw_len = ev.get("content_len")
        if raw_len is not None:
            try:
                content_lens.append(int(raw_len))
            except (TypeError, ValueError):
                pass
        raw_ts = ev.get("ts")
        if raw_ts is not None:
            try:
                timestamps.append(int(raw_ts))
            except (TypeError, ValueError):
                pass
    lr = last_by_id.get(cid) or {}
    if not names and lr.get("tools"):
        names.extend(lr["tools"])
    return names, outcomes, recovered, rewritten, bridge, lr, content_lens, timestamps

def as_num(val):
    if val is None or val is False:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None

def expect_file_status(path: str, expected: str):
    """ok, missing, or unverified. One trailing newline still matches."""
    import subprocess
    try:
        proc = subprocess.run(
            ["docker", "compose", "exec", "-T", "hermes", "cat", path],
            capture_output=True,
            timeout=30,
        )
    except Exception:
        return "unverified"
    err = proc.stderr.decode("utf-8", errors="replace")
    out = proc.stdout.decode("utf-8", errors="replace")
    if proc.returncode != 0:
        if "No such file" in err or "No such file" in out:
            return "missing"
        return "unverified"
    norm = out.replace("\r\n", "\n")
    if norm.endswith("\n"):
        norm = norm[:-1]
    return "ok" if norm == expected else "missing"

def name_matches(haystack, needle: str) -> bool:
    n = needle.lower()
    for h in haystack:
        hl = str(h).lower()
        if hl == n or n in hl or hl.endswith(n) or hl.endswith("__" + n):
            return True
    return False

def out_of_order(names, target: str, after) -> bool:
    """True when target ran before any of the `after` tools had run."""
    for i, n in enumerate(names):
        if name_matches([n], target):
            return not any(name_matches(names[:i], a) for a in after)
    return False

def gate_ok(gate: str) -> bool:
    gate = gate or "core"
    if gate == "core":
        return True
    return gate in profile_set

print(f"{'ID':<18} {'RESULT':<8} {'OUTCOME':<24} TOOLS")
print("-" * 80)
passed = failed = skipped = 0
for c in cases:
    cid = c.get("id") or "?"
    gate = c.get("gate") or "core"
    if not gate_ok(gate):
        print(f"{cid:<18} {'SKIP':<8} {'gate='+gate:<24} -")
        skipped += 1
        continue
    names, outcomes, recovered, rewritten, bridge, lr, content_lens, timestamps = tools_for(cid)
    uniq = []
    for n in names:
        if n not in uniq:
            uniq.append(n)
    outcome = session_outcome(
        uniq, outcomes, recovered=recovered, has_last_run=bool(lr)
    )

    if not uniq and not outcomes and not lr:
        print(f"{cid:<18} {'SKIP':<8} {'not_run':<24} -")
        skipped += 1
        continue

    max_content = max(content_lens) if content_lens else 0
    if not max_content and lr.get("content_len"):
        try:
            max_content = int(lr["content_len"])
        except (TypeError, ValueError):
            pass
    duration_s = None
    if len(timestamps) >= 2:
        duration_s = (max(timestamps) - min(timestamps)) / 1000.0
    elif lr.get("duration_s") is not None:
        duration_s = as_num(lr.get("duration_s"))

    result = "FAIL"
    reason = ""
    if c.get("pass_none"):
        if not uniq:
            result = "PASS"
        else:
            reason = "unexpected_tools"
    else:
        pass_any = c.get("pass_any") or []
        hit = any(name_matches(uniq, p) for p in pass_any) if pass_any else bool(uniq)
        fail_tools = c.get("fail_if_tools") or []
        bad_tool = any(name_matches(uniq, f) for f in fail_tools)
        fail_if = c.get("fail_if") or []
        bad_outcome = any(
            (isinstance(f, dict) and f.get("outcome") == "text_only" and outcome == "text_only")
            for f in fail_if
        )
        cap_chars = as_num(c.get("max_chars"))
        cap_seconds = as_num(c.get("max_seconds"))
        too_long = cap_chars is not None and max_content > cap_chars
        too_slow = cap_seconds is not None and duration_s is not None and duration_s > cap_seconds
        ordered_tool = c.get("ordered_tool")
        ordered_after = c.get("ordered_after") or []
        expect_path = c.get("expect_file") or ""
        expect_body = c.get("expect_content")
        file_status = None
        if expect_path:
            file_status = expect_file_status(
                str(expect_path),
                "" if expect_body is None else str(expect_body),
            )
        if bad_tool:
            result = "FAIL"
            reason = "wrong_tool"
        elif bad_outcome or (not uniq and outcome in ("text_only", "no_trace")):
            result = "FAIL"
            reason = "text_only" if outcome == "text_only" else "no_trace"
        elif too_long:
            result = "FAIL"
            reason = "too_long"
        elif too_slow:
            result = "FAIL"
            reason = "too_slow"
        elif ordered_tool and out_of_order(names, str(ordered_tool), ordered_after):
            result = "FAIL"
            reason = "out_of_order"
        elif file_status == "unverified":
            result = "FAIL"
            reason = "file_unverified"
        elif hit and file_status == "missing":
            result = "FAIL"
            reason = "file_missing"
        elif hit:
            result = "PASS"
            if recovered:
                reason = "recovered"
            elif rewritten:
                reason = "rewritten"
        else:
            result = "FAIL"
            if bridge and not hit:
                reason = "bridge_no_dispatch"
            elif uniq:
                reason = "missed_expected"
            else:
                reason = "text_only" if outcome == "text_only" else "no_tools"

    if result == "PASS":
        passed += 1
    else:
        failed += 1
    tools_s = ",".join(uniq) if uniq else "-"
    extra = f" ({reason})" if reason else ""
    print(f"{cid:<18} {result:<8} {outcome:<24} {tools_s}{extra}")

print("-" * 80)
print(f"passed={passed} failed={failed} skipped={skipped}")
print(f"traces={traces_path} last-run={last_path}")
raise SystemExit(0 if failed == 0 else 1)
PY
}

main() {
  local cmd="${1:-}"
  shift || true
  case "$cmd" in
    reset) cmd_reset ;;
    list) cmd_list ;;
    run) cmd_run "${1:-all}" ;;
    score) cmd_score ;;
    -h|--help|help|"") usage; [[ -n "$cmd" ]] || exit 1 ;;
    *) echo "Unknown command: $cmd" >&2; usage; exit 1 ;;
  esac
}

main "$@"
