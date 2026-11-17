# AI DevOps Agents — Target Architecture & Plan

Status: **partially implemented** (see below) · Scope: clean-slate design; the original implementation was removed.

## Implementation status

| Area | State |
|---|---|
| Phase 0 foundations (job store, leases, idempotency, supersede, ingress API, worker, config, CI, Terraform) | built; Terraform unvalidated |
| Phase 1 LLM platform (gateway, redaction, budgets, cache, prompt registry, eval harness) | built |
| Phase 2–3 review agent (chunking, grounding, ranking, shadow/comment modes) | built |
| Phase 4 security agent (secrets + Semgrep scanners, LLM triage, Check Run) | built; **test generation/sandbox run not built** |
| Phase 5–6 incident agent (evidence port, policy engine, runbook registry, approvals, audit) | built; **real recovery verifier and rollback execution not built** |
| Phase 7 hardening (private networking, Entra approvers, Alembic, load/DR tests) | not started |

Deviations from this document: queues do not use Service Bus sessions ([ADR-0003](adr/0003-db-lease-not-sessions.md));
the policy engine is embedded Python with rules-as-data in `policies/` (open decision #2 resolved: embedded first).


---

## 1. Purpose and non-goals

**Purpose.** Reduce engineering toil in three areas, with measurable quality:

1. **Review** — high-signal pull-request review (bugs, security, risk), not style noise.
2. **Security & tests** — find real vulnerabilities and test gaps in changed code; propose verified fixes/tests.
3. **IT Ops** — triage alerts with evidence, and execute *pre-approved, bounded* remediations safely.

**Non-goals (v1).** Autonomous merging; free-form shell access for the LLM; multi-cloud; replacing
human on-call; training/fine-tuning models.

## 2. Design principles

| # | Principle | Consequence |
|---|---|---|
| 1 | **Workflows, not free agents.** Fixed pipelines with LLM steps inside. | Predictable cost, testable, auditable. |
| 2 | **Deterministic tools detect; LLMs judge.** Use Semgrep/CodeQL/gitleaks for detection; LLM only triages, explains, drafts. | Fewer false positives, no home-grown regex SAST. |
| 3 | **Policy decides, never the model.** LLM confidence is advisory input; a deterministic policy engine gates every side effect. | Safe remediation. |
| 4 | **Untrusted input everywhere.** PR text, diffs, logs are attacker-controlled (prompt injection). Review agents get *read-only* tools. | Privilege separation. |
| 5 | **One ingress path, durable execution.** Webhook → verify → persist → queue → worker. Nothing runs in the request process. | No lost or duplicated work. |
| 6 | **Idempotent by construction.** Keys include the revision (`head_sha`) and agent version; jobs have lease + terminal states. | Retries and re-pushes behave correctly. |
| 7 | **Everything measurable.** Eval sets, shadow mode, acceptance-rate metrics gate rollout. | Quality is data, not opinion. |
| 8 | **No long-lived secrets.** Managed identity, Entra auth to Azure OpenAI, GitHub OIDC for deploy. | Smaller blast radius. |
| 9 | **Config is typed and versioned.** Per-repo `.ai-agents.yml` + service-level settings validated at startup. | No dead config. |

## 3. System context

```
 GitHub (App webhooks)   Azure Monitor alerts   Developer CLI / API
          │                      │                       │
          └──────────────┬───────┴───────────────────────┘
                         ▼
              ┌────────────────────┐   verify signature / Entra token
              │   Ingress API      │   normalise → Event; dedupe by delivery id
              │   (FastAPI, thin)  │   persist Job(status=queued) + enqueue
              └─────────┬──────────┘   returns 202 in <100 ms
                        ▼
              ┌────────────────────┐
              │ Azure Service Bus  │   queues: review, security, incident
              │ (sessions per PR/  │   session id = repo#pr / incident id
              │  incident, DLQ)    │   → ordered, one-at-a-time per target
              └─────────┬──────────┘
                        ▼
              ┌────────────────────┐
              │ Worker (Container  │   lease job → run workflow → record result
              │ Apps, KEDA-scaled  │   heartbeats; cancels stale revisions
              │ per queue)         │
              └──┬───────┬───────┬─┘
                 │       │       │
          Review      Security   Incident
          workflow    workflow   workflow
                 │       │       │
        ┌────────┴───────┴───────┴────────┐
        │  Core services (in-process libs)│
        │  LLM gateway · Tool registry ·  │
        │  Policy engine · Redactor ·     │
        │  Budget meter · Prompt registry │
        └────────┬───────────────┬────────┘
                 ▼               ▼
         Azure OpenAI       Integrations: GitHub, Semgrep/gitleaks
         (Entra auth)       sandbox, Azure Automation, Log Analytics,
                            ARM (read-only), ITSM/Issues

 State: PostgreSQL (jobs, runs, findings, audit, memory)  ·  Blob (artifacts)
 Secrets: Key Vault (GitHub App key, webhook secret only)
 Telemetry: OpenTelemetry → App Insights / Log Analytics
```

**Why these choices**

- **PostgreSQL instead of Cosmos + Redis.** Idempotency, leases, job state and audit need
  transactions and unique constraints. One datastore removes a failure mode (the old fail-open
  Redis claim). Use `pgvector` later if retrieval memory is needed.
- **Service Bus sessions.** Guarantees serial processing per PR/incident and supports
  dead-lettering natively.
- **Container Apps + KEDA.** Scale each worker pool on its own queue depth (scale to zero).
- **GitHub App only** (no PATs): per-installation tokens, repo-scoped permissions.

## 4. Core domain model

```
Event        (source, delivery_id, type, payload_ref, received_at)
Job          (id, agent, target_key, revision, status, attempts, lease_until, config_snapshot)
Run          (job_id, agent_version, prompt_version, model, tokens, cost, started/finished)
Finding      (run_id, kind, severity, path, line, evidence, fingerprint, state)
Action       (run_id, type, tier, params, policy_decision, approved_by, result, verified)
AuditLog     (append-only: who/what/why for every side effect)
```

- **Idempotency key:** `agent:target:revision:agent_version` (e.g. `review:org/repo#42:<head_sha>:v3`).
  A new push → new revision → new job; the superseded job is cancelled. Same event twice → no-op.
- **Job states:** `queued → leased → running → succeeded | failed(retryable) | dead | cancelled | superseded`.
  Lease expiry returns crashed jobs to `queued`. Success is recorded in the same transaction as the output pointer.
- **Finding fingerprint** (rule + path + normalised code hash) lets us dedupe across pushes and avoid
  re-posting comments that were already resolved or dismissed.

## 5. Shared platform services

### 5.1 LLM gateway
- Single interface; **structured outputs against JSON Schema** (no regex JSON repair), schema-validated with Pydantic.
- **Model routing** by task (small model for classification/summaries, large for reasoning); fallback on failure.
- Retries with jittered backoff, timeouts, circuit breaker, **per-repo and per-day token/cost budgets**
  (hard stop + alert), response cache keyed on `hash(prompt_version, inputs)`.
- **Prompt registry:** prompts are versioned files (`prompts/<agent>/<name>.v3.md`) with metadata; version is stored on each Run.
- **Pre-flight redaction:** secrets/PII scrubbed (gitleaks patterns) before anything leaves the service.
- Auth via Entra managed identity; no API keys.

### 5.2 Tool registry (for agentic steps)
Tools are typed, each declared with `risk_tier` (`read`, `write_low`, `write_high`) and required scopes.
Review/security agents are **bound to `read` tools only**. Only the incident workflow can propose
`write_*` actions, and only through the policy engine (§7.3).

### 5.3 Policy engine
Deterministic rules (code + data, e.g. OPA/Cedar or a small rules module) evaluated on every Action.
Inputs: action type, target environment, severity, time window, recent action count, approval state,
evidence checks. Output: `allow | require_approval | deny` + reason (logged).

### 5.4 Per-repo configuration
`.ai-agents.yml` in the repo (validated, schema published):
```yaml
version: 1
review:   { enabled: true, mode: shadow|comment|request_changes, paths_ignore: ["**/generated/**"], max_comments: 15 }
security: { enabled: true, rulesets: [semgrep:p/python, gitleaks], fail_on: [critical] }
tests:    { enabled: true, propose_as: suggestion|pull_request, framework: pytest }
```
Service-level limits (budgets, model names, thresholds) live in typed `Settings`, loaded once at startup.

## 6. Observability, safety, and cost

- **Tracing:** OpenTelemetry with GenAI semantic conventions; one trace per Job spanning ingress → worker → tool calls.
- **Metrics:** job latency/queue age, success/dead-letter rate, tokens & cost per agent/repo, comment acceptance rate,
  false-positive rate, remediation success rate, MTTR delta.
- **Dashboards & alerts:** DLQ depth, budget burn, error spikes, p95 review latency.
- **Security of the system itself:** HMAC verification mandatory (fail closed), Entra auth on non-GitHub endpoints,
  CORS closed by default, rate limiting, SSRF-safe fetchers, sandboxed execution of generated tests
  (no network, read-only repo mount, CPU/time limits), SBOM + dependency/image scanning in CI.
- **Prompt-injection defence:** delimit untrusted content, never include instructions from it, read-only tools,
  output only through schema, allowlisted comment targets, no secrets in the model context.
- **Kill switches:** global and per-agent/per-repo disable flags checked at job start and before every Action.

## 7. Agent workflows

### 7.1 Review agent
```
PR event ─► fetch diff+files @head_sha (App token)
        ─► filter (ignored paths, binaries, lockfiles, generated)
        ─► chunk by file/hunk with token budget; add limited repo context
              (changed symbols' definitions, CODEOWNERS, repo review guide, past dismissed findings)
        ─► Pass 1: per-chunk issue discovery (structured)
        ─► Pass 2: dedupe, rank, verify each candidate against the actual hunk (line must exist in diff)
        ─► map to valid diff positions; drop/convert ungroundable items to summary notes
        ─► post ONE review (or update previous bot review); resolve stale threads on new push
        ─► record Findings + feedback hooks (👍/👎, resolved/dismissed)
```
Key properties: grounded line numbers (no 422s), no silent truncation (reports “reviewed N of M files”),
capped comment count sorted by severity×confidence, learns from dismissals via fingerprints.

### 7.2 Security & test agent
```
PR event ─► checkout head_sha in sandbox (shallow)
        ─► run deterministic scanners on FULL files: Semgrep, gitleaks, dependency audit, IaC (Checkov)
        ─► filter to findings touching changed lines (+ new secrets anywhere)
        ─► LLM triage (is it reachable/exploitable here?) + explanation + fix (per-finding, keyed by fingerprint)
        ─► coverage: real coverage data (diff-cover) instead of name matching
        ─► test generation for uncovered changed functions ► RUN in sandbox ► keep only passing, meaningful tests
        ─► output: check run with annotations (SARIF), summary comment, tests as suggestion or a follow-up PR branch
```
Blocking policy is expressed via a GitHub **Check Run** conclusion driven by `fail_on` config, not by prose.

### 7.3 Incident (IT Ops) agent
```
Alert ─► normalise (Azure Monitor common schema) ─► dedupe/correlate with open incident
      ─► gather EVIDENCE via read-only tools: Log Analytics (KQL templates), metrics, recent deployments,
         Activity Log, runbook history, similar past incidents (retrieval)
      ─► LLM hypothesis ranking with cited evidence ─► candidate remediation from RUNBOOK REGISTRY
      ─► Policy engine decision
            Tier 0 (diagnostic: collect more data)             → auto
            Tier 1 (reversible, low blast: restart, scale +1)  → auto if policy gates pass
            Tier 2 (state-changing: failover, rotate, drain)   → human approval (Teams/Slack/GitHub)
            Tier 3 (destructive/unknown)                        → deny; ticket only
      ─► execute via Azure Automation (typed params) ─► VERIFY (alert clears / health query passes within T)
      ─► on failed verification: rollback if defined, escalate, page
      ─► incident timeline + ticket + audit log; post-incident summary
```
**Runbook registry** (`runbooks/<name>/runbook.yaml` + script): name, tier, JSON-schema params,
allowed environments, preconditions, verification query, rollback, cooldown, max executions/hour.
LLM may only select a registered runbook and fill schema-valid params; unknown/invalid → ticket.

**Safety controls:** per-resource cooldowns and rate limits, change-freeze windows, production requires
approval until the agent’s measured precision (from shadow-mode history) exceeds a threshold, global kill switch,
full audit trail. “Confidence” is calibrated against historical outcomes; until then it never authorises alone.

## 8. Evaluation strategy

| Layer | What | Gate |
|---|---|---|
| Unit | Pure logic: diff mapping, policy rules, schema validation, idempotency | CI required |
| Contract | GitHub/Azure API fakes (recorded fixtures) | CI required |
| Integration | Testcontainers: Postgres + Service Bus emulator, sandbox runner | CI required |
| **LLM evals** | Golden sets: PRs with known bugs/vulns, incident logs with known root cause; metrics: precision, recall, grounding rate, cost | Regression threshold blocks prompt/model changes |
| Shadow mode | Agent runs, output logged not posted | Exit criterion before enabling per repo |
| Online | Comment acceptance, dismissals, remediation success | Dashboard + weekly review |

Prompt or model changes go through the same PR flow and must pass the eval suite.

## 9. Repository layout (monorepo, `src` layout)

```
ai-devops-agents/
├── pyproject.toml                  # uv-managed; workspace; ruff, mypy --strict, import-linter config
├── uv.lock
├── Makefile                        # make dev | test | eval | lint | up | plan
├── README.md                       # 1-page overview + links
├── CLAUDE.md / AGENTS.md           # contributor + AI-assistant conventions
├── LICENSE  SECURITY.md  CONTRIBUTING.md  CODEOWNERS
│
├── src/devopsagents/
│   ├── domain/                     # pure models & rules; no I/O imports
│   │   ├── events.py  jobs.py  findings.py  actions.py  policy_types.py
│   ├── core/                       # platform services (depend on domain only)
│   │   ├── llm/        gateway.py  routing.py  budget.py  cache.py  redact.py  schemas.py
│   │   ├── prompts/    registry.py            # loads versioned prompt files
│   │   ├── policy/     engine.py  rules/      # deterministic gating
│   │   ├── tools/      registry.py  base.py   # typed tools with risk tiers
│   │   ├── jobs/       store.py  lease.py  idempotency.py  runner.py
│   │   ├── config/     settings.py  repo_config.py  schemas/   # typed + published JSON schema
│   │   └── telemetry/  tracing.py  metrics.py  logging.py
│   ├── integrations/               # adapters behind interfaces (ports)
│   │   ├── github/     app_auth.py  diff.py  reviews.py  checks.py  comments.py
│   │   ├── azure/      openai.py  servicebus.py  automation.py  loganalytics.py  arm_readonly.py
│   │   ├── scanners/   semgrep.py  gitleaks.py  checkov.py  diffcover.py
│   │   ├── sandbox/    runner.py                # isolated test execution
│   │   └── notify/     teams.py  github_issues.py
│   ├── agents/
│   │   ├── review/     workflow.py  chunking.py  grounding.py  ranking.py  publish.py
│   │   ├── security/   workflow.py  triage.py  testgen.py  sarif.py
│   │   └── incident/   workflow.py  evidence.py  hypotheses.py  remediation.py  verify.py
│   └── apps/
│       ├── api/        main.py  routes/{github,azure_monitor,admin,health}.py  auth.py
│       ├── worker/     main.py  consumers.py
│       └── cli/        main.py                  # trigger, replay-event, run-eval, rollback
│
├── prompts/                        # versioned prompt files, reviewed like code
│   ├── review/  security/  incident/
├── runbooks/                       # registry: <name>/{runbook.yaml, run.ps1|py, tests/}
│   ├── restart-service/  scale-out/  clear-disk/  rotate-credentials/  flush-cache/ ...
├── policies/                       # policy-as-data (tiers, freeze windows, rate limits)
├── schemas/                        # JSON Schemas: repo config, LLM outputs, runbook.yaml
│
├── evals/
│   ├── datasets/                   # review/, security/, incident/ golden cases (+ provenance)
│   ├── graders/                    # deterministic + LLM-as-judge (judge calibrated)
│   ├── run_evals.py  report.py
│
├── tests/
│   ├── unit/  contract/  integration/  e2e/  fixtures/ (recorded GitHub/Azure payloads)
│
├── infra/
│   ├── terraform/
│   │   ├── modules/  network/ postgres/ servicebus/ openai/ keyvault/ containerapps/ monitoring/ identity/ automation/
│   │   ├── envs/     dev/ staging/ prod/   # tfvars + backend per env
│   │   └── versions.tf  (azurerm ~> 4.x, pinned)
│   └── container/  Dockerfile  (one image, multiple entrypoints: api | worker | cli)
│
├── .github/
│   ├── workflows/  ci.yml  evals.yml  infra-plan.yml  deploy.yml  security.yml  release.yml
│   ├── actions/    setup-python-uv/            # composite actions
│   └── dependabot.yml  ISSUE_TEMPLATE/  PULL_REQUEST_TEMPLATE.md
│
└── docs/
    ├── architecture/  overview.md  data-model.md  threat-model.md  sequence-diagrams/
    ├── adr/           0001-postgres-over-cosmos.md  0002-policy-gated-remediation.md ...
    ├── runbooks-ops/  on-call.md  dlq-handling.md  kill-switch.md  rollback.md
    ├── guides/        onboarding-a-repo.md  writing-a-runbook.md  writing-a-prompt.md
    └── slo.md
```

**Layering rules (enforced by import-linter in CI):**
`domain` ← `core` ← `integrations` ← `agents` ← `apps`. Agents depend on interfaces from `core`, never on SDKs directly; SDK
imports live only in `integrations/`. No import-time side effects (no clients or settings built at import).

**Tooling:** `uv`, `ruff` (lint+format), `mypy --strict`, `pytest` + `hypothesis`, `testcontainers`, `import-linter`,
`pre-commit`, `pip-audit`, `trivy`, `semgrep` (on this repo too), Renovate/Dependabot.

## 10. Infrastructure & delivery

| Concern | Decision |
|---|---|
| Compute | Azure Container Apps: `api` (min 2 replicas, external ingress), `worker-review`, `worker-security`, `worker-incident` (KEDA per queue, scale to zero except incident min 1) |
| State | PostgreSQL Flexible Server (zone-redundant in prod, PITR on); Blob for artifacts/SARIF |
| Messaging | Service Bus Standard/Premium; sessions enabled; DLQ alarms |
| AI | Azure OpenAI, Entra auth, content filters on, quota per deployment; PTU only if volume justifies |
| Secrets | Key Vault for GitHub App private key + webhook secret; everything else managed identity |
| Network | Prod: VNet integration, private endpoints for Postgres/KV/Service Bus; WAF/Front Door in front of API |
| IaC | Terraform modules + per-env directories; plan on PR, apply on merge with environment approvals; state in Azure Storage with locking |
| CI | lint, types, unit/contract/integration, import-lint, security scans, image build + scan + SBOM, evals on prompt/model changes |
| CD | GitHub OIDC → Azure (no stored credentials); staging auto, prod gated; migrations (Alembic) run as a pre-deploy job; blue/green via revisions; automatic rollback on failed health/SLO check |
| Environments | `dev` (cheap, shared), `staging` (mirrors prod, test GitHub org + test Azure Monitor), `prod` |

## 11. SLOs and targets (initial)

- Review posted within **3 min p95** for PRs < 800 changed lines.
- Webhook ack **< 200 ms p99**; **zero** lost events (DLQ reviewed < 1 business day).
- Reviewer comment acceptance **≥ 50%** (resolved/👍) before enabling `request_changes` mode.
- Security triage false-positive rate **< 20%** on surfaced findings.
- Incident evidence summary in **< 2 min** from alert; auto-remediation verification success **≥ 95%** before widening tiers.
- Cost: tracked per PR/incident; budget alarms at 80% of monthly cap.

## 12. Delivery plan

| Phase | Weeks | Deliverables | Exit criteria |
|---|---|---|---|
| **0. Foundations** | 1–2 | Repo scaffold, CI, typed settings, Postgres + job store (lease/idempotency), ingress API with HMAC, Service Bus, telemetry skeleton, Terraform `dev` | Replayed webhook yields exactly one completed job; kill -9 worker recovers |
| **1. LLM platform** | 3–4 | Gateway (structured outputs, budgets, cache, redaction), prompt registry, eval harness skeleton | Budget cap enforced in test; eval runs in CI |
| **2. Review agent (shadow)** | 5–7 | Chunking, grounding, ranking, publish, thread resolution; shadow on 2–3 internal repos | Grounding rate > 98%; acceptance measured on ≥ 100 comments |
| **3. Review agent (live)** | 8 | Enable `comment` mode, feedback capture, per-repo config | Acceptance ≥ 50%, p95 latency met |
| **4. Security & tests** | 9–12 | Scanner integration, triage, SARIF/Check Run, diff-cover, sandboxed test generation | FP rate < 20%; generated tests run green before posting |
| **5. Incident agent (read-only)** | 13–16 | Alert normalisation, evidence tools, hypotheses, ticketing, retrieval of past incidents | Root-cause top-3 hit rate ≥ 70% on labelled history |
| **6. Remediation (gated)** | 17–20 | Runbook registry, policy engine, approvals, verification/rollback, audit | Tier-1 only in staging, then prod after shadow precision threshold |
| **7. Hardening** | 21–22 | Threat-model review, pen-test of prompt injection, load test, DR drill, runbooks-ops docs | All SLOs met; sign-off |

Sequence rationale: platform before agents; read-only before write; shadow before live. Each phase ships independently.

## 13. Top risks and mitigations

| Risk | Mitigation |
|---|---|
| Noisy reviews erode trust | Shadow mode, comment cap, fingerprint-based suppression, acceptance KPI gating |
| Prompt injection via PR/logs | Read-only tools for review/security, schema-only outputs, policy-gated actions, red-team evals |
| Wrong auto-remediation | Tiering, evidence requirement, verification + rollback, cooldowns, human approval default for prod |
| Cost overrun | Budgets, caching, small-model routing, chunk filtering, alarms |
| Model/API drift | Pinned versions, eval gate on changes, fallback deployment |
| GitHub/Azure quota or outage | Queue buffering, backoff, DLQ with replay CLI |
| Data leakage to model | Redaction, repo allowlist, enterprise Azure OpenAI terms, no training on data |

## 14. Open decisions (to settle in ADRs before Phase 0 ends)

1. Postgres vs Cosmos (recommended: Postgres; revisit only if multi-region writes are required).
2. Policy engine: embedded Python rules vs OPA/Cedar (start embedded; keep rules as data).
3. Approvals channel for Tier 2: GitHub Environments vs Teams adaptive cards.
4. SaaS scanner (GitHub Advanced Security/CodeQL) vs self-run Semgrep — depends on licensing.
5. Single-tenant (one org) vs multi-tenant GitHub App — drives data-isolation design.
