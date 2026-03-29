terraform {
  required_version = ">= 1.7.0"
  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 3.100"
    }
    azuread = {
      source  = "hashicorp/azuread"
      version = "~> 2.47"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }

  backend "azurerm" {
    resource_group_name  = "tfstate-rg"
    storage_account_name = "tfstateaidvops"
    container_name       = "tfstate"
    key                  = "ai-devops-agents.tfstate"
  }
}

provider "azurerm" {
  features {
    key_vault {
      purge_soft_delete_on_destroy    = false
      recover_soft_deleted_key_vaults = true
    }
  }
}

# --------------------------------------------------------------------------- #
# Data sources
# --------------------------------------------------------------------------- #

data "azurerm_client_config" "current" {}

data "azurerm_subscription" "current" {}

# --------------------------------------------------------------------------- #
# Resource Group
# --------------------------------------------------------------------------- #

resource "azurerm_resource_group" "main" {
  name     = "${var.prefix}-${var.environment}-rg"
  location = var.location
  tags     = local.common_tags
}

# --------------------------------------------------------------------------- #
# Locals
# --------------------------------------------------------------------------- #

locals {
  common_tags = {
    project     = "ai-devops-agents"
    environment = var.environment
    managed_by  = "terraform"
  }
  name_suffix = "${var.prefix}-${var.environment}"
}

# --------------------------------------------------------------------------- #
# Modules
# --------------------------------------------------------------------------- #

module "keyvault" {
  source              = "./modules/keyvault"
  name_suffix         = local.name_suffix
  resource_group_name = azurerm_resource_group.main.name
  location            = var.location
  tenant_id           = data.azurerm_client_config.current.tenant_id
  object_id           = data.azurerm_client_config.current.object_id
  tags                = local.common_tags
}

module "openai" {
  source              = "./modules/openai"
  name_suffix         = local.name_suffix
  resource_group_name = azurerm_resource_group.main.name
  location            = var.openai_location
  keyvault_id         = module.keyvault.keyvault_id
  tags                = local.common_tags
}

module "cosmosdb" {
  source              = "./modules/cosmosdb"
  name_suffix         = local.name_suffix
  resource_group_name = azurerm_resource_group.main.name
  location            = var.location
  keyvault_id         = module.keyvault.keyvault_id
  tags                = local.common_tags
}

module "redis" {
  source              = "./modules/redis"
  name_suffix         = local.name_suffix
  resource_group_name = azurerm_resource_group.main.name
  location            = var.location
  keyvault_id         = module.keyvault.keyvault_id
  tags                = local.common_tags
}

module "servicebus" {
  source              = "./modules/servicebus"
  name_suffix         = local.name_suffix
  resource_group_name = azurerm_resource_group.main.name
  location            = var.location
  keyvault_id         = module.keyvault.keyvault_id
  tags                = local.common_tags
}

module "monitoring" {
  source              = "./modules/monitoring"
  name_suffix         = local.name_suffix
  resource_group_name = azurerm_resource_group.main.name
  location            = var.location
  keyvault_id         = module.keyvault.keyvault_id
  tags                = local.common_tags
}

module "containerapp" {
  source              = "./modules/containerapp"
  name_suffix         = local.name_suffix
  resource_group_name = azurerm_resource_group.main.name
  location            = var.location
  keyvault_url        = module.keyvault.keyvault_url
  acr_login_server    = var.acr_login_server
  image_tag           = var.image_tag
  appinsights_connection_string = module.monitoring.appinsights_connection_string
  tags                = local.common_tags

  depends_on = [
    module.keyvault,
    module.openai,
    module.cosmosdb,
    module.redis,
    module.servicebus,
    module.monitoring,
  ]
}
