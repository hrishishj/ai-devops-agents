# Service-level objectives and rollout gates

| Objective | Target | Measured by |
|---|---|---|
| Webhook acknowledgement | p99 < 200 ms | ingress latency |
| Review posted | p95 < 3 min for PRs < 800 changed lines | job created → `review.posted` audit |
| Lost events | 0 (dead jobs triaged < 1 business day) | `/health/ready` `dead_jobs`, DLQ depth |
| Review grounding rate | ≥ 98% of candidates map to a diff line | `result.grounded / candidates` |
| Review precision (eval) | ≥ 0.80 | `python -m evals.run_evals` |
| Review comment acceptance (online) | ≥ 50% before `comment` mode org-wide | resolved / 👍 vs dismissed |
| Security false-positive rate | < 20% of surfaced findings | dismissed / surfaced |
| Incident runbook accuracy (eval) | ≥ 0.80 | evals |
| Auto-remediation verified success | ≥ 95% before widening `auto_environments` (esp. prod) | `action.verified` audit |

**Rollout gates:** repo starts in `shadow`; move to `comment` after 100 shadow comments meet precision/grounding targets.
`prod` may be added to `auto_environments` only after ≥ 30 days of verified staging results meeting the success target and
a documented sign-off from SRE leads.
