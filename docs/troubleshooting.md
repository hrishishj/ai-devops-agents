# Troubleshooting

Find your symptom, then the likely cause and the fix. Quickest diagnostics first:
- **What happened to a job?** `GET /admin/jobs/<id>` (header `x-admin-token`, or your GitHub token in `github` auth mode) shows `status`,
  `attempts`, `error` and the stored `result`. In Actions mode read the run's **Summary** tab.
- **Logs** are JSON lines: `docker compose logs -f worker api`, or the Actions step log.
- **Health:** `GET /health/ready` (database reachable, `dead_jobs` count). **Metrics:** `GET /metrics` ([observability.md](observability.md)).
- **Stop everything:** `DA_KILL_SWITCH=true` and restart; or per repo `review: {mode: "off"}` in `.ai-agents.yml`.

## Review and GitHub Actions
| Symptom | Cause | Fix |
|---|---|---|
| The *AI review* run is **skipped** (grey) | `AI_LLM_BASE_URL` not set; the PR is from a **fork**; or it is a **draft** | Set the variable; use a same-repo branch; mark ready for review |
| Run is green but the Summary says `review: dead` | `run-pr` exits 0 on agent failure unless strict | Set variable `AI_STRICT`=`true` so the run fails when an agent fails |
| Run succeeds but **nothing on the PR** | Shadow mode is the default | Read the run **Summary**; add `.ai-agents.yml` with `review: {mode: comment}` |
| `LLM request rejected (HTTP 401/403)` | Bad or missing provider key | Check the `AI_LLM_API_KEY` **secret** (not a variable) and that it matches the provider in `AI_LLM_BASE_URL`. GitHub Models was retired on 2026-07-30 |
| `LLM request rejected (HTTP 404)` | Wrong model id or base URL | Check `AI_LLM_BASE_URL` and the exact model id in the provider's catalogue |
| `LLM request rejected (HTTP 400)` mentioning `response_format` | The model/endpoint does not support JSON mode | Set `AI_LLM_JSON_MODE=false` (`DA_LLM_JSON_MODE=false`) |
| `LLM unavailable after 5 attempts` | Rate limit (429) or outage; the client honours `Retry-After` up to 90 s | The Summary now shows the provider's last error. Free pools often need minutes: set variables `AI_LLM_MAX_RETRIES`=6 and `AI_LLM_BACKOFF_SECONDS`=20; use a smaller `AI_CHUNK_TOKENS`; or another model/provider |
| `invalid model output` | The model cannot produce the required JSON even after one repair attempt | Use a stronger model; keep JSON mode on |
| `BudgetExceeded: daily token budget exhausted` | Per-repo (2,000,000) or global (20,000,000) daily tokens used | Raise `DA_DAILY_TOKEN_BUDGET_PER_REPO` / `DA_DAILY_TOKEN_BUDGET_GLOBAL` after checking for a runaway repo |
| `GitHub API 403: Resource not accessible by integration` | The token lacks a permission | Needs: Pull requests **write** (reviews, thread resolve), Checks **write** (security check), Contents **read**, Issues **write** (incident tickets), Actions **write** (runbook dispatch) |
| Job `dead` with `stale revision` | A new commit arrived; the old job correctly stopped | Nothing: a job for the new commit runs instead |
| Summary: `dropped (not in diff)` is high | The model points at lines that are not in the diff (they are discarded, never posted) | A few is normal; consistently most means a weak model: try another or lower `AI_CHUNK_TOKENS` |
| `Reviewed N of M files` with M > N | Files are ignored (`paths_ignore`), binary, or over the chunk budget | Adjust `paths_ignore`; raise `DA_REVIEW_CHUNK_TOKENS` if the model's context allows |
| **Ollama**: findings ignore the end of the diff | Ollama's default context window truncates long prompts silently | Set `OLLAMA_CONTEXT_LENGTH=16384` and a smaller `DA_REVIEW_CHUNK_TOKENS` (e.g. 2500) |
| **Ollama**: very slow or timeouts | CPU inference | Use a smaller model; allow several minutes per PR; the HTTP timeout is 300 s |
| The same finding keeps coming back | It was not resolved/👎'd, or it moved to a new line | Resolve the thread or react 👎; the fingerprint survives line moves |

