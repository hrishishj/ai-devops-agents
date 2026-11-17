# Free incident response (no Azure)

The incident agent works with free, self-hosted tools:

| Job | Free option |
|---|---|
| Alert source | **Prometheus Alertmanager** webhook |
| Evidence | **Prometheus** HTTP API (fixed PromQL templates; label values validated) |
| Run a runbook | **GitHub Actions** `workflow_dispatch` (free for public repos; 2,000 min/month private; self-hosted runners are free) |
| Verify recovery | **Prometheus** (the alert is no longer in the `ALERTS` firing series) |
| Tickets | **GitHub Issues** |
| Approvals | the approver's own **GitHub login** (verified with GitHub; no identity provider to run) |

The safety model is unchanged: the model only proposes a **registered runbook** with schema-validated parameters, and the policy
engine decides (allow / needs human / deny). See `policies/default.yaml` and `runbooks/`.

## 1. Point Alertmanager at the API
```yaml
route:
  receiver: ai-agents
receivers:
  - name: ai-agents
    webhook_configs:
      - url: http://<api-host>:8000/webhooks/alertmanager
        send_resolved: false
        http_config:
          authorization:
            type: Bearer
            credentials: <value of DA_ALERT_TOKEN>
```
Set `DA_ALERT_TOKEN` (an empty token makes the endpoint reject everything). Only **firing** alerts are handled. Set an
`environment` (or `env`) label on your alerts: **alerts without it are treated as `prod`**, the strictest policy.

## 2. Give it evidence and a recovery check
`DA_PROMETHEUS_URL=http://prometheus:9090` (add `DA_PROMETHEUS_TOKEN` if yours needs a bearer token). Evidence queries: whether the
alert is firing, `up`, `process_cpu_seconds_total` rate and `process_resident_memory_bytes` for the alert's `instance`. Missing
metrics simply produce no evidence; nothing is invented. After a runbook runs, the agent only reports **recovered** if the alert
is gone from Prometheus; otherwise it **escalates** to a human.

## 3. Run runbooks with GitHub Actions
1. In an ops repository, copy [`docs/templates/runbook.yml`](templates/runbook.yml) to `.github/workflows/runbook.yml`.
2. Add scripts at `runbook-scripts/<Name>.sh`, where `<Name>` is the `automation_runbook` in `runbooks/<runbook>/runbook.yaml`
   (for example `Restart-Service.sh`). Parameters arrive as JSON in `$PARAMS`. A missing script makes the run fail, never succeed.
3. Configure the agents: `DA_RUNBOOK_REPO=owner/ops-repo` (plus `DA_RUNBOOK_WORKFLOW`, `DA_RUNBOOK_REF` if not the defaults).
4. `DA_GITHUB_TOKEN` needs **Actions: read and write** on that repo (and Issues write on `DA_INCIDENT_REPO` for tickets).
5. Optional gate: create an Environment named `runbooks` with required reviewers (free on public repos; paid plan for private).
   The agent waits up to ~10 minutes for the run; a run still waiting for approval is reported as not completed and a ticket is filed.
6. To reach your own machines, use a self-hosted runner (free).

## 4. Approving actions that need a human
Set `DA_ADMIN_AUTH=github`, `DA_INCIDENT_REPO=owner/repo` (or `DA_APPROVER_REPO`) and optionally `DA_APPROVERS=alice,bob`.
An approver proves who they are with their own GitHub token; they must have **write access** to the repo (and be in the allow-list
if one is set):
```bash
curl -X POST -H "Authorization: Bearer $(gh auth token)" http://127.0.0.1:8000/admin/incidents/<incident-target>/approve
```
The audit log records the verified GitHub login (`verified_identity: true`); any approver name in the request body is ignored. The
policy is re-checked on approval: an approval can lift "needs a human" but never a denial (cooldown, bad parameters, kill switch).

## Honest limits
- Not run against a real Prometheus, Alertmanager or GitHub Actions workflow: all adapters are tested with mock transports and a
  scripted model. Try it in a non-production environment first, with `auto_environments` left at dev/staging.
- GitHub-hosted runners cannot reach private infrastructure; use a self-hosted runner for that.
- The GitHub token you give the agent is powerful (it can dispatch workflows). Use a fine-grained token limited to the ops repo.
- Metrics like `process_cpu_seconds_total` exist only if the target exposes them; add your own PromQL templates in
  `integrations/prometheus/client.py` for other exporters.
