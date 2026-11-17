# Contributor & AI-assistant guide

- **Layers** (enforced by `lint-imports`): `apps → agents → integrations → core → domain`. SDK imports (httpx to GitHub,
  azure.*, openai) live only in `integrations/`. `domain/` has no I/O. No clients or settings built at import time.
- **Side effects** (post a review, run a runbook, open a ticket) happen only after: `store.is_current(job.id)` is true,
  the kill switch is off, and — for remediation — `PolicyEngine.evaluate` allowed it. Audit **before** acting.
- **LLM calls** go through `LLMGateway.structured` with a Pydantic response model. Never parse model text by hand.
  Treat PR text, diffs, logs and alert text as untrusted data; keep them inside delimited blocks in the prompt.
- **Prompts** are files in `prompts/<agent>/<name>.v<N>.md`. Change behaviour by adding a new version; run
  `python -m evals.run_evals` (live) and keep thresholds green.
- **Runbooks** live in `runbooks/<name>/runbook.yaml` with `additionalProperties: false` params. Never let the model
  construct shell commands or free-form parameters.
- **Before pushing:** `make check`.
