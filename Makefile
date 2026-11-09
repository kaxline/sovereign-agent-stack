.PHONY: setup ensure-local services model-defaults model-defaults-add model-defaults-reload up down logs ps restart clean doctor hermes-upgrade hermes-reload hermes-recreate gateway-preflight \
	bootstrap-buzz buzz-cli-install agent-create hermes-buzz-employee \
	buzz-relay-start buzz-relay-stop buzz-relay-status buzz-relay-logs buzz-relay-restart \
	buzz-admin \
	corpus-create corpus-use corpus-list corpus-ingest corpus-destroy \
	project-init project-index project-index-check project-sources-test bash32-test synthetic-mail-test synthetic-messages-test \
	messages-index messages-index-doctor messages-index-chats messages-index-test \
	messages-watch messages-notify-test messages-watch-test messages-app messages-app-test \
	mail-index mail-index-doctor mail-index-mailboxes mail-index-test mail-script-test mail-imap-test mail-mcp-test \
	mail-drafts mail-drafts-watch mail-drafts-doctor mail-drafts-test mail-apps mail-apps-test \
	mail-triage-cron mail-triage-cron-show mail-triage-cron-remove mail-cron-test model-use model-sync \
	messages-mcp-test messages-triage-cron messages-triage-cron-show messages-triage-cron-remove \
	calendar-test \
	data-dir-show data-dir-set data-dir-migrate \
	db-backup db-service-add db-psql todo-init todo-open dashboard-open \
	crm-init crm-add crm-remove crm-access crm-open crm-mode \
	vault-init vault-add vault-unlock vault-lock vault-sync vault-grant vault-requests vault-remove vault-open \
	secrets-sync bitwarden-test addon-list addon-test \
	budget-init budget-add budget-open budget-remove budget-access \
	coding-root-list coding-root-add \
	context-root-list context-root-add opencode-smoke-fixture \
	kb-eval-fixture kb-eval-fixture-remove \
	tool-eval-reset tool-eval-run tool-eval-score tool-eval-list

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
	@mkdir -p compose/postgres/services
	@test -f compose/caldav-mcp/accounts/personal.env || (cp compose/caldav-mcp/account.env.example compose/caldav-mcp/accounts/personal.env && echo "Created compose/caldav-mcp/accounts/personal.env — set CALDAV_* credentials for calendar profile")
	@./scripts/todo.sh ensure
	@./scripts/crm.sh ensure
	@./scripts/sync-signal-profile.sh
	@./scripts/sync-buzz-profile.sh
	@./scripts/sync-terminal-backend.sh
	@./scripts/sync-profiles.sh
	@./scripts/sync-n8n-credentials.sh

doctor:
	./scripts/doctor.sh

# Clone/setup local Buzz checkout, wire .env, start relay, install Hermes buzz CLI.
#   make bootstrap-buzz
bootstrap-buzz:
	./scripts/bootstrap-buzz.sh

# Install headless Buzz CLI into data/hermes/.local/bin (gitignored volume).
buzz-cli-install:
	./scripts/install-buzz-cli.sh

# Create an independent Hermes agent (data/hermes only — not committed).
#   make agent-create NAME=software-engineer DISPLAY_NAME="Software Engineer"
#   make agent-create NAME=designer ROLE="UI/UX" WITH=buzz
agent-create:
	@test -n "$(NAME)" || (echo "Usage: make agent-create NAME=<slug> [DISPLAY_NAME=\"Name\"] [ROLE=\"…\"] [WITH=buzz]"; exit 1)
	NAME="$(NAME)" DISPLAY_NAME="$(DISPLAY_NAME)" ROLE="$(ROLE)" WITH="$(WITH)" \
		./scripts/agent-create.sh "$(NAME)" "$(DISPLAY_NAME)"

# Compatibility: agent-create + Buzz attach (prefer make agent-create … WITH=buzz).
#   make hermes-buzz-employee PROFILE=software-engineer DISPLAY_NAME="Software Engineer"
hermes-buzz-employee:
	@test -n "$(PROFILE)$(NAME)" || (echo "Usage: make hermes-buzz-employee PROFILE=<slug> [DISPLAY_NAME=\"Name\"]"; exit 1)
	./scripts/hermes-buzz-employee.sh "$(if $(PROFILE),$(PROFILE),$(NAME))" "$(DISPLAY_NAME)"

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

