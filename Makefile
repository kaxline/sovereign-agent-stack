.PHONY: setup ensure-local up down logs ps restart clean doctor hermes-upgrade \
	corpus-create corpus-use corpus-list corpus-ingest corpus-destroy

setup:
	./scripts/setup.sh

ensure-local:
	@test -f searxng/settings.local.yml || cp searxng/settings.yml searxng/settings.local.yml
	@test -f opencode/opencode.local.json || cp opencode/opencode.json opencode/opencode.local.json
	@test -f compose/hermes/api-server.env || (cp compose/hermes/api-server.env.example compose/hermes/api-server.env && echo "Created compose/hermes/api-server.env — set API_SERVER_KEY")
	@test -f compose/hermes/browser.env || (cp compose/hermes/browser.env.example compose/hermes/browser.env && echo "Created compose/hermes/browser.env — set API_SERVER_KEY")

doctor:
	./scripts/doctor.sh

up: ensure-local
	docker compose up -d

down:
	docker compose down

logs:
	docker compose logs -f hermes hermes-webui searxng

ps:
	docker compose ps

restart:
	docker compose restart hermes hermes-webui

# Corpus lifecycle (requires rag profile). One hot WORKSPACE at a time.
#   make corpus-create SLUG=demo-a
#   make corpus-use SLUG=demo-a
#   make corpus-ingest
#   make corpus-list
#   make corpus-destroy SLUG=demo-a
corpus-create:
	@test -n "$(SLUG)" || (echo "Usage: make corpus-create SLUG=<slug>"; exit 1)
	./scripts/corpus.sh create "$(SLUG)"

corpus-use:
	@test -n "$(SLUG)" || (echo "Usage: make corpus-use SLUG=<slug>"; exit 1)
	./scripts/corpus.sh use "$(SLUG)"

corpus-list:
	./scripts/corpus.sh list

corpus-ingest:
	./scripts/corpus.sh ingest $(SLUG)

corpus-destroy:
	@test -n "$(SLUG)" || (echo "Usage: make corpus-destroy SLUG=<slug>"; exit 1)
	./scripts/corpus.sh destroy "$(SLUG)"

# Upgrade Hermes and its WebUI together. The WebUI reads the agent's on-disk
# state layout and is only tested against a matching agent, so bumping one alone
# is the failure mode this target exists to prevent — pass both tags:
#   make hermes-upgrade AGENT=v2026.9.1 WEBUI=0.53.12
# Re-runs both profile bootstraps afterwards, because a new agent may add config
# keys the running profiles do not have yet.
hermes-upgrade:
	@if [ -z "$(AGENT)" ] || [ -z "$(WEBUI)" ]; then \
		echo "Usage: make hermes-upgrade AGENT=<agent-tag> WEBUI=<webui-tag>"; \
		echo "  agent tags: https://hub.docker.com/r/nousresearch/hermes-agent/tags"; \
		echo "  webui tags: https://github.com/nesquena/hermes-webui/releases (drop the leading v)"; \
		echo; \
		echo "Currently pinned:"; \
		grep -E '^HERMES_(AGENT|WEBUI)_IMAGE_TAG=' .env; \
		exit 1; \
	fi
	@python3 -c 'import re,sys,pathlib; p=pathlib.Path(".env"); t=p.read_text(); \
	    [t := re.sub(r"(?m)^%s=.*$$" % k, "%s=%s" % (k, v), t) for k, v in \
	     (("HERMES_AGENT_IMAGE_TAG", "$(AGENT)"), ("HERMES_WEBUI_IMAGE_TAG", "$(WEBUI)"))]; \
	    p.write_text(t)'
	@grep -E '^HERMES_(AGENT|WEBUI)_IMAGE_TAG=' .env
	docker compose pull hermes hermes-webui
	docker compose run --rm hermes-api-bootstrap
	docker compose run --rm hermes-browser-bootstrap
	docker compose up -d hermes hermes-webui
	@echo
	@echo "Upgraded. Now run the verification block in docs/hermes-webui.md —"
	@echo "the tool filters and skills.external_dirs are what silently regress."

clean:
	@echo "WARNING: This removes all Docker volumes and local data/ for this project."
	@read -r -p "Type 'yes' to continue: " confirm; \
	if [ "$$confirm" = yes ]; then \
		docker compose down -v; \
		rm -rf data/; \
		echo "Cleaned."; \
	else \
		echo "Aborted."; \
	fi
