# AI DevOps Agents

Policy-gated AI agents for **pull-request review**, **security triage** and **IT incident response**.

**Free by default:** runs inside GitHub Actions or on your own machine with a free LLM (Ollama, GitHub Models, Groq, Gemini)
and needs no paid service. Azure (Container Apps, PostgreSQL, Service Bus, Azure OpenAI, Terraform) is an optional
deployment target, not a requirement. Start here: **[docs/free-setup.md](docs/free-setup.md)**.

| Agent | Trigger | What it does | Safety posture |
|---|---|---|---|
| **Review** | PR opened/updated | Chunks the diff, finds real defects with an LLM, **grounds every comment to a line that exists in the diff**, posts one review | Shadow mode by default; read-only |
| **Security** | PR opened/updated | Runs deterministic scanners (secrets, Semgrep) on changed lines; LLM only triages/explains; reports via a **Check Run** | LLM can't dismiss high-precision findings |
| **Incident** | Azure Monitor alert | Gathers read-only evidence, ranks hypotheses, proposes a **registered runbook**; a **policy engine** decides run / ask a human / deny; verifies recovery | Model confidence is advisory only |

Design rationale: [docs/TARGET_ARCHITECTURE.md](docs/TARGET_ARCHITECTURE.md) · decisions: [docs/adr/](docs/adr/) ·
SLOs: [docs/slo.md](docs/slo.md) · on-call: [docs/runbooks-ops/](docs/runbooks-ops/)

## How it works

```
GitHub webhook / Azure Monitor ──► Ingress API (verify, normalise, persist job, 202)
                                          │  job row = commitment; queue message = nudge
                                          ▼
                            PostgreSQL job store  ◄──── sweep ────  Workers (review|security|incident)
                            (idempotent keys, leases,                 lease job → run workflow → record
                             supersede, audit log)                          │
                                                                            ▼
                                  LLM gateway (redaction, budgets, cache, retries, schema-validated output)
                                  Policy engine + runbook registry (deterministic; the only path to side effects)
```

Key properties (each covered by tests): idempotent enqueue keyed on `agent:target:head_sha:version`;
a new push **supersedes** in-flight work and a superseded job can never post; crashed workers' leases expire and are
re-claimed; lost queue messages can't strand a job (workers sweep the DB); every action is audited **before** it runs;
approvals can lift a "needs approval" but never a "deny" (cooldown, bad params, kill switch).

## Repository layout

```
src/devopsagents/
  domain/         pure models: jobs, events, findings, actions, diff parser     (no I/O)
  core/           config, job store/runner/queue, LLM gateway, prompts, policy  (depends on domain)
  integrations/   GitHub App client, Azure (OpenAI, Service Bus, Automation), scanners
  agents/         review/  security/  incident/  workflows
  apps/           api (ingress) · worker · cli · container (composition root)
prompts/          versioned prompt files (review/discover.v1.md ...)
runbooks/         registry: <name>/runbook.yaml  (tier, typed params, cooldown, verification)
policies/         policy-as-data (auto-execution environments, freeze windows, ...)
evals/            golden datasets + harness (precision/recall/grounding gates)
infra/            terraform (modules + envs) · container/Dockerfile
tests/            unit · contract · integration
```

Layering `apps → agents → integrations → core → domain` is enforced in CI by import-linter.

## Free quick start: review a pull request

```bash
pip install "git+https://github.com/hrishishj/ai-devops-agents@main"
export DA_GITHUB_TOKEN=<token that can read the PR and comment>
export DA_LLM_BASE_URL=http://localhost:11434/v1 DA_LLM_MODEL_LARGE=qwen2.5-coder:7b   # Ollama
devopsagents run-pr --repo owner/name --pr 42      # shadow mode: prints what it would post
```

Incident response without Azure (Alertmanager + Prometheus + GitHub Actions runbooks + GitHub-verified approvals):
[docs/free-incidents.md](docs/free-incidents.md).

Prefer an always-on local stack (Ollama + PostgreSQL + API + worker)? `docker compose up -d --build` - see
[docs/local-docker.md](docs/local-docker.md).