# Shared Postgres (db profile). See docs/database.md.
#   make db-backup                       pg_dump into DB_BACKUP_DIR
#   make db-service-add NAME=<slug>      register an integration's schema + role
#   make db-psql                         admin psql shell
db-backup:
	./scripts/db.sh backup

db-service-add:
	@test -n "$(NAME)" || (echo "Usage: make db-service-add NAME=<slug>"; exit 1)
	./scripts/db.sh service-add "$(NAME)"

db-psql:
	./scripts/db.sh psql

# Vikunja todo lists (todo profile). See docs/todo.md.
#   make todo-init     account with a generated password + the Hermes MCP tokens
#   make todo-open     open the web app, signed in
todo-init:
	./scripts/todo.sh init

todo-open:
	@./scripts/todo.sh open

# Twenty CRM (crm profile, or remote). See docs/crm.md.
#   make crm-init [SLUG=main] [ROTATE=1]   local workspace + both Hermes keys
#   make crm-add SLUG=name URL=https://... [FORCE=1]  a workspace on a remote Twenty
#   make crm-remove SLUG=name              forget a workspace (Twenty keeps it)
#   make crm-access SLUG=name ACCESS=read  look things up only (write: make changes)
#   make crm-open [SLUG=name]              open a workspace in the browser
#   make crm-mode MULTI=0|1                single- or multi-workspace local instance
crm-init:
	./scripts/crm.sh init $(if $(SLUG),--slug "$(SLUG)") $(if $(ROTATE),--rotate)

crm-add:
	@test -n "$(SLUG)" -a -n "$(URL)" || (echo "Usage: make crm-add SLUG=<slug> URL=https://crm.example.com"; exit 1)
	./scripts/crm.sh add --slug "$(SLUG)" --url "$(URL)" $(if $(FORCE),--force)

crm-remove:
	@test -n "$(SLUG)" || (echo "Usage: make crm-remove SLUG=<slug>"; exit 1)
	./scripts/crm.sh remove --slug "$(SLUG)"

crm-access:
	@test -n "$(SLUG)" -a -n "$(ACCESS)" || (echo "Usage: make crm-access SLUG=<slug> ACCESS=read|write"; exit 1)
	./scripts/crm.sh access --slug "$(SLUG)" "$(ACCESS)"

crm-open:
	@./scripts/crm.sh open $(if $(SLUG),--slug "$(SLUG)")

crm-mode:
	@test -n "$(MULTI)" || (echo "Usage: make crm-mode MULTI=0|1"; exit 1)
	./scripts/crm.sh mode "$(MULTI)"

# Bitwarden vault for browser sign-ins (bitwarden profile; vault for local mode). See docs/bitwarden.md.
#   make vault-init [VAULT_OWNER_EMAIL=you@example.com]  local Vaultwarden, the agent account, you as Owner
#   make vault-add SLUG=name MODE=cloud [REGION=eu] [SCOPE=folder:Agent] [UNLOCK=stored|manual] [FORCE=1]
#   make vault-add SLUG=name MODE=selfhosted URL=https://... [SCOPE=collection:<id>]
#                                          an account the broker signs in to; SCOPE=all also needs CONFIRM_SCOPE_ALL=1
#   make vault-unlock SLUG=name            unlock a manual account (hidden prompt)
#   make vault-lock SLUG=name              lock it again
#   make vault-sync                        re-read every unlocked account now
#   make vault-requests                    the agent's pending access requests
#   make vault-grant [SITE=example.com] [SLUG=name]  move an item into the agent's scope
#   make vault-remove SLUG=name            forget an account (file, secrets, bw data)
#   make vault-open                        open the local web vault
#   make secrets-sync                      write stack secrets from the vault into their files
vault-init:
	./scripts/vault.sh init $(if $(VAULT_OWNER_EMAIL),--owner "$(VAULT_OWNER_EMAIL)")

vault-add:
	@test -n "$(SLUG)" -a -n "$(MODE)" || (echo "Usage: make vault-add SLUG=<slug> MODE=cloud|selfhosted [REGION=eu] [URL=https://...] [SCOPE=...] [UNLOCK=stored|manual]"; exit 1)
	./scripts/vault.sh add --slug "$(SLUG)" --mode "$(MODE)" $(if $(REGION),--region "$(REGION)") \
		$(if $(URL),--url "$(URL)") $(if $(SCOPE),--scope "$(SCOPE)") $(if $(UNLOCK),--unlock "$(UNLOCK)") \
		$(if $(FORCE),--force)

