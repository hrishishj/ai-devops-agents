<div align="center">

# 🤖 AI DevOps Agents

**Agentic AI for software development and IT operations — powered by Azure OpenAI GPT-4o, GitHub Actions, and Azure Container Apps.**

[![Python 3.12](https://img.shields.io/badge/Python-3.12-3776ab?style=flat-square&logo=python&logoColor=white)](https://python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.111-009688?style=flat-square&logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com)
[![Azure OpenAI](https://img.shields.io/badge/Azure_OpenAI-GPT--4o-0078d4?style=flat-square&logo=microsoft-azure&logoColor=white)](https://azure.microsoft.com/en-us/products/ai-services/openai-service)
[![Terraform](https://img.shields.io/badge/Terraform-1.7+-7b42bc?style=flat-square&logo=terraform&logoColor=white)](https://terraform.io)
[![Tests](https://img.shields.io/badge/Tests-120_passing-28a745?style=flat-square&logo=pytest&logoColor=white)](#testing)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow?style=flat-square)](LICENSE)

<br/>

*Three specialised AI agents that automate code review, IT incident resolution, and security scanning — integrated directly into your GitHub pull request workflow.*

[**Quick Start**](#quick-start) · [**Architecture**](#architecture) · [**Agents**](#agents) · [**Local Setup**](#local-development-setup) · [**Deployment**](#cloud-deployment) · [**Testing**](#testing)

</div>

---

## Table of Contents

- [What This Project Does](#what-this-project-does)
- [Architecture](#architecture)
- [Agents](#agents)
  - [Coding Co-Pilot](#-coding-co-pilot)
  - [IT Ops Agent](#-it-ops-agent)
  - [Test & Security Agent](#-test--security-agent)
- [Tech Stack](#tech-stack)
- [Project Structure](#project-structure)
- [Quick Start](#quick-start)
- [Local Development Setup](#local-development-setup)
- [Environment Variables](#environment-variables)
- [Cloud Deployment](#cloud-deployment)
- [GitHub Actions Workflows](#github-actions-workflows)
- [GitHub Repository Secrets](#github-repository-secrets)
- [Testing](#testing)
- [Security Model](#security-model)
- [Observability](#observability)
- [Contributing](#contributing)
- [License](#license)

---

## What This Project Does

Software developers and IT teams spend enormous time on repetitive tasks — boilerplate code reviews, chasing test coverage, diagnosing production incidents from raw logs. This project replaces that toil with three AI agents:

| Agent | Trigger | What It Does |
|---|---|---|
| 🧠 **Coding Co-Pilot** | Every pull request | Reviews your code diff with GPT-4o. Posts inline comments with severity ratings (`critical` / `warning` / `suggestion`), category labels (`bug` / `security` / `perf`), and concrete fix suggestions — just like a senior engineer would. |
| 🔧 **IT Ops Agent** | Azure Monitor alert or log stream | Analyses system logs, identifies root cause, scores its own confidence, and either auto-triggers an Azure Automation runbook (confidence ≥ 0.85) or drafts a full incident ticket for human review. |
| 🛡️ **Test & Security Agent** | Every pull request | Runs 10 built-in SAST rules to find security vulnerabilities, parses the AST of changed files to find untested functions, then uses GPT-4o to generate the missing unit tests and explain every finding in plain English. |

All three agents run automatically when you open a pull request or when Azure Monitor fires an alert — no manual steps required.

---

## Architecture

```
┌──────────────────────────────────────────────────────────────┐
│                        TRIGGER SOURCES                        │
│   GitHub Webhooks   Azure Monitor Alerts   Developer CLI      │
└──────────────┬───────────────┬──────────────────┬────────────┘
               │               │                  │
               └───────────────▼──────────────────┘
                               │
                    ┌──────────▼──────────┐
                    │   FastAPI Webhook    │
                    │      Receiver       │
                    │  (HMAC validation,  │
                    │   auth, routing)    │
                    └──────────┬──────────┘
                               │
                    ┌──────────▼──────────┐
                    │  Azure Service Bus  │
                    │  ┌───────────────┐  │
                    │  │ coding-queue  │  │
                    │  │ it-ops-queue  │  │
                    │  │ security-queue│  │
                    │  └───────────────┘  │
                    └──────────┬──────────┘
                               │
               ┌───────────────┼───────────────┐
               ▼               ▼               ▼
       ┌───────────┐   ┌───────────┐   ┌───────────┐
       │  Coding   │   │  IT Ops   │   │  Test &   │
       │ Co-Pilot  │   │   Agent   │   │ Security  │
       │   Agent   │   │           │   │   Agent   │
       └─────┬─────┘   └─────┬─────┘   └─────┬─────┘
             │               │               │
             └───────────────┼───────────────┘
                             │
                  ┌──────────▼──────────┐
                  │  Azure OpenAI GPT-4o│
                  └──────────┬──────────┘
                             │
          ┌──────────────────┼──────────────────┐
          │                  │                  │
    ┌─────▼─────┐    ┌───────▼──────┐   ┌──────▼──────┐
    │ Cosmos DB │    │    Redis     │   │  Key Vault  │
    │  (memory) │    │(idempotency) │   │  (secrets)  │
    └───────────┘    └──────────────┘   └─────────────┘
                             │
          ┌──────────────────┼──────────────────┐
          ▼                  ▼                  ▼
   PR inline comments  Auto-runbooks     GitHub Issues
   Security reports    (restart, scale)  Ticket drafts
```

### How a Pull Request Review Works

1. Developer opens a PR → GitHub sends a webhook event
2. FastAPI validates the HMAC signature and pushes to Service Bus
3. Service Bus consumer picks up the message and calls the agent
4. **Coding Co-Pilot** fetches the diff, retrieves up to 3 historical reviews from Cosmos DB for context, builds a structured prompt, calls GPT-4o, validates the JSON response, and posts inline review comments
5. **Test & Security Agent** runs in parallel: SAST scan (no LLM needed) + coverage gap detection + GPT-4o for explanations and test generation
6. Both agents store their results in Cosmos DB and release the idempotency key

### How an IT Ops Alert Works

1. Azure Monitor fires an alert → webhook hits `/webhooks/azure-monitor`
2. IT Ops Agent extracts error signatures (OOMKilled, ETIMEDOUT, deadlock, etc.) via regex
3. GPT-4o analyses the full log and returns: root cause, confidence score, affected components, and remediation
4. **If confidence ≥ 0.85**: Azure Automation runbook is triggered automatically
5. **If confidence < 0.85**: A full incident ticket draft is created and posted as a GitHub Issue for human review

---

## Agents

### 🧠 Coding Co-Pilot

**File:** `agents/coding_copilot/agent.py`

**Pipeline:**
```
PR Diff
  └─► Context Builder   →  fetches diff + file tree + 3 historical reviews from Cosmos
        └─► Prompt Engine  →  assembles structured prompt with few-shot examples
              └─► GPT-4o   →  returns validated JSON with inline comments
                    └─► Response Parser  →  validates schema (Pydantic)
                          └─► GitHub SDK  →  posts inline PR review
                                └─► Cosmos DB  →  persists review record
```

**Output in your PR:**

```
🔴 [CRITICAL] security  ·  auth/handler.py  ·  line 14

SQL injection vulnerability via string formatting.
The user-supplied token is directly interpolated into the SQL query,
allowing an attacker to manipulate the query structure.

Suggested fix:
  db.execute('SELECT * FROM users WHERE token = ?', (token,))
```

**Severity levels:** `🔴 critical` · `🟡 warning` · `🔵 suggestion` · `ℹ️ info`

**Categories:** `bug` · `security` · `perf` · `style` · `docs`

---

### 🔧 IT Ops Agent

**File:** `agents/it_ops/agent.py`

**Pipeline:**
```
Log / Alert Payload
  └─► Signature Extractor  →  regex pattern matching (10 error patterns)
        └─► GPT-4o Analysis  →  root cause + confidence + remediation JSON
              └─► Decision Router
                    ├─► confidence ≥ 0.85 + runbook known
                    │     └─► Azure Automation Runbook  (auto-executed)
                    └─► confidence < 0.85 or runbook unknown
                          └─► Ticket Draft  →  GitHub Issue
```

**Built-in error pattern detection:**

| Pattern | Detected Strings |
|---|---|
| `OutOfMemory` | OOMKilled, out of memory |
| `ConnectionTimeout` | ETIMEDOUT, connection timed out |
| `DiskFull` | ENOSPC, no space left on device |
| `HighCPU` | CPU usage 9x% or 100% |
| `AuthFailure` | 401 Unauthorized, 403 Forbidden |
| `DatabaseError` | deadlock, connection pool exhausted |
| `ServiceUnavailable` | 503, service unavailable |
| `NullPointer` | NullPointerException, NoneType |

**Available runbooks:** `restart-service` · `scale-out-aks` · `clear-disk-space` · `rotate-credentials` · `flush-cache` · `failover-db` · `drain-node`

---

### 🛡️ Test & Security Agent

**File:** `agents/test_security/agent.py`

**Pipeline (two tracks run in parallel):**

```
PR Changed Files
  ├─► SAST Scanner  (no LLM needed)
  │     ├─► 10 regex-based security rules
  │     └─► GPT-4o explains each finding + suggests fix
  │
  └─► Coverage Analyser
        ├─► AST parser extracts all public function names
        ├─► Compares against test file content
        └─► GPT-4o generates pytest unit tests for gaps
```

**Built-in SAST rules:**

| Rule | Detects | Severity | CWE |
|---|---|---|---|
| SEC001 | SQL injection via f-strings | 🔴 Critical | CWE-89 |
| SEC002 | Hardcoded passwords/secrets | 🔴 Critical | CWE-798 |
| SEC003 | Shell injection (`shell=True`) | 🟠 High | CWE-78 |
| SEC004 | MD5/SHA1 for security hashing | 🟠 High | CWE-327 |
| SEC005 | Insecure pickle deserialization | 🟠 High | CWE-502 |
| SEC006 | `eval()` / `exec()` usage | 🟠 High | CWE-95 |
| SEC007 | XML External Entity (XXE) | 🟡 Medium | CWE-611 |
| SEC008 | `verify=False` (SSL bypass) | 🟡 Medium | CWE-295 |
| SEC009 | Path traversal risk | 🟡 Medium | CWE-22 |
| SEC010 | `DEBUG=True` in source | 🔵 Low | CWE-489 |

---

## Tech Stack

| Layer | Technology |
|---|---|
| **Language** | Python 3.12 |
| **Web Framework** | FastAPI + Uvicorn |
| **AI Model** | Azure OpenAI Service — GPT-4o |
| **Message Queue** | Azure Service Bus |
| **Agent Memory** | Azure Cosmos DB (NoSQL, serverless) |
| **Session / Idempotency** | Azure Cache for Redis |
| **Secrets** | Azure Key Vault + Managed Identity |
| **Observability** | OpenTelemetry → Azure Monitor Application Insights |
| **Hosting** | Azure Container Apps (scale-to-zero) |
| **CI/CD** | GitHub Actions |
| **Infrastructure as Code** | Terraform ≥ 1.7 |
| **Schema Validation** | Pydantic v2 |
| **GitHub Integration** | PyGitHub + HMAC-SHA256 webhook verification |

---

## Project Structure

```
ai-devops-agents/
│
├── agents/                           # The three AI agents
│   ├── coding_copilot/
│   │   └── agent.py                  # Code review pipeline
│   ├── it_ops/
│   │   └── agent.py                  # Log analysis + runbook executor
│   └── test_security/
│       └── agent.py                  # AST coverage analyser + SAST scanner
│
├── api/                              # Web layer
│   ├── main.py                       # FastAPI app — webhook routes + health probes
│   └── service_bus_consumer.py       # Async Service Bus message consumer
│
├── shared/                           # Utilities shared by all agents
│   ├── llm_client.py                 # Azure OpenAI wrapper (retry, backoff, token guard, OTel)
│   ├── config.py                     # Pydantic settings + Key Vault secret resolution
│   ├── cosmos_store.py               # Cosmos DB CRUD + agent memory queries
│   ├── redis_cache.py                # Redis cache + idempotency key management
│   ├── github_client.py              # GitHub API — diff fetch, comment posting, commits
│   └── telemetry.py                  # OpenTelemetry bootstrap (traces + metrics)
│
├── .github/
│   └── workflows/
│       ├── ai_code_review.yml        # Runs Coding Co-Pilot on every PR
│       ├── ai_test_security.yml      # Runs Test & Security Agent on every PR
│       ├── it_ops_analysis.yml       # Runs IT Ops Agent on manual/dispatched alerts
│       └── deploy.yml                # Build → integration test → deploy to Azure
│
├── infra/
│   └── terraform/
│       ├── main.tf                   # Root module — orchestrates all modules
│       ├── variables.tf              # Input variable declarations
│       ├── outputs.tf                # Output values (URLs, FQDNs)
│       ├── backend.hcl               # Remote state backend config
│       ├── terraform.tfvars.example  # Example variable values (copy + fill in)
│       └── modules/
│           ├── keyvault/             # Azure Key Vault + RBAC assignments
│           ├── openai/               # Azure OpenAI account + GPT-4o deployment
│           ├── cosmosdb/             # Cosmos DB account + 3 containers
│           ├── redis/                # Azure Cache for Redis (Standard, SSL)
│           ├── servicebus/           # Service Bus namespace + 3 queues
│           ├── monitoring/           # Log Analytics workspace + Application Insights
│           └── containerapp/         # Container Apps env + API app + consumer app
│
├── scripts/
│   ├── bootstrap.sh                  # One-time Azure infrastructure setup script
│   ├── dev_setup.sh                  # Local development environment setup
│   ├── trigger_review.py             # CLI: manually trigger a code review on any PR
│   ├── trigger_itops.py              # CLI: manually trigger IT Ops analysis from a log file
│   └── runbooks/
│       ├── restart_service.ps1       # Restart Container App / AKS deployment / App Service
│       ├── clear_disk_space.ps1      # Clean logs, temp files, Docker layers on target VM
│       └── scale_out_aks.ps1         # Scale AKS node pool or deployment replica count
│
├── config/
│   ├── agents.yml                    # Agent tuning (temperature, thresholds, max tokens)
│   └── logging_config.py             # Structured JSON logging setup (structlog)
│
├── tests/
│   ├── conftest.py                   # Shared pytest fixtures + Azure dependency patches
│   ├── unit/                         # 109 unit tests — no Azure account needed
│   │   ├── test_coding_copilot.py    # 21 tests
│   │   ├── test_it_ops.py            # 22 tests
│   │   ├── test_test_security.py     # 28 tests
│   │   ├── test_shared.py            # 23 tests
│   │   └── test_api.py               # 15 tests
│   └── integration/                  # 11 integration tests — requires live Azure
│       └── test_integration.py
│
├── Dockerfile                        # Multi-stage build (builder → runtime → dev)
├── pyproject.toml                    # Build system + Ruff linter + mypy + coverage config
├── pytest.ini                        # Test runner configuration
├── requirements.txt                  # Pinned Python dependencies
└── .env.example                      # Environment variable template — copy to .env
```

---

## Quick Start

### Option A — Unit Tests Only (No Azure Account Needed)

```bash
# 1. Create a virtual environment
python -m venv .venv
source .venv/bin/activate        # Mac / Linux
# .venv\Scripts\activate         # Windows

# 2. Install dependencies
pip install -r requirements.txt

# 3. Run all 120 unit tests
pytest tests/unit/ -v
# Expected: 120 passed
```

### Option B — Full Local Development

```bash
# 1. Copy and fill in credentials
cp .env.example .env
# Open .env and add your Azure OpenAI, Cosmos DB, and GitHub credentials

# 2. Start local Redis via Docker
docker run -d --name local-redis -p 6379:6379 \
  redis:7-alpine --requirepass localdevpassword

# 3. Start the webhook API server
uvicorn api.main:app --reload --port 8000

# 4. Open the interactive API docs
# http://localhost:8000/docs

# 5. Manually trigger an IT Ops analysis
echo "FATAL: OOMKilled — memory limit 512Mi exceeded" | \
  python scripts/trigger_itops.py --alert-id test-001 --source manual --dry-run
```

---

## Local Development Setup

### Prerequisites

| Tool | Minimum Version | Install |
|---|---|---|
| Python | 3.12 | [python.org/downloads](https://python.org/downloads) |
| Docker Desktop | Latest | [docker.com/products/docker-desktop](https://docker.com/products/docker-desktop) |
| Git | Latest | [git-scm.com](https://git-scm.com) |
| Azure CLI | Latest | [learn.microsoft.com](https://learn.microsoft.com/en-us/cli/azure/install-azure-cli) |
| VS Code | Latest | [code.visualstudio.com](https://code.visualstudio.com) |

**Recommended VS Code extensions:** Python, Pylance, HashiCorp Terraform

### Full Setup Steps

```bash
# 1. Create and activate virtual environment
python -m venv .venv
source .venv/bin/activate          # Mac/Linux
# .venv\Scripts\activate           # Windows

# 2. Install all dependencies
pip install -r requirements.txt

# 3. Configure VS Code interpreter
# Press Ctrl+Shift+P → "Python: Select Interpreter" → choose .venv

# 4. Start local Redis
docker run -d --name local-redis -p 6379:6379 \
  redis:7-alpine --requirepass localdevpassword

# 5. Create your .env file
cp .env.example .env
# Fill in credentials (see Environment Variables section)

# 6. Verify everything works
pytest tests/unit/ -v
# Expected output: 120 passed in ~5s

# 7. Start the API server
uvicorn api.main:app --reload --port 8000

# 8. Health check
curl http://localhost:8000/health
# {"status": "ok"}
```

---

## Environment Variables

Copy `.env.example` to `.env` and populate the values. All secrets are pulled from Azure Key Vault automatically in production — the `.env` file is only used for local development.

### Azure OpenAI (Required — powers all three agents)

```bash
AZURE_OPENAI_ENDPOINT=https://your-resource.openai.azure.com/
AZURE_OPENAI_API_KEY=your-api-key-here
AZURE_OPENAI_API_VERSION=2024-02-01
AZURE_OPENAI_DEPLOYMENT=gpt-4o
```

### Cosmos DB (Required — agent memory and history)

```bash
COSMOS_ENDPOINT=https://your-account.documents.azure.com:443/
COSMOS_KEY=your-primary-key
COSMOS_DATABASE=ai_agents_db
COSMOS_CONTAINER_REVIEWS=code_reviews
COSMOS_CONTAINER_INCIDENTS=it_incidents
```

### Redis (Required — idempotency and session state)

```bash
# Local Docker (development):
REDIS_HOST=localhost
REDIS_PORT=6379
REDIS_PASSWORD=localdevpassword
REDIS_SSL=false

# Azure Cache for Redis (production):
# REDIS_HOST=your-cache.redis.cache.windows.net
# REDIS_PORT=6380
# REDIS_PASSWORD=your-access-key
# REDIS_SSL=true
```

### GitHub (Required for code review and security agents)

```bash
# Option A: Personal Access Token (local development)
GITHUB_TOKEN=ghp_your_personal_access_token
GITHUB_WEBHOOK_SECRET=any-random-string-you-invent

# Option B: GitHub App (production — recommended)
GITHUB_APP_ID=123456
GITHUB_APP_PRIVATE_KEY="-----BEGIN RSA PRIVATE KEY-----\n...\n-----END RSA PRIVATE KEY-----"
```

### IT Ops Agent

```bash
AZURE_AUTOMATION_ACCOUNT_URL=https://management.azure.com/subscriptions/.../automationAccounts/your-account
CONFIDENCE_THRESHOLD_AUTO_RESOLVE=0.85
```

### Observability (Optional)

```bash
APPLICATIONINSIGHTS_CONNECTION_STRING=InstrumentationKey=...;IngestionEndpoint=...
OTEL_SERVICE_NAME=ai-devops-agents
```

### Production Only

```bash
# In production, only this is needed — all other secrets come from Key Vault
AZURE_KEYVAULT_URL=https://your-keyvault.vault.azure.net/
```

---

## Manually Triggering Agents

You can run any agent from the command line without a GitHub webhook event.

### Trigger a Code Review on Any PR

```bash
# Review PR #42
python scripts/trigger_review.py --repo your-org/your-repo --pr 42

# Review + security scan together
python scripts/trigger_review.py --repo your-org/your-repo --pr 42 --security
```

### Trigger an IT Ops Analysis

```bash
# From a log file
python scripts/trigger_itops.py \
  --alert-id incident-001 \
  --source manual \
  --log-file /path/to/application.log

# Pipe kubectl logs directly
kubectl logs pod/worker-7d9f8b -n production | \
  python scripts/trigger_itops.py \
    --alert-id k8s-$(date +%s) \
    --source kubernetes

# Dry run — shows analysis, does not execute any runbooks
echo "FATAL: OOMKilled — memory limit exceeded" | \
  python scripts/trigger_itops.py \
    --alert-id test-001 \
    --source test \
    --dry-run
```

---

## Cloud Deployment

### Prerequisites

- Azure subscription with Contributor access
- Terraform ≥ 1.7
- Azure CLI authenticated (`az login`)
- Docker Desktop running

### One-Command Bootstrap (Recommended)

```bash
./scripts/bootstrap.sh --env staging --location eastus2
```

This single command:
1. Creates an Azure Storage Account to hold Terraform state
2. Creates an Azure Container Registry, builds the Docker image, and pushes it
3. Runs `terraform init` + `terraform plan` + `terraform apply`
4. Provisions the full stack: Key Vault, Azure OpenAI + GPT-4o deployment, Cosmos DB, Redis, Service Bus (3 queues), Application Insights, Container Apps (API + consumer)

### Manual Terraform Deployment

```bash
cd infra/terraform

# Copy and fill in your variable values
cp terraform.tfvars.example terraform.tfvars
# Edit terraform.tfvars

# Initialise with remote state backend
terraform init -backend-config=backend.hcl

# Preview what will be created
terraform plan -var-file=terraform.tfvars

# Create everything
terraform apply -var-file=terraform.tfvars

# View output values (API URL, Key Vault URL, etc.)
terraform output
```

### Subsequent Deployments

After initial bootstrap, every push to the `main` branch triggers the `deploy.yml` GitHub Actions workflow which automatically builds a new image, runs integration tests, and updates the Container Apps with zero downtime.

---

## GitHub Actions Workflows

| Workflow | File | Trigger | What It Does |
|---|---|---|---|
| **AI Code Review** | `ai_code_review.yml` | PR opened or updated | Runs Coding Co-Pilot, posts inline review comments on the PR |
| **AI Test & Security** | `ai_test_security.yml` | PR opened or updated, push to main | Runs existing test suite, then SAST scan and GPT-4o test generation |
| **IT Ops Analysis** | `it_ops_analysis.yml` | Manual dispatch or `repository_dispatch` event | Analyses an alert payload, optionally creates a GitHub Issue |
| **Deploy to Azure** | `deploy.yml` | Push to `main`, or manual dispatch | Builds Docker image, runs integration tests, deploys to Container Apps |

### Making Reviews Happen Automatically

Once you add the secrets below and push the workflows to a repository, code reviews and security scans will run on every new pull request — no further configuration needed.

---

## GitHub Repository Secrets

Go to **repository → Settings → Secrets and variables → Actions → New repository secret**

### Core (All Workflows)

| Secret | Description |
|---|---|
| `AZURE_OPENAI_ENDPOINT` | Your Azure OpenAI service endpoint URL |
| `AZURE_OPENAI_API_KEY` | Azure OpenAI API key |
| `AZURE_OPENAI_DEPLOYMENT` | Model deployment name (default: `gpt-4o`) |
| `COSMOS_ENDPOINT` | Cosmos DB account endpoint URL |
| `COSMOS_KEY` | Cosmos DB primary master key |
| `REDIS_HOST` | Azure Cache for Redis hostname |
| `REDIS_PASSWORD` | Redis primary access key |

### GitHub Integration (Code Review + Security Workflows)

| Secret | Description |
|---|---|
| `GH_APP_ID` | GitHub App ID |
| `GH_APP_PRIVATE_KEY` | GitHub App RSA private key in PEM format |

### IT Ops Workflow

| Secret | Description |
|---|---|
| `AZURE_AUTOMATION_ACCOUNT_URL` | Azure Automation account REST API base URL |
| `AZURE_CLIENT_ID` | Service principal or managed identity client ID |
| `AZURE_CLIENT_SECRET` | Service principal client secret |
| `AZURE_TENANT_ID` | Azure Active Directory tenant ID |

### Deploy Workflow

| Secret | Description |
|---|---|
| `AZURE_CREDENTIALS` | Service principal JSON blob used by `azure/login` action |
| `ACR_LOGIN_SERVER` | Container registry login server (e.g. `myacr.azurecr.io`) |
| `ACR_NAME` | Container registry name (without `.azurecr.io`) |
| `AZURE_RESOURCE_GROUP` | Resource group that contains the Container Apps |
| `AZURE_KEYVAULT_URL` | Key Vault URL for runtime secret resolution by the containers |

### Optional

| Secret | Description |
|---|---|
| `APPINSIGHTS_CONNECTION_STRING` | Application Insights connection string for OpenTelemetry export |

---

## Testing

The project ships with **120 unit tests** (no Azure account needed) and **11 integration tests** (require live Azure services).

### Run Unit Tests

```bash
# All unit tests
pytest tests/unit/ -v

# Single test file
pytest tests/unit/test_it_ops.py -v

# Single test class
pytest tests/unit/test_test_security.py::TestRunSastScan -v

# With HTML coverage report
pytest tests/unit/ \
  --cov=agents --cov=shared --cov=api \
  --cov-report=html:htmlcov
# Then open htmlcov/index.html in your browser
```

### Run Integration Tests

Integration tests make real API calls to Azure OpenAI, Cosmos DB, and Redis. They are skipped automatically if `AZURE_OPENAI_ENDPOINT` is not set.

```bash
pytest tests/integration/ -v -m integration
```

### Test Coverage Breakdown

| Test File | Count | What Is Tested |
|---|---|---|
| `test_coding_copilot.py` | 21 | Pydantic schema validation, context building, diff truncation, idempotency, prompt construction, LLM response parsing |
| `test_it_ops.py` | 22 | All 8 error signature patterns, confidence routing thresholds, schema validation, runbook failure handling, metadata persistence |
| `test_test_security.py` | 28 | All 10 SAST rules, line number accuracy, AST function extraction, coverage gap detection, file splitting from diff |
| `test_shared.py` | 23 | LLM retry on rate limit, exponential backoff, token budget enforcement, Redis NX semantics, Cosmos CRUD and query |
| `test_api.py` | 15 | HMAC-SHA256 signature verification, webhook event routing, health and readiness probes, direct API triggers |
| `test_integration.py` | 11 | Live Redis ping/set/get/idempotency, live Cosmos upsert/query/delete, live GPT-4o completion, IT Ops end-to-end |

---

## Security Model

| Mechanism | Implementation |
|---|---|
| **Zero credential code** | Azure Managed Identity handles all service-to-service auth in production — no keys in environment variables or source code |
| **Azure Key Vault** | Single secrets store; all values resolved at startup via `DefaultAzureCredential` |
| **Webhook HMAC-SHA256** | Every incoming GitHub webhook is signature-verified before any processing begins |
| **Idempotency keys** | Atomic Redis `SET NX` prevents duplicate agent runs for the same PR or alert ID |
| **Non-root container** | Application runs as the `agents` user — no root privileges in production |
| **Pydantic schema validation** | Every LLM JSON response is validated against a strict typed schema before being acted upon |
| **Token budget ceiling** | Hard 4096-token cap per request enforced in `AzureOpenAIClient` — prevents runaway costs |
| **Exponential back-off** | Rate limits and transient 5xx errors are retried with increasing wait times (max 5 attempts) |
| **Dead-letter queues** | Messages that fail processing are routed to Service Bus dead-letter queues rather than silently dropped |

---

## Observability

Every agent call emits **OpenTelemetry spans** exported to **Azure Monitor Application Insights**. All spans include:

**LLM attributes (every agent):**
```
llm.deployment         gpt-4o
llm.prompt_tokens      812
llm.completion_tokens  384
llm.total_tokens       1196
llm.latency_ms         2341.5
llm.finish_reason      stop
```

**Coding Co-Pilot spans:**
```
github.repo            org/repo
github.pr_number       42
review.comment_count   3
review.overall_quality needs_work
```

**IT Ops Agent spans:**
```
alert.id               incident-001
alert.source           azure_monitor
alert.signatures       OutOfMemory,ConnectionTimeout
alert.confidence       0.91
alert.severity         high
alert.outcome          auto_resolved
```

**Test & Security Agent spans:**
```
github.pr_number       42
sast.finding_count     2
test.generated_count   4
```

### Useful Application Insights KQL Queries

```kql
// Average LLM latency per hour
traces
| where customDimensions["llm.deployment"] == "gpt-4o"
| summarize avg(todouble(customDimensions["llm.latency_ms"])) by bin(timestamp, 1h)
| render timechart

// IT Ops auto-resolution rate
traces
| where isnotempty(tostring(customDimensions["alert.outcome"]))
| summarize count() by tostring(customDimensions["alert.outcome"])
| render piechart

// Total token usage by service
traces
| where isnotempty(tostring(customDimensions["llm.total_tokens"]))
| summarize sum(toint(customDimensions["llm.total_tokens"])) by serviceName
```

---

## Contributing

Contributions are welcome. Please follow this process:

**1. Fork and set up locally**
```bash
git clone https://github.com/your-org/ai-devops-agents
cd ai-devops-agents
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

**2. Create a branch**
```bash
git checkout -b feature/your-improvement
```

**3. Make changes, then verify**
```bash
pytest tests/unit/ -v                  # all tests must pass
ruff check . && ruff format --check .  # no lint errors
mypy agents/ shared/ api/              # no type errors
```

**4. Open a Pull Request**

The Coding Co-Pilot and Test & Security agents will automatically review your PR and post feedback.

### Extending the Project

**Adding a SAST rule** — add a `SastRule` entry to `_SAST_RULES` in `agents/test_security/agent.py` and a corresponding test in `tests/unit/test_test_security.py`:

```python
SastRule(
    rule_id="SEC011",
    description="Your rule description",
    pattern=re.compile(r"your_regex", re.IGNORECASE),
    severity="high",   # critical | high | medium | low
    cwe="CWE-XXX",
)
```

**Adding a runbook** — create `scripts/runbooks/your_runbook.ps1`, add its name to `config/agents.yml` under `it_ops.allowed_runbooks`, add it to `_ITOPS_SYSTEM_PROMPT` in `agents/it_ops/agent.py`, then upload it to your Azure Automation account.

**Adjusting agent behaviour** — edit `config/agents.yml` to tune temperatures, token limits, confidence thresholds, and allowed runbooks without changing any Python code.

---

## License

```
MIT License — Copyright (c) 2024 AI DevOps Agents

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT.
```

---

<div align="center">

Built on Azure OpenAI · FastAPI · GitHub Actions · Terraform

</div>
