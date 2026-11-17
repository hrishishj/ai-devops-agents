data "azurerm_client_config" "current" {}

locals {
  name = "${var.prefix}-${var.environment}"
  tags = {
    project     = "ai-devops-agents"
    environment = var.environment
    managed_by  = "terraform"
  }
}

resource "azurerm_resource_group" "main" {
  name     = "rg-${local.name}"
  location = var.location
  tags     = local.tags
}

# One user-assigned identity shared by api + workers. Everything below grants it least privilege.
resource "azurerm_user_assigned_identity" "app" {
  name                = "id-${local.name}"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  tags                = local.tags
}

resource "azurerm_role_assignment" "acr_pull" {
  scope                = var.acr_id
  role_definition_name = "AcrPull"
  principal_id         = azurerm_user_assigned_identity.app.principal_id
}

module "monitoring" {
  source              = "./modules/monitoring"
  name                = local.name
  location            = var.location
  resource_group_name = azurerm_resource_group.main.name
  tags                = local.tags
}

module "keyvault" {
  source              = "./modules/keyvault"
  name                = local.name
  location            = var.location
  resource_group_name = azurerm_resource_group.main.name
  tenant_id           = data.azurerm_client_config.current.tenant_id
  deployer_object_id  = data.azurerm_client_config.current.object_id
  reader_principal_id = azurerm_user_assigned_identity.app.principal_id
  tags                = local.tags
  secrets = {
    "github-app-private-key" = var.github_app_private_key
    "github-webhook-secret"  = var.github_webhook_secret
    "alert-token"            = var.alert_token
    "admin-token"            = var.admin_token
    "database-url"           = module.postgres.connection_url
  }
}

module "postgres" {
  source              = "./modules/postgres"
  name                = local.name
  location            = var.location
  resource_group_name = azurerm_resource_group.main.name
  sku                 = var.postgres_sku
  zone_redundant      = var.environment == "prod"
  tags                = local.tags
}

module "servicebus" {
  source              = "./modules/servicebus"
  name                = local.name
  location            = var.location
  resource_group_name = azurerm_resource_group.main.name
  principal_id        = azurerm_user_assigned_identity.app.principal_id
  tags                = local.tags
}

module "openai" {
  source              = "./modules/openai"
  name                = local.name
  location            = var.openai_location
  resource_group_name = azurerm_resource_group.main.name
  principal_id        = azurerm_user_assigned_identity.app.principal_id
  tags                = local.tags
}

module "containerapps" {
  source              = "./modules/containerapps"
  name                = local.name
  location            = var.location
  resource_group_name = azurerm_resource_group.main.name
  log_analytics_id    = module.monitoring.log_analytics_id
  identity_id         = azurerm_user_assigned_identity.app.id
  identity_client_id  = azurerm_user_assigned_identity.app.client_id
  acr_login_server    = var.acr_login_server
  image               = var.image
  api_min_replicas    = var.api_min_replicas
  worker_replicas     = var.worker_replicas
  keyvault_secret_ids = module.keyvault.secret_ids
  tags                = local.tags

  env = {
    DA_ENVIRONMENT                           = var.environment
    DA_SERVICEBUS_NAMESPACE                  = module.servicebus.fqdn
    DA_OPENAI_ENDPOINT                       = module.openai.endpoint
    DA_GITHUB_APP_ID                         = var.github_app_id
    DA_INCIDENT_REPO                         = var.incident_repo
    DA_INCIDENT_INSTALLATION_ID              = tostring(var.incident_installation_id)
    DA_AUTOMATION_ACCOUNT_URL                = var.automation_account_url
    DA_LOG_ANALYTICS_WORKSPACE_ID            = module.monitoring.workspace_customer_id
    DA_APPLICATIONINSIGHTS_CONNECTION_STRING = module.monitoring.appinsights_connection_string
    DA_ADMIN_AUTH                            = var.admin_auth
    DA_CREATE_SCHEMA                         = "false" # schema managed by `devopsagents migrate`
    AZURE_CLIENT_ID                          = azurerm_user_assigned_identity.app.client_id
  }
}