vault-unlock:
	@test -n "$(SLUG)" || (echo "Usage: make vault-unlock SLUG=<slug>"; exit 1)
	@./scripts/vault.sh unlock --slug "$(SLUG)"

vault-lock:
	@test -n "$(SLUG)" || (echo "Usage: make vault-lock SLUG=<slug>"; exit 1)
	@./scripts/vault.sh lock --slug "$(SLUG)"

vault-sync:
	@./scripts/vault.sh sync

vault-requests:
	@./scripts/vault.sh requests

vault-grant:
	@./scripts/vault.sh grant $(if $(SITE),--site "$(SITE)") $(if $(SLUG),--slug "$(SLUG)")

vault-remove:
	@test -n "$(SLUG)" || (echo "Usage: make vault-remove SLUG=<slug>"; exit 1)
	./scripts/vault.sh remove --slug "$(SLUG)"

vault-open:
	@./scripts/vault.sh open

secrets-sync:
	@./scripts/vault.sh secrets-sync

# Every Bitwarden add-on test. The image parts skip without the pinned Hermes
# image; test-vw-register.py and the compose config check need Docker, and the
# compose check a .env that satisfies interpolation. Runs all, then lists failures.
BITWARDEN_TESTS = \
	"python3 scripts/test-addon-manifests.py" \
	"python3 scripts/test-digest-redaction.py" \
	"python3 scripts/test-bitwarden-broker.py" \
	"python3 scripts/test-bitwarden-access.py" \
	"python3 scripts/test-bitwarden-fill.py" \
	"sh scripts/test-bitwarden-registration.sh" \
	"python3 scripts/test-bitwarden-compose-config.py" \
	"sh scripts/test-bitwarden-secrets-sync.sh" \
	"sh scripts/test-bitwarden-setup.sh" \
	"bash scripts/test-vault-sh.sh" \
	"python3 scripts/test-vw-register.py"
bitwarden-test:
	@failed=""; for t in $(BITWARDEN_TESTS); do \
		echo "=== $$t"; $$t || failed="$$failed\n  $$t"; \
	done; \
	if [ -n "$$failed" ]; then printf 'FAILED:%b\n' "$$failed"; exit 1; fi; \
	echo "all Bitwarden tests passed"

# Built-in add-ons through scripts/addon.sh, the interface Boundary calls.
#   make addon-list    every add-on with its settings, instances and missing values (JSON)
#   make addon-test    the manifest, store and addon.sh tests
addon-list:
	@./scripts/addon.sh list --json

addon-test:
	python3 scripts/test-addon-manifests.py
	python3 scripts/test-addon-store.py
	sh scripts/test-addon-cli.sh

# Actual Budget (budget and budget-local profiles, or remote). See docs/budget.md.
#   make budget-init [SYNC_ID=id] [ROTATE=1]  local server: password, budget, budget.env
#     (scripts/budget.sh init --server-only is the add-on's hook: no budget.env)
#   make budget-add URL=https://... [SYNC_ID=id] [FORCE=1]  a budget on a remote server
#   make budget-open                         open the web app, password on the clipboard
#   make budget-remove                       forget the budget (file only; server data stays)
#   make budget-access ACCESS=read           look things up only (write: make changes)
budget-init:
	$(if $(SYNC_ID),ACTUAL_BUDGET_SYNC_ID="$(SYNC_ID)") ./scripts/budget.sh init $(if $(ROTATE),--rotate)

budget-add:
	@test -n "$(URL)" || (echo "Usage: make budget-add URL=https://budget.example.com [SYNC_ID=id]"; exit 1)
	$(if $(SYNC_ID),ACTUAL_BUDGET_SYNC_ID="$(SYNC_ID)") ./scripts/budget.sh add --url "$(URL)" $(if $(FORCE),--force)

budget-open:
	@./scripts/budget.sh open

budget-remove:
	./scripts/budget.sh remove

budget-access:
	@test -n "$(ACCESS)" || (echo "Usage: make budget-access ACCESS=read|write"; exit 1)
	./scripts/budget.sh access "$(ACCESS)"

