.PHONY: dev test lint types arch check eval image tf-validate

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
