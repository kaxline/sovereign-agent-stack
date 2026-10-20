

# --- Appended by compose/lightrag-mcp/Dockerfile ---------------------------
# LightRAG v1.5 dropped GET /documents (405, only DELETE is allowed). Rebuild
# the catalog the get_documents tool returned before, {"statuses": {status:
# [doc, ...]}}, from POST /documents/paginated. Documents are plain dicts so
# statuses the generated DocStatus enum lacks (parsing, analyzing) still parse.
# This redefinition replaces the generated asyncio() above; lightrag_client
# imports it after the module has loaded.
_PAGE_SIZE = 200  # LightRAG's maximum


async def asyncio(
    *,
    client: AuthenticatedClient,
    api_key_header_value: Union[None, Unset, str] = UNSET,
) -> dict[str, Any]:
    statuses: dict[str, list[dict[str, Any]]] = {}
    page = 1
    while True:
        response = await client.get_async_httpx_client().request(
            method="post",
            url="/documents/paginated",
            json={
                "page": page,
                "page_size": _PAGE_SIZE,
                "sort_field": "created_at",
                "sort_direction": "asc",
            },
        )
        if response.status_code != 200:
            raise errors.UnexpectedStatus(response.status_code, response.content)
        body = response.json()
        for doc in body.get("documents", []):
            doc = dict(doc)
            # Pre-1.5 servers named this field "error".
            if doc.get("error_msg") is not None:
                doc.setdefault("error", doc["error_msg"])
            statuses.setdefault(str(doc.get("status")), []).append(doc)
        pagination = body.get("pagination") or {}
        if not pagination.get("has_next") or page >= pagination.get("total_pages", page):
            break
        page += 1
    return {"statuses": statuses}
