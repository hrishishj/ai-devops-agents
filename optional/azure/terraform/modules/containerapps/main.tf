variable "name" { type = string }
variable "location" { type = string }
variable "resource_group_name" { type = string }
variable "log_analytics_id" { type = string }
variable "identity_id" { type = string }
variable "identity_client_id" { type = string }
variable "acr_login_server" { type = string }
variable "image" { type = string }
variable "api_min_replicas" { type = number }
variable "worker_replicas" { type = number }
variable "keyvault_secret_ids" { type = map(string) }
variable "env" {
  type      = map(string)
  sensitive = true
}
variable "tags" { type = map(string) }

locals {
  # Key Vault secret name -> environment variable
  secret_env = {
    "database-url"           = "DA_DATABASE_URL"
    "github-app-private-key" = "DA_GITHUB_APP_PRIVATE_KEY"
    "github-webhook-secret"  = "DA_GITHUB_WEBHOOK_SECRET"
    "alert-token"            = "DA_ALERT_TOKEN"
    "admin-token"            = "DA_ADMIN_TOKEN"
  }
  workers = toset(["review", "security", "incident"])
}

resource "azurerm_container_app_environment" "this" {
  name                       = "cae-${var.name}"
  location                   = var.location
  resource_group_name        = var.resource_group_name
  log_analytics_workspace_id = var.log_analytics_id
  tags                       = var.tags
}

resource "azurerm_container_app" "api" {
  name                         = "ca-${var.name}-api"
  container_app_environment_id = azurerm_container_app_environment.this.id
  resource_group_name          = var.resource_group_name
  revision_mode                = "Single"
  tags                         = var.tags

  identity {
    type         = "UserAssigned"
    identity_ids = [var.identity_id]
  }
  registry {
    server   = var.acr_login_server
    identity = var.identity_id
  }

  dynamic "secret" {
    for_each = local.secret_env
    content {
      name                = secret.key
      key_vault_secret_id = var.keyvault_secret_ids[secret.key]
      identity            = var.identity_id
    }
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
    min_replicas = var.api_min_replicas
    max_replicas = 10

    http_scale_rule {
      name                = "http"
      concurrent_requests = "50"
    }

    container {
      name   = "api"
      image  = var.image
      cpu    = 0.5
      memory = "1Gi"

      dynamic "env" {
        for_each = nonsensitive(var.env)
        content {
          name  = env.key
          value = env.value
        }
      }
      dynamic "env" {
        for_each = local.secret_env
        content {
          name        = env.value
          secret_name = env.key
        }
      }

      liveness_probe {
        transport = "HTTP"
        path      = "/health"
        port      = 8000
      }
      readiness_probe {
        transport = "HTTP"
        path      = "/health/ready"
        port      = 8000
      }
    }
  }
}

# Workers have no ingress. Replica count is fixed (queue-driven KEDA scaling with managed identity
# is a follow-up); each worker also sweeps the database so no job is ever stranded.
resource "azurerm_container_app" "worker" {
  for_each                     = local.workers
  name                         = "ca-${var.name}-${each.key}"
  container_app_environment_id = azurerm_container_app_environment.this.id
  resource_group_name          = var.resource_group_name
  revision_mode                = "Single"
  tags                         = var.tags

  identity {
    type         = "UserAssigned"
    identity_ids = [var.identity_id]
  }
  registry {
    server   = var.acr_login_server
    identity = var.identity_id
  }

  dynamic "secret" {
    for_each = local.secret_env
    content {
      name                = secret.key
      key_vault_secret_id = var.keyvault_secret_ids[secret.key]
      identity            = var.identity_id
    }
  }

  template {
    min_replicas = var.worker_replicas
    max_replicas = var.worker_replicas

    container {
      name    = "worker"
      image   = var.image
      cpu     = 0.5
      memory  = "1Gi"
      command = ["devopsagents", "worker", "--agent", each.key]

      dynamic "env" {
        for_each = nonsensitive(var.env)
        content {
          name  = env.key
          value = env.value
        }
      }
      dynamic "env" {
        for_each = local.secret_env
        content {
          name        = env.value
          secret_name = env.key
        }
      }
    }
  }
}

# One-shot job that applies Alembic migrations; started by the deploy workflow after each apply.
resource "azurerm_container_app_job" "migrate" {
  name                         = "caj-${var.name}-migrate"
  location                     = var.location
  resource_group_name          = var.resource_group_name
  container_app_environment_id = azurerm_container_app_environment.this.id
  replica_timeout_in_seconds   = 600
  replica_retry_limit          = 1
  tags                         = var.tags

  manual_trigger_config {
    parallelism              = 1
    replica_completion_count = 1
  }

  identity {
    type         = "UserAssigned"
    identity_ids = [var.identity_id]
  }
  registry {
    server   = var.acr_login_server
    identity = var.identity_id
  }

  secret {
    name                = "database-url"
    key_vault_secret_id = var.keyvault_secret_ids["database-url"]
    identity            = var.identity_id
  }

  template {
    container {
      name    = "migrate"
      image   = var.image
      cpu     = 0.25
      memory  = "0.5Gi"
      command = ["devopsagents", "migrate"]

      env {
        name        = "DA_DATABASE_URL"
        secret_name = "database-url"
      }
    }
  }
}

output "api_fqdn" { value = azurerm_container_app.api.ingress[0].fqdn }
output "migrate_job_name" { value = azurerm_container_app_job.migrate.name }
