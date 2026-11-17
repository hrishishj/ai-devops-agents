# Threat model (summary)

| Threat | Mitigation | Residual risk |
|---|---|---|
| Forged webhooks | HMAC-SHA256, fail closed when secret unset; shared-token auth on alert/admin endpoints | Shared tokens → move to Entra (ADR-0004) |
| Prompt injection via PR/diff/logs | Untrusted-data delimiters; review & security have read-only capabilities; output only via schema; grounding drops hallucinated targets; policy gates all actions | Model may still be misled into low-quality comments — bounded, no side effects |
| Malicious/oversized PR (cost) | Chunk budget, `max_chunks`, per-repo + global daily token budgets, output cap | Budget exhaustion = denial of review for that repo |
| Secret exfiltration to the model | Redaction in the LLM gateway; scanner output masks matched values | Novel secret formats not matched by patterns |
| Unsafe remediation | Registry-only runbooks, typed params, tiers, evidence, cooldown/rate limit, approval for stateful/prod, audit-before-act, kill switch | Wrong-but-allowed tier-1 action in non-prod |
| Stale/duplicate work | Idempotent keys, supersede, `is_current` check before side effects | — |
| Semgrep on untrusted code | Throw-away dir, no shell, timeout, metrics off, path-traversal guard; deploy workers with restricted egress | Scanner vulnerabilities |
| Credential theft | Managed identity, Entra-only OpenAI/Service Bus, Key Vault via RBAC, GitHub OIDC deploy | Postgres admin password lives in Key Vault (move to Entra auth) |
