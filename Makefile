.PHONY: api-install api-migrate api-dev api-real api-real-admin api-test api-sync web-install web-dev web-build \
	check pilot-up pilot-down pilot-preflight pilot-backup pilot-restore

API_HOST ?= 0.0.0.0
API_PORT ?= 8000
WEB_HOST ?= 0.0.0.0
WEB_PORT ?= 3000
API_INTERNAL_HOST ?= 127.0.0.1
PUBLIC_HOST ?= 127.0.0.1
MODEL_CONFIG ?= API.txt
MODEL_TIMEOUT ?= 180

api-install:
	uv venv --python 3.12 .venv
	uv pip install --python .venv/bin/python -e "apps/api[dev]"

api-migrate:
	.venv/bin/alembic -c apps/api/alembic.ini upgrade head

api-dev:
	.venv/bin/uvicorn hzcu_agent.main:app --app-dir apps/api/src --reload --host "$(API_HOST)" --port "$(API_PORT)" --no-access-log

api-real:
	.venv/bin/hzcu-agent serve --host "$(API_HOST)" --port "$(API_PORT)" --model-config "$(MODEL_CONFIG)" --model-timeout "$(MODEL_TIMEOUT)" --anonymous-campus-mirror

api-real-admin:
	HZCU_AUTH_MODE=anonymous \
	HZCU_LOCAL_ADMIN_ENABLED=true \
	HZCU_PUBLIC_API_BASE_URL="http://$(PUBLIC_HOST):$(API_PORT)" \
	HZCU_WEB_APP_URL="http://$(PUBLIC_HOST):$(WEB_PORT)" \
	HZCU_AUTH_COOKIE_SECURE=false \
	.venv/bin/hzcu-agent serve --host "$(API_HOST)" --port "$(API_PORT)" --model-config "$(MODEL_CONFIG)" --model-timeout "$(MODEL_TIMEOUT)" --anonymous-campus-mirror

api-test:
	.venv/bin/pytest apps/api/tests

api-sync:
	.venv/bin/hzcu-agent sync-sources

web-install:
	cd apps/web && npm install

web-dev:
	cd apps/web && npm run dev -- --api-url "http://$(API_INTERNAL_HOST):$(API_PORT)" --hostname "$(WEB_HOST)" --port "$(WEB_PORT)"

web-build:
	cd apps/web && npm run build

check: api-test web-build

pilot-up:
	docker compose up -d --build

pilot-down:
	docker compose down

pilot-preflight:
	docker compose exec -T api hzcu-agent pilot-preflight

pilot-backup:
	docker compose exec -T api hzcu-agent pilot-backup

pilot-restore:
	@test -n "$(BACKUP)" || (echo "用法: make pilot-restore BACKUP=hzcu-pilot-YYYYMMDDTHHMMSSZ.db" && exit 2)
	docker compose stop api ingestion-worker
	docker compose run --rm --no-deps api hzcu-agent pilot-restore --backup "/app/data/backups/$(BACKUP)"
	docker compose up -d api ingestion-worker caddy
