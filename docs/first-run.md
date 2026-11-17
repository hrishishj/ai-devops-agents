# First run: try it on a real pull request

Goal: see the agents work on real code, judge the quality honestly, and only then let them comment. Pick **one** path.
Everything starts in **shadow mode**: the agents run and report what they *would* say, but post nothing to the pull request.

| Path | Best for | Needs |
|---|---|---|
| **A. GitHub Actions + a free hosted model (Groq)** | no local setup | a GitHub repo, a free Groq account |
| **B. Your machine + Ollama** | private code, full control | Python 3.12, ~8 GB free RAM for a 7B model |
| **C. Docker Compose** | an always-on local stack | Docker; see [local-docker.md](local-docker.md) |

## Path A: GitHub Actions + Groq (free hosted model)
> GitHub Models, which earlier versions of this guide used, was **retired on 2026-07-30**. Groq's free plan is the simplest hosted replacement.
> Hosted free tiers may keep your prompts: use a repo whose code you are happy to share, or use **Path B (Ollama)** for private code.

1. **Get a key.** Create a free account at https://console.groq.com, open **API Keys**, create a key and copy it (it is shown once).
2. Use a **test repository** (a copy of a real project is ideal). Pull requests must come from branches **in the same repo** (not forks).
3. Copy [`.github/workflows/ai-review.yml`](../.github/workflows/ai-review.yml) into the test repo (skip this if you are using this repo).
4. **Settings -> Secrets and variables -> Actions**:
   - **Secrets** tab -> New repository secret: `AI_LLM_API_KEY` = your Groq key
   - **Variables** tab -> `AI_LLM_BASE_URL` = `https://api.groq.com/openai/v1`
   - **Variables** tab -> `AI_LLM_MODEL_LARGE` = `llama-3.3-70b-versatile`
5. Open a small pull request (a few files, one real change).
6. **Actions** tab -> the *AI review* run -> **Summary**. You should see counts for candidates, grounded findings and the coverage line.
7. Nothing appears on the PR itself. That is shadow mode, and it is correct.

Groq's free plan limits tokens per minute, so a first run may show a rate-limit retry (it waits and retries by itself). If a run fails with `HTTP 400` mentioning
`response_format`, add the variable `AI_LLM_JSON_MODE` = `false`.

## Path B: your machine + Ollama
```bash
ollama pull qwen2.5-coder:7b
export OLLAMA_CONTEXT_LENGTH=16384            # Ollama's default context silently truncates long prompts
pip install "git+https://github.com/hrishishj/ai-devops-agents@main"
export DA_GITHUB_TOKEN=<token that can read the PR>      # fine-grained: Pull requests (read), Contents (read)
export DA_LLM_BASE_URL=http://localhost:11434/v1 DA_LLM_MODEL_LARGE=qwen2.5-coder:7b DA_REVIEW_CHUNK_TOKENS=2500
devopsagents run-pr --repo owner/name --pr 42
```
The same summary is printed to the terminal. A 7B model on CPU can take several minutes per PR; that is normal.

## What good output looks like
- `status shadow`, `candidates` > 0 on a PR with real changes, and `grounded` close to `candidates`. Comments the model aims at lines
  that are not in the diff are **dropped** (`dropped (not in diff)`); a few is normal, most of them means the model is weak.
- Each listed finding shows `path:line`, a severity and a title. Open the file at that line: is it a real problem?
- On a trivial or clean PR the review should return **no findings**. A review that always finds something is noise.

## Judge it before you trust it (the actual test)
Run shadow mode on **at least 10 real PRs**, then score every finding in a table:

| PR | finding | real issue? (yes / partly / no) | right line? | would you act on it? |
|---|---|---|---|---|

Targets from [slo.md](slo.md): grounded >= 98% of candidates, **at least half** of findings useful. If it falls short: try a larger model, lower
`AI_CHUNK_TOKENS`, add `paths_ignore` for generated code, or tune the prompt in `prompts/review/` and re-score. Free small models will need the most tuning.
Also run `python -m evals.run_evals` (live) to compare models on the built-in cases.

## Turn comments on (one repo only)
In that repo add `.ai-agents.yml`:
```yaml
version: 1
review:   { mode: comment, max_comments: 8 }
```
Open a PR and check the review lands correctly. React with a 👎 on a bad comment or resolve the thread: the next run stops raising it.

## Optional: security scan and generated tests
- The **security** agent runs automatically (secrets scanner; Semgrep only if installed) and reports as a **Check Run**. Plant a fake
  `AKIAIOSFODNN7EXAMPLE` in a test branch to see it fire.
- Verified test generation is opt-in: `tests: {enabled: true}` plus a Docker sandbox. See [free-setup.md](free-setup.md).

## Optional: try the incident path
With the stack running ([local-docker.md](local-docker.md)) and `DA_ALERT_TOKEN` set:
```bash
curl -s -X POST http://127.0.0.1:8000/webhooks/alertmanager \
  -H "Authorization: Bearer $DA_ALERT_TOKEN" -H "Content-Type: application/json" \
  -d '{"version":"4","groupKey":"g","alerts":[{"status":"firing","fingerprint":"t1",
       "labels":{"alertname":"HighCPU","instance":"api-1:9100","severity":"critical","environment":"staging"},
       "annotations":{"summary":"CPU above 95% for 10m"},"startsAt":"2026-01-01T00:00:00Z"}]}'
# -> {"status":"accepted","jobs":[{"job_id":"<id>","correlated":false}]}
curl -s -H "x-admin-token: $DA_ADMIN_TOKEN" http://127.0.0.1:8000/admin/jobs/<id>
```
Use `environment: staging` (or dev) for the first try. Alerts without that label are treated as **prod**, where every action waits for a human.
Without a runbook workflow configured nothing is executed: you will see the analysis and a ticket (a log line if no tracker is set).

## When something goes wrong
See [troubleshooting.md](troubleshooting.md). Stop everything instantly with `DA_KILL_SWITCH=true` (restart), or set a repo's
`.ai-agents.yml` to `review: {mode: "off"}`.
