#!/usr/bin/env bash
# ============================================================
# dev_setup.sh — Local development environment setup
#
# Spins up Redis locally via Docker, installs Python deps,
# and verifies the configuration before first run.
#
# Usage:
#   ./scripts/dev_setup.sh
# ============================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

cd "$PROJECT_ROOT"

echo "============================================================"
echo " AI DevOps Agents — Local Dev Setup"
echo "============================================================"

# ─── Python version check ────────────────────────────────────
PYTHON_VERSION=$(python3 --version 2>&1 | awk '{print $2}')
REQUIRED_MAJOR=3
REQUIRED_MINOR=12

MAJOR=$(echo "$PYTHON_VERSION" | cut -d. -f1)
MINOR=$(echo "$PYTHON_VERSION" | cut -d. -f2)

if [[ "$MAJOR" -lt "$REQUIRED_MAJOR" ]] || \
   [[ "$MAJOR" -eq "$REQUIRED_MAJOR" && "$MINOR" -lt "$REQUIRED_MINOR" ]]; then
    echo "❌ Python $REQUIRED_MAJOR.$REQUIRED_MINOR+ required. Found: $PYTHON_VERSION"
    exit 1
fi
echo "✅ Python $PYTHON_VERSION"

# ─── Virtual environment ─────────────────────────────────────
if [[ ! -d ".venv" ]]; then
    echo ">>> Creating virtual environment..."
    python3 -m venv .venv
fi

source .venv/bin/activate
echo "✅ Virtual environment: .venv"

# ─── Install dependencies ────────────────────────────────────
echo ">>> Installing dependencies..."
pip install --upgrade pip --quiet
pip install -r requirements.txt --quiet
echo "✅ Dependencies installed"

# ─── .env file ───────────────────────────────────────────────
if [[ ! -f ".env" ]]; then
    cp .env.example .env
    echo "⚠️  Created .env from .env.example — please fill in your Azure credentials."
else
    echo "✅ .env found"
fi

# ─── Docker / local Redis ────────────────────────────────────
if command -v docker &>/dev/null; then
    if ! docker ps --filter "name=local-redis" --filter "status=running" -q | grep -q .; then
        echo ">>> Starting local Redis (non-SSL on port 6379)..."
        docker run -d \
            --name local-redis \
            --rm \
            -p 6379:6379 \
            redis:7-alpine \
            --requirepass "localdevpassword" \
            > /dev/null 2>&1 || true
        echo "✅ Redis started on localhost:6379"
        echo "    Password: localdevpassword"
        echo "    Set in .env: REDIS_HOST=localhost REDIS_PORT=6379 REDIS_PASSWORD=localdevpassword REDIS_SSL=false"
    else
        echo "✅ Redis already running"
    fi
else
    echo "⚠️  Docker not found — start Redis manually or use Azure Cache for Redis"
fi

# ─── Run unit tests to verify setup ──────────────────────────
echo ""
echo ">>> Running unit tests..."
pytest tests/unit/ -q --tb=short 2>&1 | tail -20
echo ""

# ─── Start dev server ────────────────────────────────────────
echo "============================================================"
echo " Setup complete!"
echo ""
echo " Start the API server:"
echo "   source .venv/bin/activate"
echo "   uvicorn api.main:app --reload --port 8000"
echo ""
echo " Webhook test (requires ngrok or similar):"
echo "   ngrok http 8000"
echo ""
echo " Run all unit tests:"
echo "   pytest tests/unit/ -v"
echo ""
echo " Run integration tests (requires Azure credentials in .env):"
echo "   pytest tests/integration/ -v -m integration"
echo "============================================================"
