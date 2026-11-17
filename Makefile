.PHONY: backup restore sandbox-image dev test lint types arch check eval image tf-validate up down logs compose-check

dev:
	uv venv --python 3.12 && uv pip install -e ".[dev]"

test:
	.venv/bin/pytest -q

lint:
	.venv/bin/ruff check src tests evals && .venv/bin/ruff format --check src tests evals

types:
	.venv/bin/mypy -p devopsagents -p evals

arch:
	.venv/bin/lint-imports

check: lint types arch test
	.venv/bin/devopsagents check-config
	.venv/bin/python -m evals.run_evals --offline

eval:
	.venv/bin/python -m evals.run_evals

image:
	docker build -f infra/container/Dockerfile -t devopsagents:dev .

tf-validate:
	terraform -chdir=infra/terraform init -backend=false && terraform -chdir=infra/terraform validate

up:
	docker compose up -d --build

down:
	docker compose down

logs:
	docker compose logs -f worker api

compose-check:
	docker compose config -q

sandbox-image:
	docker build -t devopsagents-sandbox infra/sandbox

backup:
	scripts/backup.sh

restore:
	@test -n "$(FILE)" || { echo "usage: make restore FILE=backups/<file>.dump"; exit 2; }
	scripts/restore.sh "$(FILE)"
