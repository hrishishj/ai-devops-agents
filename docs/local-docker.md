# Run it on your own machine (Docker Compose, free)

Starts everything locally with free software: **Ollama** (LLM), **PostgreSQL**, the **API**, and a **worker**. No cloud account.
Everything is bound to `127.0.0.1`; nothing is exposed to your network.

## Requirements
- Docker with Compose v2 (Docker Desktop is free for personal use and small businesses).
- Disk: ~6 GB for the default model. RAM: ~8 GB free for `qwen2.5-coder:7b` (smaller models need less).
- Without a GPU, a 7B model on CPU is slow: reviewing a small PR can take minutes. That is fine for shadow mode and trials.

## Start
```bash
cp .env.example .env
# edit .env: set DA_GITHUB_TOKEN (a token that can read PRs and comment), DA_GITHUB_WEBHOOK_SECRET,
# and DA_ADMIN_TOKEN / DA_ALERT_TOKEN if you use those endpoints. Empty secrets make the endpoints reject everything.
docker compose up -d --build
docker compose logs -f worker       # first start downloads the model (ollama-pull); the worker waits for it
curl http://127.0.0.1:8000/health/ready
```

Services: `ollama` -> `ollama-pull` (one-shot model download) · `postgres` · `migrate` (one-shot Alembic) · `api` · `worker`.
The worker handles review, security and incident jobs and also sweeps the database, so no message queue is needed.

## Review a pull request right now
```bash
docker compose run --rm api devopsagents review --repo owner/name --pr 42
docker compose logs -f worker       # watch it run; in shadow mode results are stored, not posted
```
Shadow mode is the default: nothing is posted until the repo has `.ai-agents.yml` with `review: {mode: comment}`.

## Receive pull-request webhooks (optional)
Your machine has no public address, so use a free relay such as [smee.io](https://smee.io):
1. Create a channel at smee.io and put its URL in `.env` as `SMEE_URL`.
2. In the GitHub repo: **Settings -> Webhooks -> Add webhook**: payload URL = your smee URL, content type `application/json`,
   secret = your `DA_GITHUB_WEBHOOK_SECRET`, event: **Pull requests**.
3. `docker compose --profile webhooks up -d`

Privacy: smee.io is a public relay, so webhook payloads pass through a third party. For private repositories, use a tunnel you
control instead, or skip webhooks and use the `review` command above.

## Use a different LLM
Set `COMPOSE_LLM_BASE_URL` (and `DA_LLM_API_KEY`, `DA_LLM_MODEL_LARGE`) in `.env`, for example an Ollama already running on your
host: `COMPOSE_LLM_BASE_URL=http://host.docker.internal:11434/v1`. The `ollama` service can then be left idle.
For a GPU, uncomment the `deploy` block under `ollama` in `compose.yaml`.

## Operate
```bash
docker compose down            # stop, keep data
docker compose down -v         # stop and delete the database and downloaded models
docker compose up -d --build   # apply code changes (migrations run automatically)
```
`DA_KILL_SWITCH=true` in `.env` stops all LLM calls and actions on next restart.

## Limits
- Not run end to end in the environment where this was written (no Docker daemon was available): the compose file is validated
  (`docker compose config`) and its pieces are tested, but the first `up` on your machine is the real test. Report what breaks.
- The Alembic migration has only been tested on SQLite; this is its first run against PostgreSQL.
- Verified test generation needs an isolated sandbox and is not part of this stack.
- For production use behind a public address, put a reverse proxy with TLS in front of the API.