# Hermes dashboard, signed in through a one-time link. See docs/hermes.md.
#   make dashboard-open
#   make dashboard-open NEXT=/kanban
dashboard-open:
	@./scripts/dashboard-open.py "$(or $(NEXT),/)"

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

# Re-apply LLM_MODEL from .env to every Hermes profile and OpenCode.
#   make model-sync RESTART=1
model-sync:
	@if [ "$(RESTART)" = "1" ]; then ./scripts/model-use.sh --sync --restart; else ./scripts/model-use.sh --sync; fi

# Per-model request defaults for llm-proxy (docs/model-defaults.md).
#   make model-defaults                          # list examples and local.json
#   make model-defaults-add EXAMPLE=qwen3.6-35b-a3b [REPLACE=1]
model-defaults:
	@./scripts/model-defaults.py list

model-defaults-add:
	@[ -n "$(EXAMPLE)" ] || { echo "Usage: make model-defaults-add EXAMPLE=<name> [REPLACE=1]"; exit 1; }
	@set -- add "$(EXAMPLE)"; [ "$(REPLACE)" = "1" ] && set -- "$$@" --replace; \
	./scripts/model-defaults.py "$$@"
	@$(MAKE) --no-print-directory model-defaults-reload

# Re-read model-defaults/local.json without restarting llm-proxy.
model-defaults-reload:
	@if [ -n "$$(docker compose ps -q llm-proxy 2>/dev/null)" ]; then \
		docker compose kill -s HUP llm-proxy >/dev/null && echo "[model-defaults] llm-proxy reloaded"; \
	else \
		echo "[model-defaults] llm-proxy is not running; it reads the file when it starts"; \
	fi

# Each optional service's *_ENABLED flag and whether its profile is on.
services:
	@./scripts/sync-profiles.sh --list

up: ensure-local
	@echo "Profiles: $$(grep -E '^COMPOSE_PROFILES=' .env | head -1 | cut -d= -f2-)"
	@./scripts/vault.sh pre-up
	docker compose up -d
	@./scripts/vault.sh up-hook

down:
	docker compose down

logs:
	docker compose logs -f hermes hermes-webui searxng

ps:
	docker compose ps

# Compose restarts bitwarden-broker with hermes (depends_on restart: true);
# vault.sh follow-hermes repairs a broker left in a removed hermes namespace
# and lists the manual accounts to unlock.
restart:
	@rc=0; docker compose restart hermes hermes-webui || rc=$$?; \
	./scripts/vault.sh follow-hermes; exit $$rc

# Re-apply Hermes config after changing .env, MCP tokens or servers, skills, or
# the compose file: both profile bootstraps, then recreate hermes so every
# profile reloads its config and the system prompt. Takes about two minutes.
#   make hermes-reload
hermes-reload: ensure-local
	docker compose run --rm -e HERMES_BOOTSTRAP_FORCE=1 hermes-api-bootstrap
	docker compose run --rm --no-deps -e HERMES_BOOTSTRAP_FORCE=1 hermes-browser-bootstrap
	docker compose up -d --no-deps --force-recreate hermes
	@./scripts/vault.sh follow-hermes

# Recreate hermes alone, so s6 picks up new profiles and gateways, then move the
# Bitwarden broker into its new network namespace. Runs no bootstrap.
#   make hermes-recreate
hermes-recreate: ensure-local
	docker compose up -d --force-recreate hermes
	@./scripts/vault.sh follow-hermes

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

project-sources-test:
	python3 scripts/test-project-sources.py

# Host scripts under macOS /bin/bash 3.2. Skips where there is no bash 3.x.
bash32-test:
	sh scripts/test-bash32.sh

# Fake Apple Mail store for mail-reader tests. See docs/email.md.
synthetic-mail-test:
	python3 scripts/test-synthetic-mail.py

# Fake Messages.app store for message-reader tests. See imessage/README.md.
synthetic-messages-test:
	python3 imessage/tests/test-synthetic-messages.py

# Messages indexer (host-side, needs Full Disk Access). Reads chat.db
# read-only into a scoped index. See imessage/README.md.
#   cp imessage/messages-index.toml.example messages-index.toml   # then fill it in
MESSAGES_INDEX_CONFIG ?= messages-index.toml
messages-index:
	python3 imessage/messages-index.py --config "$(MESSAGES_INDEX_CONFIG)" run

