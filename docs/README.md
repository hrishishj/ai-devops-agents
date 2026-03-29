# AI DevOps Agents

> **Azure + GitHub agentic AI** for automated code review, IT operations, and security scanning.

[![Deploy to Azure](https://aka.ms/deploytoazurebutton)](https://portal.azure.com)
[![Python 3.12](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

---

## Overview

This project transforms software development and IT operations with three specialised AI agents built on **Azure OpenAI GPT-4o**, integrated into **GitHub workflows**, and deployed as **Azure Container Apps**.

| Agent | Trigger | Action |
|---|---|---|
| **Coding Co-Pilot** | PR opened / synchronised | Posts inline code review comments with severity ratings and fix suggestions |
| **IT Ops Agent** | Azure Monitor alert / Service Bus message | Identifies root cause, auto-executes runbooks (confidence ≥ 0.85) or drafts a ticket |
| **Test & Security Agent** | PR opened / synchronised | Detects SAST vulnerabilities, generates missing unit tests, posts combined report |

---

## Architecture

```
Trigger Sources
  GitHub Webhooks ──┐
  Azure Monitor  ──┤──► Azure API Management ──► FastAPI Webhook Receiver
  Developer CLI  ──┘         (auth + routing)        │
                                                      ▼
                                            Azure Service Bus Queues
                                         ┌─────────────────────────┐
                                         │  coding-copilot-queue   │
                                         │  it-ops-queue           │
                                         │  test-security-queue    │
                                         └────────────┬────────────┘
                                                      ▼
                                         Service Bus Consumer (async)
                                              │          │          │
                                              ▼          ▼          ▼
                                         Coding      IT Ops    Test &
                                         Co-Pilot    Agent     Security
                                         Agent                 Agent
                                              │          │          │
                                              └──────────┼──────────┘
                                                         ▼
                                              Azure OpenAI GPT-4o
                                                         │
                                              ┌──────────┴──────────┐
                                              │  Supporting Services │
                                              │  Cosmos DB (memory) │
                                              │  Redis (idempotency)│
                                              │  Key Vault (secrets)│
                                              │  App Insights (OTel)│
                                              └─────────────────────┘
                                                         │
                                              ┌──────────┴──────────┐
                                              │      Outputs        │
                                              │  PR inline comments │
                                              │  Auto-fix commits   │
                                              │  Resolved incidents │
                                              │  GitHub Issues      │
                                              └─────────────────────┘
```

---

## Folder Structure

```
ai-devops-agents/
├── agents/
│   ├── coding_copilot/
│   │   └── agent.py          # Code review pipeline
│   ├── it_ops/
│   │   └── agent.py          # Log analysis + runbook executor
│   └── test_security/
│       └── agent.py          # AST coverage + SAST scanner
│
├── api/
│   ├── main.py               # FastAPI webhook receiver
│   └── service_bus_consumer.py  # Async Service Bus consumer
│
├── shared/
│   ├── llm_client.py         # Azure OpenAI wrapper (retry, backoff, OTel)
│   ├── config.py             # Pydantic settings + Key Vault resolution
│   ├── cosmos_store.py       # Cosmos DB CRUD + agent memory
│   ├── redis_cache.py        # Redis session state + idempotency keys
│   ├── github_client.py      # GitHub API (diff fetch, comment posting)
│   └── telemetry.py          # OpenTelemetry bootstrap
│
├── .github/workflows/
│   ├── ai_code_review.yml    # Triggered on PR — runs Coding Co-Pilot
│   ├── ai_test_security.yml  # Triggered on PR — runs Test & Security
│   ├── it_ops_analysis.yml   # Triggered on alert — runs IT Ops Agent
│   └── deploy.yml            # Build, test, and deploy to Azure
│
├── infra/terraform/
│   ├── main.tf               # Root module
│   ├── variables.tf
│   ├── outputs.tf
│   └── modules/
│       ├── keyvault/         # Azure Key Vault
│       ├── openai/           # Azure OpenAI + GPT-4o deployment
│       ├── cosmosdb/         # Cosmos DB + containers
│       ├── redis/            # Azure Cache for Redis
│       ├── servicebus/       # Service Bus + 3 queues
│       ├── monitoring/       # Log Analytics + Application Insights
│       └── containerapp/     # Container Apps environment + apps
│
├── scripts/
│   ├── bootstrap.sh          # One-time Azure infrastructure setup
│   ├── dev_setup.sh          # Local development setup
│   ├── trigger_review.py     # Manually trigger a code review
│   ├── trigger_itops.py      # Manually trigger IT Ops analysis
│   └── runbooks/
│       ├── restart_service.ps1
│       ├── clear_disk_space.ps1
│       └── scale_out_aks.ps1
│
├── config/
│   ├── agents.yml            # Agent configuration (temperature, thresholds)
│   └── logging_config.py     # Structured logging setup
│
├── tests/
│   ├── conftest.py           # Shared fixtures + env patching
│   ├── unit/
│   │   ├── test_coding_copilot.py
│   │   ├── test_it_ops.py
│   │   ├── test_test_security.py
│   │   ├── test_shared.py
│   │   └── test_api.py
│   └── integration/
│       └── test_integration.py
│
├── Dockerfile                # Multi-stage production build
├── pyproject.toml            # Build system + ruff + mypy + coverage config
├── pytest.ini                # Test runner configuration
├── requirements.txt          # Pinned Python dependencies
└── .env.example              # Environment variable template
```

---

## Quick Start

### Prerequisites

- Python 3.12+
- Docker
- Azure CLI (`az login`)
- Terraform ≥ 1.7
- GitHub App or PAT with `pull_requests: write` permission

### Local Development

```bash
# 1. Clone and run setup
git clone https://github.com/your-org/ai-devops-agents
cd ai-devops-agents
./scripts/dev_setup.sh

# 2. Fill in your Azure credentials
cp .env.example .env
# Edit .env with your AZURE_OPENAI_ENDPOINT, COSMOS_ENDPOINT, etc.

# 3. Start the webhook server
source .venv/bin/activate
uvicorn api.main:app --reload --port 8000

# 4. Expose locally for GitHub webhooks (optional)
ngrok http 8000
```

### Infrastructure Deployment

```bash
# One-time bootstrap (creates tfstate storage, ACR, and full Azure stack)
./scripts/bootstrap.sh --env staging --location eastus2

# Subsequent deployments are handled by GitHub Actions on push to main
```

### GitHub Repository Setup

Add these secrets to your GitHub repository (`Settings → Secrets and variables → Actions`):

| Secret | Description |
|---|---|
| `AZURE_OPENAI_ENDPOINT` | Azure OpenAI service endpoint |
| `AZURE_OPENAI_API_KEY` | Azure OpenAI API key |
| `AZURE_OPENAI_DEPLOYMENT` | Deployment name (default: `gpt-4o`) |
| `COSMOS_ENDPOINT` | Cosmos DB endpoint URL |
| `COSMOS_KEY` | Cosmos DB primary key |
| `REDIS_HOST` | Azure Cache for Redis hostname |
| `REDIS_PASSWORD` | Redis access key |
| `GH_APP_ID` | GitHub App ID |
| `GH_APP_PRIVATE_KEY` | GitHub App RSA private key (PEM) |
| `AZURE_CREDENTIALS` | Azure service principal JSON (for deploy workflow) |
| `ACR_LOGIN_SERVER` | Azure Container Registry login server |
| `ACR_NAME` | ACR name |
| `AZURE_RESOURCE_GROUP` | Resource group for Container Apps |
| `AZURE_KEYVAULT_URL` | Key Vault URL (`https://<name>.vault.azure.net/`) — used by Container Apps at runtime |
| `APPINSIGHTS_CONNECTION_STRING` | Application Insights connection string |
| `AZURE_AUTOMATION_ACCOUNT_URL` | Azure Automation account REST URL — required for IT Ops runbook execution |
| `AZURE_CLIENT_ID` | Service principal / managed identity client ID — used by IT Ops workflow for Azure auth |
| `AZURE_CLIENT_SECRET` | Service principal secret — used by IT Ops workflow (omit when using Managed Identity) |
| `AZURE_TENANT_ID` | Azure Active Directory tenant ID — required alongside `AZURE_CLIENT_ID` |

### GitHub Webhooks

Point your GitHub repository webhook to:
```
https://<your-api-fqdn>/webhooks/github
```

Events to subscribe: `pull_request`, `push`

---

## Agent Details

### Coding Co-Pilot

**Pipeline:** PR diff → Context enrichment (historical reviews from Cosmos DB) → GPT-4o structured JSON → Schema validation → Inline PR comments

**Output example:**
```
🔴 [CRITICAL] security
SQL injection vulnerability via string formatting.
Suggested fix:
  db.execute('SELECT * FROM users WHERE id = ?', (user_id,))
```

### IT Ops Agent

**Pipeline:** Log payload → Regex/pattern signature extraction → GPT-4o root-cause analysis → Confidence routing:
- **≥ 0.85:** Auto-execute Azure Automation runbook
- **< 0.85:** Draft human-readable ticket response → GitHub Issue

**Available runbooks:** `restart-service`, `scale-out-aks`, `clear-disk-space`, `rotate-credentials`, `flush-cache`, `failover-db`, `drain-node`

### Test & Security Agent

**Pipeline (parallel):**
- **SAST Scanner:** 10 built-in rules (SQL injection, hardcoded secrets, shell injection, pickle, eval, MD5/SHA1, XXE, SSL bypass, path traversal, debug mode)
- **Coverage Analyser:** AST-based function extraction → gap detection vs test files → GPT-4o test generation

---

## Running Tests

```bash
# Unit tests (no Azure required)
pytest tests/unit/ -v

# Unit tests with coverage
pytest tests/unit/ --cov=agents --cov=shared --cov=api --cov-report=term-missing

# Integration tests (requires Azure credentials in .env)
pytest tests/integration/ -v -m integration

# Full test suite
pytest -v
```

---

## Security Model

- **Managed Identity** for all Azure service-to-service authentication — no credentials in code or environment variables in production.
- **Azure Key Vault** holds all secrets; resolved at startup via `DefaultAzureCredential`.
- **GitHub webhook HMAC-SHA256** signature validation on every incoming event.
- **Idempotency keys** in Redis prevent duplicate processing of the same PR event or alert.
- **Non-root Docker container** — runs as `agents` user.

---

## Observability

Every agent call emits OpenTelemetry spans to **Azure Monitor Application Insights** with:
- `llm.prompt_tokens`, `llm.completion_tokens`, `llm.total_tokens`
- `llm.latency_ms`, `llm.model`, `llm.deployment`
- Agent-specific attributes: `github.repo`, `github.pr_number`, `alert.confidence`, `alert.outcome`

---

## Contributing

1. Fork the repository
2. Create a feature branch: `git checkout -b feature/my-improvement`
3. Run tests: `pytest tests/unit/`
4. Lint: `ruff check . && ruff format --check .`
5. Type check: `mypy agents/ shared/ api/`
6. Open a Pull Request — the AI Code Review agent will automatically review it!

---

## License

MIT License — see [LICENSE](LICENSE).
