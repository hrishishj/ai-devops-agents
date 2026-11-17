# On-call guide

## Kill switches
- **Global:** set `DA_KILL_SWITCH=true` on api + workers (new revision). LLM calls and all actions stop immediately.
- **Policy-only:** set `kill_switch: true` in `policies/default.yaml` (denies all remediation, keeps analysis).
- **Per repo:** `review: {mode: "off"}` / `security: {enabled: false}` in `.ai-agents.yml`.

## Dead jobs
`GET /health/ready` shows `dead_jobs`. Inspect: `GET /admin/jobs/{id}` (header `x-admin-token`) → `error`, `attempts`.
Most are stale revisions (expected) or exhausted retries (model/GitHub outage). Re-run by re-pushing or
`devopsagents review --repo org/r --pr N --installation ID`.

## Stuck "leased" jobs
Leases expire (default 5 min; incident 20 min) and workers re-claim them automatically.

## LLM budget exhausted
Jobs fail with `BudgetExceeded` and dead-letter. Raise `DA_DAILY_TOKEN_BUDGET_*` only after checking for runaway repos in
the `llm_usage` table.

## Approving a remediation
`POST /admin/incidents/{target}/approve` with `{"approver": "<you>"}`. The policy is re-evaluated; cooldown, rate limit,
kill switch and parameter checks still apply. Review the audit trail in `audit_log` (`approval.*`, `action.*`).

## Migrations
Schema changes: add an Alembic revision under `src/devopsagents/migrations/versions/` (the test `test_migrations` fails if models
and migrations drift). Deploys run the `caj-*-migrate` job automatically; to run by hand:
`az containerapp job start -n <job> -g <rg>` or locally `devopsagents migrate` with `DA_DATABASE_URL` set.
Keep migrations additive (expand/contract) because old and new revisions overlap during a deploy.

## Enabling deployment
The optional Azure deploy workflow (`deploy-azure.yml`) is skipped until `AZURE_CLIENT_ID` is set as a repository variable. To enable it, configure GitHub OIDC
federation for an Azure app registration, then set:

- **Variables:** `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID`, `ACR_NAME`, `ACR_LOGIN_SERVER`, `ACR_ID`,
  `GH_APP_ID`, `TFSTATE_RG`, `TFSTATE_STORAGE`
- **Secrets:** `GH_APP_PRIVATE_KEY`, `GH_WEBHOOK_SECRET`, `ALERT_TOKEN`, `ADMIN_TOKEN`
- **Environments:** create `staging` and `prod`; add required reviewers on `prod`.