messages-index-doctor:
	python3 imessage/messages-index.py --config "$(MESSAGES_INDEX_CONFIG)" doctor

messages-index-chats:
	python3 imessage/messages-index.py --config "$(MESSAGES_INDEX_CONFIG)" chats

messages-index-test:
	python3 imessage/tests/test-messages-index.py

# Messages watcher: keeps the index current and raises new-message events as
# the config's [notify] table says. Runs until stopped. See imessage/README.md.
messages-watch:
	python3 imessage/messages-index.py --config "$(MESSAGES_INDEX_CONFIG)" watch

messages-notify-test:
	python3 imessage/messages-index.py --config "$(MESSAGES_INDEX_CONFIG)" notify-test

messages-watch-test:
	python3 imessage/tests/test-messages-watch.py

# Messages reader app (macOS): holds Full Disk Access instead of your terminal.
messages-app:
	imessage/macos/build.sh

messages-app-test:
	python3 imessage/tests/test-messages-app.py

# Apple Mail indexer (host-side). Reads through Mail.app by default; set
# source = "store" in the config to read Mail's files instead. See docs/email.md.
#   cp mail-index.toml.example mail-index.toml   # then fill it in
#   make mail-index-doctor && make mail-index
MAIL_INDEX_CONFIG ?= mail-index.toml
mail-index:
	python3 scripts/mail-index.py --config "$(MAIL_INDEX_CONFIG)" run

mail-index-doctor:
	python3 scripts/mail-index.py --config "$(MAIL_INDEX_CONFIG)" doctor

mail-index-mailboxes:
	python3 scripts/mail-index.py --config "$(MAIL_INDEX_CONFIG)" mailboxes

mail-index-test:
	python3 scripts/test-mail-indexer.py

# Mail.app scripting source, run under Node against a fake Mail (needs node).
mail-script-test:
	python3 scripts/test-mail-script.py

# IMAP source and drafts against a throwaway GreenMail server in Docker
# (synthetic mail only; skipped when Docker isn't running).
mail-imap-test:
	python3 scripts/test-mail-imap.py

# Mail drafts (host-side): saves drafts queued by mail-drafts-mcp to Mail's
# Drafts. Never sends. See docs/email.md.
MAIL_DRAFTS_DIR ?= ./data/mail-drafts
mail-drafts:
	python3 scripts/mail-drafts.py --dir "$(MAIL_DRAFTS_DIR)" run

mail-drafts-watch:
	python3 scripts/mail-drafts.py --dir "$(MAIL_DRAFTS_DIR)" watch

mail-drafts-doctor:
	python3 scripts/mail-drafts.py --dir "$(MAIL_DRAFTS_DIR)" doctor

mail-drafts-test:
	python3 scripts/test-mail-drafts.py
	sh scripts/test-mail-drafts-registration.sh

# macOS apps that hold the mail permissions instead of your terminal. Build
# here; install with macos/mail-apps/install.sh (see docs/email.md).
mail-apps:
	macos/mail-apps/build.sh

mail-apps-test:
	python3 scripts/test-mail-apps.py

# Hermes cron job that reports new mail: mail tools only, runs only when newer
# mail was indexed, delivers to Signal when set up. See docs/email.md.
#   make mail-triage-cron [SCHEDULE="every 15m"] [DELIVER=signal]
mail-triage-cron:
	scripts/mail-triage-cron.sh create $(if $(SCHEDULE),--schedule "$(SCHEDULE)") $(if $(DELIVER),--deliver "$(DELIVER)")

mail-triage-cron-show:
	scripts/mail-triage-cron.sh show

mail-triage-cron-remove:
	scripts/mail-triage-cron.sh remove

mail-cron-test:
	python3 scripts/test-mail-cron.py

# Read-only mail MCP: query layer against a synthetic index, and which profiles get it.
mail-mcp-test:
	python3 scripts/test-mail-mcp.py
	sh scripts/test-mail-mcp-registration.sh

