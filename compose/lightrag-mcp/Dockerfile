FROM python:3.11-slim

ARG LIGHTRAG_MCP_VERSION=0.1.1

ENV PYTHONUNBUFFERED=1

# LightRAG v1.5 expects API key auth as X-API-Key. The pinned MCP package
# defaults to Authorization: Bearer, so patch the generated client in-place.
# mcp 2.x dropped mcp.server.fastmcp; pin 1.x for lightrag_mcp 0.1.1.
RUN pip install --no-cache-dir "lightrag_mcp==${LIGHTRAG_MCP_VERSION}" "mcp>=1.6.0,<2" \
    && python - <<'PY'
from pathlib import Path

client_path = Path("/usr/local/lib/python3.11/site-packages/lightrag_mcp/lightrag_client.py")
source = client_path.read_text()
source = source.replace(
    "AuthenticatedClient(base_url=base_url, token=api_key, verify_ssl=False)",
    "AuthenticatedClient(base_url=base_url, token=api_key, prefix=\"\", auth_header_name=\"X-API-Key\", verify_ssl=False)",
)
client_path.write_text(source)
PY

EXPOSE 8000

ENTRYPOINT ["lightrag-mcp"]
