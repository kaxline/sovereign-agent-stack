"""Patch the installed lightrag_mcp client for LightRAG v1.5. Run once at build."""

from pathlib import Path

client_path = Path("/usr/local/lib/python3.11/site-packages/lightrag_mcp/lightrag_client.py")
source = client_path.read_text()
source = source.replace(
    "AuthenticatedClient(base_url=base_url, token=api_key, verify_ssl=False)",
    "AuthenticatedClient(base_url=base_url, token=api_key, prefix=\"\", auth_header_name=\"X-API-Key\", verify_ssl=False)",
)
client_path.write_text(source)

docs_path = client_path.parent / "client/light_rag_server_api_client/api/documents/documents_documents_get.py"
docs_source = docs_path.read_text()
assert '"url": "/documents",' in docs_source, "documents_documents_get.py changed upstream"
docs_path.write_text(docs_source + Path("/tmp/get_documents_paginated.py").read_text())
