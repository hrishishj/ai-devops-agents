# Observability (free)

## Metrics endpoint
`GET /metrics` returns Prometheus text format. It is **disabled (404) until `DA_METRICS_TOKEN` is set**, then requires
`Authorization: Bearer <token>`. Metrics are read from the database, so the API reports what every worker has done.

| Metric | Meaning |
|---|---|
| `devopsagents_jobs{agent,status}` | jobs by agent and status (all combinations are always present, zero-filled) |
| `devopsagents_oldest_queued_job_age_seconds{agent}` | age of the oldest job still waiting |
| `devopsagents_audit_events_total{event}` | side effects performed (`review.posted`, `action.executed`, `approval.requested`, ...) |
| `devopsagents_llm_tokens_today{scope}` | tokens used today (UTC): `global` plus the top 10 repositories |
| `devopsagents_kill_switch` | 1 when the global kill switch is on |
| `devopsagents_build_info{version}` | version |

## Local dashboards and alerts (Docker Compose)
```bash
# .env: DA_METRICS_TOKEN=<random>   DA_ALERT_TOKEN=<random>   DA_PROMETHEUS_URL=http://prometheus:9090
docker compose --profile observability up -d
```
- **Prometheus** `http://127.0.0.1:9090` scrapes the API every 30s and evaluates `infra/observability/alerts.yml`.
- **Alertmanager** `http://127.0.0.1:9093` forwards alerts to the agents' webhook. Add your own receiver (email, chat) in
  `infra/observability/alertmanager.yml` so a human also hears about problems.
- **Grafana** `http://127.0.0.1:3000` (user `admin`, password `GRAFANA_PASSWORD`, default `admin` - change it) with an "AI DevOps
  Agents" dashboard: jobs by status, oldest waiting job, actions taken, LLM tokens, dead jobs, kill switch.

Alerts about the agents themselves (`AgentsApiDown`, `NewDeadJobs`, `JobsStuckInQueue`, `KillSwitchEnabled`) carry
`environment: local`, which no runbook allows, so the incident agent can only open a ticket for them, never run an action. Setting
`DA_PROMETHEUS_URL` also lets the incident agent read evidence and verify recovery from this Prometheus.

## Scraping from your own Prometheus
```yaml
scrape_configs:
  - job_name: devopsagents
    authorization: { type: Bearer, credentials: <DA_METRICS_TOKEN> }   # or credentials_file
    static_configs: [{ targets: ["<api-host>:8000"] }]
```

## Honest limits
- The Compose observability configs (Prometheus, Alertmanager, Grafana) are syntax-checked but have not been started: no Docker
  daemon was available. Alert expressions were not run through `promtool`.
- Metrics are database aggregates refreshed per scrape (cheap at this scale; revisit if the audit table grows to millions of rows).
- No request-level latency histograms or traces yet; logs are structured JSON. OpenTelemetry export to Application Insights is
  available only with the Azure extra.
