# LightRAG image with OpenAI-compatible response_format patch.
# Upstream LightRAG keyword/entity extraction sends response_format
# {"type":"json_object"}; some local servers (e.g. LM Studio) only accept
# "json_schema" or "text".
ARG LIGHTRAG_BASE_IMAGE=ghcr.io/hkuds/lightrag:v1.4.9.3
FROM ${LIGHTRAG_BASE_IMAGE}

USER root
RUN python - <<'PY'
from pathlib import Path

path = Path("/app/lightrag/llm/openai.py")
source = path.read_text()

helper = '''
def _normalize_openai_compat_response_format(kwargs: dict) -> None:
    """Map OpenAI json_object mode to json_schema for stricter local servers.

    Some OpenAI-compatible servers reject response_format.type=json_object
    (only json_schema|text). LightRAG keyword/entity extraction still relies on
    JSON text in message.content, so a permissive object schema preserves
    structured-output intent.
    """
    rf = kwargs.get("response_format")
    if not isinstance(rf, dict) or rf.get("type") != "json_object":
        return
    kwargs["response_format"] = {
        "type": "json_schema",
        "json_schema": {
            "name": "json_object",
            "schema": {"type": "object"},
        },
    }


'''

anchor = "def _validate_openai_response_format(response_format: Any | None) -> None:"
if "_normalize_openai_compat_response_format" in source:
    raise SystemExit("patch already applied")
if anchor not in source:
    raise SystemExit("validate helper not found; upstream openai.py changed")
source = source.replace(anchor, helper + anchor, 1)

call_anchor = "    _validate_openai_response_format(kwargs.get(\"response_format\"))"
call_repl = (
    "    _normalize_openai_compat_response_format(kwargs)\n"
    "    _validate_openai_response_format(kwargs.get(\"response_format\"))"
)
if call_anchor not in source:
    raise SystemExit("validate call site not found; upstream openai.py changed")
# Only replace the first call site in openai_complete_if_cache
source = source.replace(call_anchor, call_repl, 1)
path.write_text(source)
print("patched", path)
PY
