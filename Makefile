.PHONY: setup ensure-local up down logs ps restart clean doctor hermes-upgrade \
	bootstrap-buzz buzz-cli-install hermes-buzz-employee \
	buzz-relay-start buzz-relay-stop buzz-relay-status buzz-relay-logs buzz-relay-restart \
	buzz-admin \
	corpus-create corpus-use corpus-list corpus-ingest corpus-destroy \
	project-init project-index project-index-check model-use \
	data-dir-show data-dir-set data-dir-migrate

# Resolve ASSISTANT_DATA_ROOT from .env (default ./data) to an absolute path.
define resolve_data_root
ROOT_DIR=$$(pwd); \
DR=$$(grep -E '^ASSISTANT_DATA_ROOT=' .env 2>/dev/null | head -1 | cut -d= -f2- | tr -d '"' | tr -d "'"); \
DR=$${DR:-./data}; \
case "$$DR" in \
  "~") DR="$$HOME" ;; \
  "~/"*) DR="$$HOME/$${DR#~/}" ;; \
esac; \
case "$$DR" in \
  /*) DATA_ROOT="$$DR" ;; \
  *) DATA_ROOT="$$ROOT_DIR/$${DR#./}" ;; \
esac
endef

setup:
	./scripts/setup.sh

ensure-local:
	@test -f searxng/settings.local.yml || cp searxng/settings.yml searxng/settings.local.yml
	@test -f opencode/opencode.local.json || cp opencode/opencode.json opencode/opencode.local.json
	@test -f compose/hermes/api-server.env || (cp compose/hermes/api-server.env.example compose/hermes/api-server.env && echo "Created compose/hermes/api-server.env — set API_SERVER_KEY")
	@test -f compose/hermes/browser.env || (cp compose/hermes/browser.env.example compose/hermes/browser.env && echo "Created compose/hermes/browser.env — set API_SERVER_KEY")
	@mkdir -p compose/caldav-mcp/accounts
	@test -f compose/caldav-mcp/accounts/personal.env || (cp compose/caldav-mcp/account.env.example compose/caldav-mcp/accounts/personal.env && echo "Created compose/caldav-mcp/accounts/personal.env — set CALDAV_* credentials for calendar profile")
	@./scripts/sync-signal-profile.sh
	@./scripts/sync-buzz-profile.sh

doctor:
	./scripts/doctor.sh

# Clone/setup local Buzz checkout, wire .env, start relay, install Hermes buzz CLI.
#   make bootstrap-buzz
bootstrap-buzz:
	./scripts/bootstrap-buzz.sh

# Install headless Buzz CLI into data/hermes/.local/bin (gitignored volume).
buzz-cli-install:
	./scripts/install-buzz-cli.sh

# Create a local Buzz employee Hermes profile (data/hermes only — not committed).
#   make hermes-buzz-employee PROFILE=software-engineer DISPLAY_NAME="Software Engineer"
hermes-buzz-employee:
	@test -n "$(PROFILE)" || (echo "Usage: make hermes-buzz-employee PROFILE=<slug> [DISPLAY_NAME=\"Name\"]"; exit 1)
	./scripts/hermes-buzz-employee.sh "$(PROFILE)" "$(DISPLAY_NAME)"

# Local Buzz community relay (host-side `just relay` from BUZZ_LOCAL_DIR_PATH).
# Long-running; daemonized under data/hermes/.cache/buzz-relay/. See docs/buzz.md.
buzz-relay-start:
	./scripts/buzz-relay.sh start

buzz-relay-stop:
	./scripts/buzz-relay.sh stop

buzz-relay-restart:
	./scripts/buzz-relay.sh restart

buzz-relay-status:
	./scripts/buzz-relay.sh status

buzz-relay-logs:
	./scripts/buzz-relay.sh logs

# Operator CLI from the Buzz checkout (BUZZ_LOCAL_DIR_PATH).
# Prefer positional args (Make needs `--` before flags like --pubkey):
#   make buzz-admin generate-key
#   make buzz-admin -- add-member --pubkey npub1…
# ARGS= still works: make buzz-admin ARGS='list-members'
ifeq ($(firstword $(MAKECMDGOALS)),buzz-admin)
  BUZZ_ADMIN_POS := $(wordlist 2,$(words $(MAKECMDGOALS)),$(MAKECMDGOALS))
  ifneq ($(BUZZ_ADMIN_POS),)
    $(eval $(BUZZ_ADMIN_POS):;@:)
  endif
endif

buzz-admin:
	@if [ -n "$(BUZZ_ADMIN_POS)" ]; then \
		./scripts/buzz-admin.sh $(BUZZ_ADMIN_POS); \
	else \
		./scripts/buzz-admin.sh $(ARGS); \
	fi

# User content root (projects, voice, memory, inputs, rag_storage, corpora).
# Hermes state stays at ./data/hermes. See docs/data-dir.md.
#   make data-dir-show
#   make data-dir-set DIR=~/AssistantData
#   make data-dir-migrate DIR=~/AssistantData
#   make data-dir-migrate FROM=./data DIR=~/AssistantData REMOVE_SOURCE=1
data-dir-show:
	./scripts/data-dir.sh show

data-dir-set:
	@test -n "$(DIR)" || (echo "Usage: make data-dir-set DIR=<path>"; exit 1)
	./scripts/data-dir.sh set "$(DIR)"

data-dir-migrate:
	@test -n "$(DIR)" || (echo "Usage: make data-dir-migrate DIR=<to> [FROM=<from>] [REMOVE_SOURCE=1] [FORCE=1]"; exit 1)
	@set --; \
	[ "$(REMOVE_SOURCE)" = "1" ] && set -- "$$@" --remove-source; \
	[ "$(FORCE)" = "1" ] && set -- "$$@" --force; \
	if [ -n "$(FROM)" ]; then \
	  ./scripts/data-dir.sh "$$@" migrate "$(FROM)" "$(DIR)"; \
	else \
	  ./scripts/data-dir.sh "$$@" migrate "$(DIR)"; \
	fi

# Switch chat profile when LM Studio is configured (interactive menu if multiple models).
#   make model-use FROM_LMSTUDIO=1 LIGHTRAG=same RESTART=1
#   make model-use MODEL=lmstudio-community/muse-glimmer-30b RESTART=1
#   make model-use PRESET=muse-glimmer
#   make model-use FROM_LMSTUDIO=1 YES=1   # non-interactive: first chat model
model-use:
	@set --; \
	[ -n "$(PRESET)" ] && set -- "$$@" --preset "$(PRESET)"; \
	[ "$(FROM_LMSTUDIO)" = "1" ] && set -- "$$@" --from-lmstudio; \
	[ "$(YES)" = "1" ] && set -- "$$@" -y; \
	[ "$(LIGHTRAG)" = "same" ] && set -- "$$@" --lightrag same; \
	[ "$(RESTART)" = "1" ] && set -- "$$@" --restart; \
	[ -n "$(MODEL)" ] && set -- "$$@" "$(MODEL)"; \
	if [ "$(FROM_LMSTUDIO)" != "1" ] && [ -z "$(MODEL)" ] && [ -z "$(PRESET)" ]; then \
		echo "Usage: make model-use MODEL=<id> | PRESET=<name> | FROM_LMSTUDIO=1"; \
		echo "  Optional: LIGHTRAG=same RESTART=1 YES=1"; \
		exit 1; \
	fi; \
	./scripts/model-use.sh "$$@"

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

# Per-project working dirs under ASSISTANT_DATA_ROOT/projects/. See docs/projects.md.
#   make project-init PROJECT=my-project
#   make project-index PROJECT=my-project
#   make project-index-check PROJECT=my-project
project-init:
	@test -n "$(PROJECT)" || (echo "Usage: make project-init PROJECT=<slug>"; exit 1)
	@$(resolve_data_root); \
	dest="$$DATA_ROOT/projects/$(PROJECT)"; \
	if [ -e "$$dest" ]; then \
		echo "Refusing to overwrite existing $$dest"; \
		exit 1; \
	fi; \
	mkdir -p "$$DATA_ROOT/projects"; \
	cp -R compose/hermes/project-template "$$dest"; \
	echo "Created $$dest — edit AGENTS.md, then make project-index PROJECT=$(PROJECT)"

project-index:
	@test -n "$(PROJECT)" || (echo "Usage: make project-index PROJECT=<slug>"; exit 1)
	@$(resolve_data_root); \
	test -d "$$DATA_ROOT/projects/$(PROJECT)" || (echo "Missing $$DATA_ROOT/projects/$(PROJECT) — run make project-init first"; exit 1); \
	python3 scripts/project-index.py --root "$$DATA_ROOT/projects/$(PROJECT)"

project-index-check:
	@test -n "$(PROJECT)" || (echo "Usage: make project-index-check PROJECT=<slug>"; exit 1)
	@$(resolve_data_root); \
	test -d "$$DATA_ROOT/projects/$(PROJECT)" || (echo "Missing $$DATA_ROOT/projects/$(PROJECT)"; exit 1); \
	python3 scripts/project-index.py --root "$$DATA_ROOT/projects/$(PROJECT)" --check

# Upgrade Hermes and its WebUI together. The WebUI reads the agent's on-disk
# state layout and is only tested against a matching agent, so bumping one alone
# is the failure mode this target exists to prevent — pass both tags:
#   make hermes-upgrade AGENT=v2026.9.14 WEBUI=0.52.113
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

# Removes Docker volumes and repo ./data (including hermes state).
# Never deletes an ASSISTANT_DATA_ROOT that lives outside the repo.
clean:
	@echo "WARNING: This removes Docker volumes and the repo ./data/ tree (incl. hermes)."
	@$(resolve_data_root); \
	REPO_DATA="$$(pwd)/data"; \
	if [ "$$DATA_ROOT" != "$$REPO_DATA" ]; then \
		echo "ASSISTANT_DATA_ROOT=$$DATA_ROOT will NOT be deleted."; \
	else \
		echo "ASSISTANT_DATA_ROOT is ./data — user content under it will be deleted with ./data/."; \
	fi
	@read -r -p "Type 'yes' to continue: " confirm; \
	if [ "$$confirm" = yes ]; then \
		docker compose down -v; \
		rm -rf data/; \
		echo "Cleaned."; \
	else \
		echo "Aborted."; \
	fi
