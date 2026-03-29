# ============================================================
# Remote backend configuration.
# Usage: terraform init -backend-config=backend.hcl
# ============================================================

resource_group_name  = "tfstate-rg"
storage_account_name = "tfstateaidvops"
container_name       = "tfstate"
key                  = "ai-devops-agents.tfstate"