Or enable `.github/workflows/ai-review.yml` by setting the `AI_LLM_BASE_URL` repository variable (details and model choices in
[docs/free-setup.md](docs/free-setup.md)). Nothing is posted to a PR until the repo opts in with `review: {mode: comment}`.

## Quick start (local, no Azure needed)

```bash
uv venv --python 3.12 && uv pip install -e ".[dev]"
.venv/bin/pytest                         # 88 tests
.venv/bin/devopsagents check-config      # validate runbooks, policy, prompts
.venv/bin/python -m evals.run_evals --offline
```

Run the stack locally (SQLite, in-process queue):

```bash
cp .env.example .env     # set DA_GITHUB_WEBHOOK_SECRET etc.
devopsagents serve --port 8000
devopsagents worker --agent review
```

Without `DA_OPENAI_ENDPOINT` the LLM backend fails loudly rather than silently doing nothing; without a GitHub App
configured, review/security workers are not registered.

## Onboarding a repository

1. Install the GitHub App on the repo (permissions: pull requests R/W, contents R, checks W, issues W, metadata R;
   events: Pull request).
2. Add `.ai-agents.yml` (optional — defaults to **shadow mode**, so nothing is posted until opted in):
   ```yaml
   version: 1
   review:   { mode: comment, max_comments: 10, paths_ignore: ["**/generated/**"] }
   security: { enabled: true, fail_on: [critical, high] }
   tests:    { enabled: true }          # verified test generation (needs the sandbox)
   ```
3. Watch shadow results (`job.result`) and the acceptance rate; flip `mode: comment` when they meet [docs/slo.md](docs/slo.md).

## Incident automation model

| Tier | Examples | Behaviour |
|---|---|---|
| 0 diagnostic | evidence queries | automatic |
| 1 reversible | `restart-service`, `scale-out`, `clear-disk` | automatic only in `auto_environments` (default dev/staging), with evidence, cooldown, rate limit, confidence floor; **prod needs approval** until enabled in `policies/default.yaml` |
| 2 stateful | `failover-db` | always human approval (`POST /admin/incidents/{target}/approve`) |
| 3 destructive | — | never automated |

Add a runbook: create `runbooks/<name>/runbook.yaml` (JSON-schema params with `additionalProperties: false`), add the
Automation runbook it names, run `devopsagents check-config`, and list it in `prompts/incident/analyse.v1.md` (bump to v2).

## Known limitations (honest status)

- **Test generation** is implemented but opt-in (`tests: {enabled: true}` in `.ai-agents.yml`) and needs an isolated sandbox
  (`DA_SANDBOX_ENDPOINT`, an Azure Container Apps session pool whose image includes pytest; not provisioned by the Terraform).
  The Azure adapter is contract-tested with a mock only. It verifies tests against the PR version of a single file, so modules
  that import other repo code will not verify; it does not measure real coverage yet.
- **Recovery verification** uses the Azure Monitor alert state (`AlertStateVerifier`, enabled when `DA_AUTOMATION_ACCOUNT_URL` is set);
  anything it cannot determine escalates to a human. Rollback is suggested in the ticket, not executed automatically.
- **Approver identity**: `DA_ADMIN_AUTH=github` verifies the approver with their own GitHub token (free; write access to the
  approver repo required, optional allow-list). `easyauth` (Entra) is also supported. The default `token` mode still trusts a
  self-declared name. No two-person rule or approval expiry yet (ADR-0004).
- **DB migrations** use Alembic (`devopsagents migrate`); in Azure a Container Apps job runs it after each deploy and apps run with
  `DA_CREATE_SCHEMA=false`. Locally the schema is auto-created.
- **Terraform** passes `fmt` and `validate` (1.9.8, `init -backend=false`) but has never been planned or applied against a real
  subscription; expect minor fixes on first apply. Network isolation (private endpoints) and queue-driven KEDA scaling are follow-ups.
- Review thread resolution on new pushes and feedback-driven suppression (👎 → fingerprint) are designed but not implemented.
- Live Azure OpenAI / GitHub / Azure Automation adapters are tested with fakes and mock transports only; run the live evals
  and a staging smoke test before trusting them.

## License

MIT — see [LICENSE](LICENSE).
