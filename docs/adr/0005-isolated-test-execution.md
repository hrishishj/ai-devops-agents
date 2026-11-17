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

## Addendum: free Docker adapter
`DockerSandbox` (`DA_SANDBOX_KIND=docker`) runs each job in a throw-away container: no network, read-only root with a small
tmpfs, all capabilities dropped, no-new-privileges, non-root user, memory/CPU/PID limits, killed on timeout. **No host mounts and no
Docker socket**: the files go in over stdin and the result comes back on stdout. A test-only suite (`tests/integration/
test_docker_sandbox.py`) runs real containers to prove these properties, and CI builds the image so it runs there.

This is container-grade isolation, not VM-grade. Use it for your own repositories and for ephemeral CI runners (the GitHub Actions
workflow). Do not use it for code from strangers, and do not mount the Docker socket into a long-lived service container (for
example the Compose worker) to give it access: that is effectively host root. Run the agents on the host or an ephemeral runner.

## Addendum: coverage as a quality gate
A test that passes is not necessarily a test that checks anything (`assert True` passes). The in-sandbox runner now runs pytest under `coverage` (when the
package is in the image) and reports which lines of the source file executed. The generator computes, per target function, the percentage of its
*body* statements that ran (the `def` line is ignored: it executes at import). Generated tests are dropped if they never execute any target
function, or if the average falls below `tests.min_coverage` (default 0 = only the first rule). If the image lacks `coverage`, tests are kept and
the PR comment says coverage was not measured (a threshold cannot be enforced on something unmeasured). This measures the PR version of one file,
not repository-wide coverage.