# Read-only Messages MCP: query layer, triage cursor and forwarder against a
# synthetic index; which profiles get it; the webhook route (needs the hermes
# container); and the container itself (needs Docker). See docs/messages.md.
messages-mcp-test:
	python3 imessage/tests/test-messages-mcp.py
	sh scripts/test-messages-mcp-registration.sh
	python3 scripts/test-messages-webhook-config.py
	python3 imessage/tests/test-messages-mcp-container.py

# Hermes cron job that reports new messages: messages tools only, fired by
# new-message events and checked on a schedule, delivers to Signal when set up.
#   make messages-triage-cron [SCHEDULE="every 30m"] [DELIVER=signal]
messages-triage-cron:
	scripts/messages-triage-cron.sh create $(if $(SCHEDULE),--schedule "$(SCHEDULE)") $(if $(DELIVER),--deliver "$(DELIVER)")

messages-triage-cron-show:
	scripts/messages-triage-cron.sh show

messages-triage-cron-remove:
	scripts/messages-triage-cron.sh remove

# CalDAV bootstrap and the calendar overlays. The image checks need the pinned
# Hermes image and skip without it.
calendar-test:
	sh scripts/test-caldav-password-ref.sh
	python3 scripts/test-caldav-foreign-entries.py
	python3 scripts/test-mirror-exclude.py
	python3 scripts/test-mcp-trust-details.py

# Shared coding roots (Hermes + OpenCode). See docs/opencode.md.
#   make coding-root-list
#   make coding-root-add DIR=/absolute/path/to/repo
coding-root-list:
	./scripts/coding-root.sh list

coding-root-add:
	@test -n "$(DIR)" || (echo "Usage: make coding-root-add DIR=/absolute/path"; exit 1)
	./scripts/coding-root.sh add "$(DIR)"

# Read-only context roots (Hermes only — notes Hermes can open, not coding targets).
#   make context-root-list
#   make context-root-add DIR=/absolute/path/to/notes
context-root-list:
	./scripts/context-root.sh list

context-root-add:
	@test -n "$(DIR)" || (echo "Usage: make context-root-add DIR=/absolute/path"; exit 1)
	./scripts/context-root.sh add "$(DIR)"

# Disposable greet+pytest repo under OPENCODE_WORKSPACE_HOST for delegation smoke tests.
opencode-smoke-fixture:
	./scripts/opencode-smoke-fixture.sh

# Temporary LightRAG text insert for kb-query (POST /documents/text, then delete).
kb-eval-fixture:
	./scripts/kb-eval-fixture.sh install

kb-eval-fixture-remove:
	./scripts/kb-eval-fixture.sh remove

# Tool-calling eval against the Hermes browser profile (see docs/tool-eval.md).
# CASE=web-fact|all  — scripted twin of WebUI paste cases.
tool-eval-reset:
	./scripts/tool-eval.sh reset

tool-eval-list:
	./scripts/tool-eval.sh list

tool-eval-run:
	./scripts/tool-eval.sh run "$(or $(CASE),all)"

tool-eval-score:
	./scripts/tool-eval.sh score

# Hermes' multiplex migration preflight, run against a throwaway copy of the
# Hermes home with gateway.standalone removed. Changes nothing.
gateway-preflight:
	./scripts/gateway-preflight.sh

# Upgrade Hermes and its WebUI together. The WebUI reads the agent's on-disk
# state layout and is only tested against a matching agent, so bumping one alone
# is the failure mode this target exists to prevent — pass both tags:
#   make hermes-upgrade AGENT=v0.22.7 WEBUI=0.52.544
# Re-runs both profile bootstraps afterwards, because a new agent may add config
# keys the running profiles do not have yet. Refuses an agent tag that dropped
# gateway.standalone while the stack still pins it.
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
	./scripts/check-hermes-standalone.sh "$(AGENT)"
	@python3 -c 'import re,sys,pathlib; p=pathlib.Path(".env"); t=p.read_text(); \
	    [t := re.sub(r"(?m)^%s=.*$$" % k, "%s=%s" % (k, v), t) for k, v in \
	     (("HERMES_AGENT_IMAGE_TAG", "$(AGENT)"), ("HERMES_WEBUI_IMAGE_TAG", "$(WEBUI)"))]; \
	    p.write_text(t)'
	@grep -E '^HERMES_(AGENT|WEBUI)_IMAGE_TAG=' .env
	docker compose pull hermes hermes-webui
	$(MAKE) hermes-reload
	docker compose up -d hermes-webui
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
