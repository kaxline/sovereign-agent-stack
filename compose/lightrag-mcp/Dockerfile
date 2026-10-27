FROM python:3.11-slim

ARG LIGHTRAG_MCP_VERSION=0.1.1

ENV PYTHONUNBUFFERED=1

# LightRAG v1.5 expects API key auth as X-API-Key. The pinned MCP package
# defaults to Authorization: Bearer, so patch the generated client in-place.
# mcp 2.x dropped mcp.server.fastmcp; pin 1.x for lightrag_mcp 0.1.1.
# get_documents_paginated.py points get_documents at /documents/paginated.
COPY get_documents_paginated.py patch_lightrag_mcp.py /tmp/
RUN pip install --no-cache-dir "lightrag_mcp==${LIGHTRAG_MCP_VERSION}" "mcp>=1.6.0,<2" \
    && python /tmp/patch_lightrag_mcp.py

EXPOSE 8000

ENTRYPOINT ["lightrag-mcp"]
