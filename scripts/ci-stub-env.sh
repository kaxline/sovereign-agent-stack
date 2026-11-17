#!/usr/bin/env bash
# Stubs .env, the local overlays and the worker's SSH key on a CI runner, so
# `docker compose config` and the tests that run it have every required value.
# For GitHub's Ubuntu runners only: it uses GNU sed and overwrites .env.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

cp .env.example .env
# Satisfy required interpolations without running full setup
sed -i 's/^NEO4J_PASSWORD=.*/NEO4J_PASSWORD=ci-neo4j-pass/' .env
sed -i 's/^POSTGRES_PASSWORD=.*/POSTGRES_PASSWORD=ci-postgres-pass/' .env
sed -i 's/^OPENCODE_SERVER_PASSWORD=.*/OPENCODE_SERVER_PASSWORD=ci-opencode-pass/' .env
sed -i 's/^LIGHTRAG_API_KEY=.*/LIGHTRAG_API_KEY=lr_ci_key/' .env
sed -i 's/^N8N_ENCRYPTION_KEY=.*/N8N_ENCRYPTION_KEY=0123456789abcdef/' .env
sed -i 's/^N8N_USER_MANAGEMENT_JWT_SECRET=.*/N8N_USER_MANAGEMENT_JWT_SECRET=0123456789abcdef/' .env
sed -i 's/^HERMES_DASHBOARD_PASSWORD=.*/HERMES_DASHBOARD_PASSWORD=ci-dashboard-pass/' .env
sed -i 's/^HERMES_DASHBOARD_AUTH_SECRET=.*/HERMES_DASHBOARD_AUTH_SECRET=0123456789abcdef0123456789abcdef/' .env
sed -i 's/^SEARXNG_SECRET=.*/SEARXNG_SECRET=ci-searxng-secret/' .env
sed -i 's/^LLM_PROXY_ADMIN_TOKEN=.*/LLM_PROXY_ADMIN_TOKEN=ci-llm-proxy-admin-token/' .env
sed -i 's|^OPENCODE_WORKSPACE_HOST=.*|OPENCODE_WORKSPACE_HOST=/tmp/opencode-workspace|' .env
mkdir -p /tmp/opencode-workspace
# .env.example turns OpenCode on; CI keeps it off. Compose still interpolates these.
sed -i 's/^OPENCODE_MCP_ENABLED=.*/OPENCODE_MCP_ENABLED=0/' .env
grep -q '^CODING_EXTRA_ROOTS=' .env || echo 'CODING_EXTRA_ROOTS=' >> .env
grep -q '^CONTEXT_EXTRA_ROOTS=' .env || echo 'CONTEXT_EXTRA_ROOTS=' >> .env
cp searxng/settings.yml searxng/settings.local.yml
cp opencode/opencode.json opencode/opencode.local.json
cp compose/hermes/api-server.env.example compose/hermes/api-server.env
cp compose/hermes/browser.env.example compose/hermes/browser.env
echo 'API_SERVER_KEY=ci-api-key-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa' >> compose/hermes/api-server.env
# browser.env example already has a placeholder key line — ensure set
sed -i 's/^API_SERVER_KEY=.*/API_SERVER_KEY=ci-browser-key-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb/' compose/hermes/browser.env
mkdir -p data/hermes/memories data/hermes/profiles/api-server data/hermes/profiles/browser \
  data/voice data/projects data/memory data/inputs/my-kb data/rag_storage/my-kb
mkdir -p compose/hermes/worker/ssh
ssh-keygen -t ed25519 -f compose/hermes/worker/ssh/id_ed25519 -N "" -C ci-hermes-worker
