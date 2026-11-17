# ADR-0002: Remediation is decided by a deterministic policy engine, not by the model

**Status:** accepted

**Context.** An LLM's self-reported confidence is uncalibrated and can be manipulated by attacker-controlled log text.

**Decision.** The model may only *propose* a runbook from a registry with typed, schema-validated parameters
(`additionalProperties: false`). `PolicyEngine.evaluate` returns allow / require_approval / deny from: kill switch,
registry membership, parameter validity, environment allow-list, risk tier, evidence, cooldown, per-hour rate limit,
freeze windows and a confidence floor. Confidence can only make the outcome stricter. Human approval can lift
`require_approval` but never `deny`. Every execution is audited before it happens.

**Consequences.** Prod automation is off until explicitly enabled by policy and measured precision. Adding a capability
means adding a reviewed runbook definition, not changing prompts alone.
