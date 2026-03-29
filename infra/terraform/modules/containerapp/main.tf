resource "azurerm_container_app_environment" "main" {
  name                       = "cae-${var.name_suffix}"
  location                   = var.location
  resource_group_name        = var.resource_group_name
  tags                       = var.tags
}

# Managed Identity for both apps
resource "azurerm_user_assigned_identity" "agents" {
  name                = "id-${var.name_suffix}-agents"
  location            = var.location
  resource_group_name = var.resource_group_name
  tags                = var.tags
}

# Grant identity access to Key Vault secrets
resource "azurerm_role_assignment" "kv_secrets_user" {
  scope                = var.keyvault_id
  role_definition_name = "Key Vault Secrets User"
  principal_id         = azurerm_user_assigned_identity.agents.principal_id
}

# ------------------------------------------------------------------ #
# Webhook API Container App
# ------------------------------------------------------------------ #

resource "azurerm_container_app" "api" {
  name                         = "ca-${var.name_suffix}-api"
  container_app_environment_id = azurerm_container_app_environment.main.id
  resource_group_name          = var.resource_group_name
  revision_mode                = "Single"
  tags                         = var.tags

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.agents.id]
  }

  ingress {
    external_enabled = true
    target_port      = 8000
    transport        = "http"

    traffic_weight {
      percentage      = 100
      latest_revision = true
    }
  }

  template {
    min_replicas = 1
    max_replicas = 10

    http_scale_rule {
      name                = "http-scaling"
      concurrent_requests = "50"
    }

    container {
      name   = "api"
      image  = "${var.acr_login_server}/ai-devops-agents:${var.image_tag}"
      cpu    = 1.0
      memory = "2Gi"

      command = ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]

      env {
        name  = "AZURE_KEYVAULT_URL"
        value = var.keyvault_url
      }
      env {
        name  = "OTEL_SERVICE_NAME"
        value = "ai-devops-agents-api"
      }
      env {
        name  = "APPLICATIONINSIGHTS_CONNECTION_STRING"
        value = var.appinsights_connection_string
      }
      env {
        name  = "AZURE_CLIENT_ID"
        value = azurerm_user_assigned_identity.agents.client_id
      }

      liveness_probe {
        transport        = "HTTP"
        path             = "/health"
        port             = 8000
        initial_delay    = 10
        period_seconds   = 30
        timeout_seconds  = 5
        failure_count_threshold = 3
      }

      readiness_probe {
        transport        = "HTTP"
        path             = "/health/ready"
        port             = 8000
        initial_delay    = 15
        period_seconds   = 10
        timeout_seconds  = 5
        failure_count_threshold = 3
      }
    }
  }
}

# ------------------------------------------------------------------ #
# Service Bus Consumer Container App (scale-to-zero)
# ------------------------------------------------------------------ #

resource "azurerm_container_app" "consumer" {
  name                         = "ca-${var.name_suffix}-consumer"
  container_app_environment_id = azurerm_container_app_environment.main.id
  resource_group_name          = var.resource_group_name
  revision_mode                = "Single"
  tags                         = var.tags

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.agents.id]
  }

  template {
    min_replicas = 0   # scale-to-zero when queues are empty
    max_replicas = 5

    # KEDA Service Bus scaling
    custom_scale_rule {
      name             = "servicebus-scaling"
      custom_rule_type = "azure-servicebus"
      metadata = {
        queueName          = "coding-copilot-queue"
        messageCount       = "5"
        activationQueueLength = "1"
      }
    }

    container {
      name   = "consumer"
      image  = "${var.acr_login_server}/ai-devops-agents:${var.image_tag}"
      cpu    = 0.5
      memory = "1Gi"

      command = ["python", "-m", "api.service_bus_consumer"]

      env {
        name  = "AZURE_KEYVAULT_URL"
        value = var.keyvault_url
      }
      env {
        name  = "OTEL_SERVICE_NAME"
        value = "ai-devops-agents-consumer"
      }
      env {
        name  = "APPLICATIONINSIGHTS_CONNECTION_STRING"
        value = var.appinsights_connection_string
      }
      env {
        name  = "AZURE_CLIENT_ID"
        value = azurerm_user_assigned_identity.agents.client_id
      }
    }
  }
}

output "api_fqdn"         { value = azurerm_container_app.api.ingress[0].fqdn }
output "consumer_fqdn"    { value = "" }
output "managed_identity_client_id" { value = azurerm_user_assigned_identity.agents.client_id }
output "managed_identity_id"        { value = azurerm_user_assigned_identity.agents.id }

variable "name_suffix"                    { type = string }
variable "resource_group_name"            { type = string }
variable "location"                       { type = string }
variable "keyvault_url"                   { type = string }
variable "keyvault_id"                    { type = string }
variable "acr_login_server"               { type = string }
variable "image_tag"                      { type = string }
variable "appinsights_connection_string"  { type = string }
variable "tags"                           { type = map(string) }
