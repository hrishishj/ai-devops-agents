#!/usr/bin/env bash
# ============================================================
# bootstrap.sh — One-time Azure infrastructure bootstrap
#
# Creates the Terraform state storage account and ACR, then
# runs `terraform init` + `terraform apply` for the full stack.
#
# Prerequisites:
#   - Azure CLI authenticated: az login
#   - Terraform >= 1.7 installed
#   - jq installed (brew install jq / apt install jq)
#
# Usage:
#   ./scripts/bootstrap.sh --env staging --location eastus2
# ============================================================

set -euo pipefail

# ─── Defaults ────────────────────────────────────────────────
ENV="staging"
LOCATION="eastus2"
PREFIX="aidvops"

# ─── Argument parsing ────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case $1 in
        --env)      ENV="$2";      shift 2 ;;
        --location) LOCATION="$2"; shift 2 ;;
        --prefix)   PREFIX="$2";   shift 2 ;;
        *) echo "Unknown argument: $1"; exit 1 ;;
    esac
done

echo "============================================================"
echo " AI DevOps Agents — Bootstrap"
echo " Environment : $ENV"
echo " Location    : $LOCATION"
echo " Prefix      : $PREFIX"
echo "============================================================"

# ─── Variables ───────────────────────────────────────────────
TFSTATE_RG="tfstate-rg"
TFSTATE_SA="tfstate${PREFIX}${ENV}"   # storage account names must be globally unique
TFSTATE_CONTAINER="tfstate"
ACR_NAME="${PREFIX}acr${ENV}"
APP_RG="${PREFIX}-${ENV}-rg"

SUBSCRIPTION_ID=$(az account show --query id -o tsv)
echo "Using subscription: $SUBSCRIPTION_ID"

# ─── 1. Terraform state storage ──────────────────────────────
echo ""
echo ">>> Creating Terraform state storage..."

az group create \
    --name "$TFSTATE_RG" \
    --location "$LOCATION" \
    --output none

az storage account create \
    --name "$TFSTATE_SA" \
    --resource-group "$TFSTATE_RG" \
    --location "$LOCATION" \
    --sku Standard_LRS \
    --kind StorageV2 \
    --min-tls-version TLS1_2 \
    --allow-blob-public-access false \
    --output none

az storage container create \
    --name "$TFSTATE_CONTAINER" \
    --account-name "$TFSTATE_SA" \
    --output none

echo "    Terraform state storage: $TFSTATE_SA/$TFSTATE_CONTAINER"

# ─── 2. Azure Container Registry ─────────────────────────────
echo ""
echo ">>> Creating Azure Container Registry..."

az group create \
    --name "$APP_RG" \
    --location "$LOCATION" \
    --output none

az acr create \
    --name "$ACR_NAME" \
    --resource-group "$APP_RG" \
    --sku Standard \
    --admin-enabled false \
    --output none

ACR_SERVER=$(az acr show --name "$ACR_NAME" --query loginServer -o tsv)
echo "    ACR login server: $ACR_SERVER"

# ─── 3. Build and push initial Docker image ───────────────────
echo ""
echo ">>> Building initial Docker image via ACR build task..."

cd "$(dirname "$0")/.."

az acr build \
    --registry "$ACR_NAME" \
    --image "ai-devops-agents:latest" \
    --file Dockerfile \
    .

echo "    Image pushed: ${ACR_SERVER}/ai-devops-agents:latest"

# ─── 4. Terraform init ────────────────────────────────────────
echo ""
echo ">>> Initialising Terraform..."

cd infra/terraform

# Write backend config
cat > backend.hcl <<-EOF
resource_group_name  = "${TFSTATE_RG}"
storage_account_name = "${TFSTATE_SA}"
container_name       = "${TFSTATE_CONTAINER}"
key                  = "ai-devops-agents-${ENV}.tfstate"
EOF

terraform init -backend-config=backend.hcl -reconfigure

# ─── 5. Terraform plan ───────────────────────────────────────
echo ""
echo ">>> Running Terraform plan..."

terraform plan \
    -var="prefix=${PREFIX}" \
    -var="environment=${ENV}" \
    -var="location=${LOCATION}" \
    -var="acr_login_server=${ACR_SERVER}" \
    -var="image_tag=latest" \
    -var-file="terraform.tfvars" \
    -out="tfplan-${ENV}.binary"

# ─── 6. Terraform apply ──────────────────────────────────────
echo ""
read -rp "Apply the plan? [y/N] " confirm
if [[ "$confirm" =~ ^[Yy]$ ]]; then
    terraform apply "tfplan-${ENV}.binary"
    echo ""
    echo "✅ Infrastructure provisioned successfully!"
    echo ""
    echo "Outputs:"
    terraform output
else
    echo "Apply cancelled."
    exit 0
fi

# ─── 7. Post-apply: store GitHub secrets in Key Vault ─────────
echo ""
echo ">>> Storing GitHub secrets in Key Vault..."

KV_URL=$(terraform output -raw keyvault_url 2>/dev/null || echo "")

if [[ -n "$KV_URL" && -f "../../.env" ]]; then
    echo "    Loading secrets from .env into Key Vault..."
    while IFS='=' read -r key value; do
        [[ "$key" =~ ^#.*$ || -z "$key" ]] && continue
        secret_name=$(echo "$key" | tr '[:upper:]_' '[:lower:]-')
        az keyvault secret set \
            --vault-name "$(basename "$KV_URL")" \
            --name "$secret_name" \
            --value "$value" \
            --output none 2>/dev/null || true
    done < "../../.env"
    echo "    Secrets loaded."
fi

echo ""
echo "============================================================"
echo " Bootstrap complete!"
echo " API URL: https://$(terraform output -raw api_app_fqdn 2>/dev/null || echo '<deploy pending>')"
echo "============================================================"
