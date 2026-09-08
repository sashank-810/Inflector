.PHONY: postgres migrate seed api test lint typecheck web-install web-dev ci

postgres:
	docker compose up postgres

migrate:
	alembic upgrade head

seed:
	python -m inflector_database.seed

api:
	uvicorn inflector_api.main:app --app-dir apps/api --reload --port 8000

test:
	pytest

lint:
	ruff check .

typecheck:
	pyright

web-install:
	cd apps/web && npm install

web-dev:
	cd apps/web && npm run dev

ci: lint typecheck test