## Security agent and test generation
| Symptom | Cause | Fix |
|---|---|---|
| No Semgrep findings | Semgrep is only installed in the Docker image, not on a plain runner | Expected: the secrets scanner always runs. Install Semgrep where the agent runs if you want it |
| The Check Run is **failing** | `security.fail_on` (default `critical`) matched | Fix the finding, or change `fail_on` in `.ai-agents.yml` |
| No generated tests posted | Off by default (`tests.enabled`); no sandbox configured; no changed public function without a test mention; or every test failed verification | Enable it; set `DA_SANDBOX_KIND=docker`; read `tests_notes` in the job result |
| `sandbox image 'devopsagents-sandbox' not found` | The image is not built | `docker build -t devopsagents-sandbox infra/sandbox` (or `make sandbox-image`) |
| `the Docker daemon is not running` / `docker is not installed` | Docker is not available to the process | Start Docker; run the agents on the host or a CI runner, not inside the Compose worker |
| `pytest is not installed in the sandbox image` | A custom image lacks pytest | Rebuild from `infra/sandbox/Dockerfile` |
| `Coverage was not measured` in the PR comment | The image has no `coverage` package | Rebuild the sandbox image (it installs `coverage`) |
| `the tests pass but never execute the target functions` | The quality gate dropped vacuous tests | Expected; the model wrote tests that do not call the code |
| `tests cover N% ... (minimum M%)` | `tests.min_coverage` is higher than the tests reach | Lower `min_coverage` or accept fewer tests |
| `rejected generated tests (banned import ...)` | The safety guard rejected generated code (process, network, file or `eval` use) | Expected; nothing unsafe was sent to the sandbox |

## Server and Docker Compose
| Symptom | Cause | Fix |
|---|---|---|
| `api` never becomes healthy | The `migrate` job failed | `docker compose logs migrate`; fix the database URL or password |
| `/health/ready` returns 503 | Database unreachable | `docker compose ps postgres`; check `POSTGRES_PASSWORD` matches the existing volume (`down -v` resets data) |
| Webhook returns **401 invalid signature** | Secret mismatch. An **empty** `DA_GITHUB_WEBHOOK_SECRET` rejects everything by design | Set the same secret in `.env` and in the GitHub webhook |
| Webhook accepted (202) but nothing happens | Worker not running; or no GitHub credentials, so the review/security agents are not registered; or the LLM is unreachable | `docker compose logs worker`; set `DA_GITHUB_TOKEN` (or a GitHub App); check `DA_LLM_BASE_URL` |
| Jobs stay `queued` | Worker down or overloaded | `docker compose ps worker`; see the `JobsStuckInQueue` alert and `devopsagents_oldest_queued_job_age_seconds` |
| `/metrics` returns 404 | Disabled until a token is set | Set `DA_METRICS_TOKEN`; scrape with `Authorization: Bearer` |
| `RuntimeError: LLM not configured: set DA_OPENAI_ENDPOINT` | `DA_LLM_PROVIDER=azure` without an endpoint | Use the default provider, or set the Azure endpoint |
| First `docker compose up` is very slow | `ollama-pull` is downloading several GB; the worker waits for it | `docker compose logs -f ollama-pull` |
| Backup/restore script errors on Windows | Run from PowerShell instead of bash | Use Git Bash or WSL; see [backup-restore.md](backup-restore.md) |

## Incident agent
| Symptom | Cause | Fix |
|---|---|---|
| Alertmanager webhook returns 401 | Missing/incorrect token | Send `Authorization: Bearer <DA_ALERT_TOKEN>` (or `X-Alert-Token`); an empty `DA_ALERT_TOKEN` rejects everything |
| Alert accepted, no action ever runs | By design in prod: alerts without an `environment` label are **prod**, and only dev/staging run unattended | Add an `environment` label; see `policies/default.yaml` (`auto_environments`) |
| Result `denied` | The policy refused: unknown runbook, invalid parameters, no evidence, cooldown, rate limit, or the kill switch | Read `reasons` in the job result |
| Result `awaiting_approval` | A human must approve (prod, tier 2, low confidence, or a freeze window) | `POST /admin/incidents/<target>/approve` |
| Approve returns **401** | No/invalid credentials | Token mode: `x-admin-token`; GitHub mode: `Authorization: Bearer <your GitHub token>` |
| Approve returns **403** | Not an approver (needs write access / allow-list), wrong role, or the action needs **two verified approvers** (token mode cannot satisfy that) | Use `DA_ADMIN_AUTH=github`; two different people approve |
| Approve returns **409** | Nothing is awaiting approval for that incident (already handled, or superseded) | Check the incident target and its latest job |
| Approve returns **410** | The pending approval **expired** (`approval_ttl_minutes`) | If the alert is still firing, a new analysis will be proposed |
| Approve returns **503** | `DA_APPROVER_REPO` / `DA_INCIDENT_REPO` not set in `github` mode | Set one of them |
| Result `escalated` after an action ran | Recovery was **not confirmed**: the alert is still firing in Prometheus, or the verifier could not tell | Check `DA_PROMETHEUS_URL`; the ticket explains. Unknown is never reported as success |
| Runbook workflow stays *Running* | Waiting for an Environment approval, or the script `runbook-scripts/<Name>.sh` is missing (then it fails) | Approve in GitHub, or add the script; see [free-incidents.md](free-incidents.md) |

## Still stuck?
Collect: the job's `error` and `result` (`GET /admin/jobs/<id>`), the worker log around that time, and your settings **with secrets removed**
(`env | grep '^DA_' | sed 's/=.*/=…/'`). Open an issue with those.
