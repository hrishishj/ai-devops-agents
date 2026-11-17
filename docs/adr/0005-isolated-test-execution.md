# ADR-0005: Generated tests run only in an isolated sandbox

**Status:** accepted

**Context.** Verifying generated tests means executing code from a pull request, which is attacker-controlled. The
worker holds a managed identity (Key Vault, Service Bus, OpenAI, Automation), so running PR code there would let a
malicious PR steal those credentials.

**Decision.** Test execution goes through a `Sandbox` port. The shipped adapter targets Azure Container Apps dynamic
sessions (Hyper-V isolated, no managed identity, egress disabled by default). The agent also statically rejects generated
tests that import process/network/filesystem modules or call `eval`/`exec`/`open`. Only tests that pass are reported, and
the surviving subset is re-run once to confirm stability. If no sandbox is configured or it fails, test generation is
skipped; there is deliberately no local-execution fallback, and no local runner ships in `src/`.

**Consequences.** The feature is opt-in per repository and per deployment. Modules are verified in isolation (the PR's
version of one file), so code that imports other repo modules will fail verification and be dropped. A session pool whose
image contains pytest must be provisioned separately; real coverage measurement would require uploading the repo and is
future work.
