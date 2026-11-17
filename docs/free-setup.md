# Free setup (no paid services)

Everything here costs nothing. Pick one way to run the agents and one LLM.

## Where it runs

| Mode | Needs | Good for |
|---|---|---|
| **GitHub Actions** (`.github/workflows/ai-review.yml`) | a GitHub repo (public = unlimited minutes; private = 2,000 free min/month) | zero hosting; runs on every PR |
| **Your machine** (`devopsagents run-pr ...`) | Python 3.12 + an LLM | trying it out, private code |

In Actions mode the agents authenticate with the built-in `GITHUB_TOKEN`; no GitHub App is required. They read the diff and
files through the GitHub API, so **pull-request code is never checked out or executed** on the runner. Fork PRs are skipped
(GitHub gives them a read-only token).

## Choose an LLM

All of these speak the OpenAI chat-completions protocol, so one setting block works for each.

| Option | `DA_LLM_BASE_URL` | Key | Notes |
|---|---|---|---|
| **Ollama** (local, free, private) | `http://localhost:11434/v1` | none | Best for privacy. Needs a machine with enough RAM/GPU. |
| **GitHub Models** (free tier) | `https://models.github.ai/inference` | a GitHub token with `models: read` (the Actions `GITHUB_TOKEN` works) | Rate-limited; check its terms before sending private code. Model ids look like `openai/gpt-4.1-mini` - confirm the current list in the GitHub Models catalogue. |
| Groq (free tier) | `https://api.groq.com/openai/v1` | Groq API key | Fast; rate-limited; hosted. |
| Gemini (free tier) | `https://generativelanguage.googleapis.com/v1beta/openai` | Google AI key | Free-tier prompts may be used to improve Google products: **do not use for private code**. |

Privacy rule of thumb: for code you would not publish, use **Ollama** (stays on your machine).

### Ollama quick start
```bash
ollama pull qwen2.5-coder:7b          # or a larger coder model if you have the RAM
export OLLAMA_CONTEXT_LENGTH=16384    # Ollama's default context is small and silently truncates long prompts
export DA_LLM_BASE_URL=http://localhost:11434/v1
export DA_LLM_MODEL_LARGE=qwen2.5-coder:7b
export DA_GITHUB_TOKEN=<a GitHub token that can read the PR and comment>
pip install "git+https://github.com/hrishishj/ai-devops-agents@main"
devopsagents run-pr --repo owner/name --pr 42
```
Lower `DA_REVIEW_CHUNK_TOKENS` (e.g. `2500`) if the model has a small context window.

## Turn on the GitHub Actions workflow
In the repository: **Settings -> Secrets and variables -> Actions -> Variables**, add:

| Variable | Example | Required |
|---|---|---|
| `AI_LLM_BASE_URL` | `https://models.github.ai/inference` | yes (its presence turns the workflow on) |
| `AI_LLM_MODEL_LARGE` | `openai/gpt-4.1-mini` | yes |
| `AI_LLM_MODEL_SMALL` | same as above | no |
| `AI_LLM_JSON_MODE` | `false` if the model rejects `response_format` | no |
| `AI_CHUNK_TOKENS` | `4000` | no |
| `AI_AGENTS_SPEC` | `git+https://github.com/hrishishj/ai-devops-agents@<tag-or-sha>` (pin a release for other repos) | no |

Optional secret `AI_LLM_API_KEY` (defaults to `GITHUB_TOKEN`, which is right for GitHub Models only).

To use the workflow in **another** repository, copy `ai-review.yml` into it and set the same variables.

## Optional: verified test generation (free, Docker)
The security agent can propose pytest tests for changed functions, run them in a locked-down Docker container (no network, read-only,
non-root, limited memory/CPU) and post only the tests that pass. Off by default.
1. Set the repository variable `AI_TESTGEN=true` (the workflow then builds the sandbox image on the runner).
2. Opt the repo in with `.ai-agents.yml`: `tests: {enabled: true}`. Optionally `min_coverage: 60` to drop tests that exercise less than 60%
   of the target functions on average; tests that pass without running the target code at all are always dropped.
Locally: `make sandbox-image`, then `DA_SANDBOX_KIND=docker devopsagents run-pr ...`. This is container-grade isolation: fine for
your own repositories and ephemeral runners, not for code from strangers (see ADR-0005).

## What you will see
- **Shadow mode is the default**: nothing is posted to the PR. Open the workflow run -> *Summary* to read what the review
  *would* have said (counts, coverage, findings). When the quality looks good, add `.ai-agents.yml` with
  `review: {mode: comment}` to start posting.
- The security agent posts a **Check Run** (secrets scanner by default; Semgrep only if installed on the runner).

## Honest expectations
- Small free/local models find fewer real bugs and break JSON more often. The gateway retries and repairs output, and
  grounding drops comments that don't match the diff, but expect a lower hit rate than a frontier model. Keep shadow mode
  on until you've read a few summaries.
- Free hosted tiers rate-limit; the client honours `Retry-After` and gives up after a few attempts, then the job is
  reported as failed in the summary (the workflow stays green unless you pass `--strict`).
- This repo's Azure code and Terraform are optional and unused in free mode.

## How the review learns from you
Every comment the agent posts carries a hidden fingerprint of the finding (rule, file and quoted code, so it survives line shifts).
On the next run it reads the PR's review threads and:
- **does not repeat** a finding whose thread is still open;
- **stops raising** a finding you resolved (a human resolving the thread) or reacted to with 👎, on this PR and, where the database
  persists (Compose / server mode), on other PRs in the same repo for 90 days;
- **resolves outdated threads** (code changed since the comment) after the new review is posted.
In shadow mode nothing on the PR changes; the summary shows how many threads it *would* resolve. Needs `pull-requests: write`
(already set in `ai-review.yml`); if the token cannot read threads, the review still runs without feedback.
