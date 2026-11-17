variable "name" { type = string }
variable "location" { type = string }
variable "resource_group_name" { type = string }
variable "principal_id" { type = string }
variable "tags" { type = map(string) }

resource "azurerm_cognitive_account" "this" {
  name                          = "oai-${var.name}"
  location                      = var.location
  resource_group_name           = var.resource_group_name
  kind                          = "OpenAI"
  sku_name                      = "S0"
  custom_subdomain_name         = "oai-${var.name}"
  local_auth_enabled            = false # Entra only: no API keys exist
  public_network_access_enabled = true
  tags                          = var.tags
}

resource "azurerm_cognitive_deployment" "large" {
  name                 = "gpt-4o"
  cognitive_account_id = azurerm_cognitive_account.this.id
  model {
    format  = "OpenAI"
    name    = "gpt-4o"
    version = "2024-08-06"
  }
  sku {
    name     = "GlobalStandard"
    capacity = 50
  }
}

resource "azurerm_cognitive_deployment" "small" {
  name                 = "gpt-4o-mini"
  cognitive_account_id = azurerm_cognitive_account.this.id
  model {
    format  = "OpenAI"
    name    = "gpt-4o-mini"
    version = "2024-07-18"
  }
  sku {
    name     = "GlobalStandard"
    capacity = 50
  }
  depends_on = [azurerm_cognitive_deployment.large] # the API rejects parallel deployments
}

resource "azurerm_role_assignment" "openai_user" {
  scope                = azurerm_cognitive_account.this.id
  role_definition_name = "Cognitive Services OpenAI User"
  principal_id         = var.principal_id
}

output "endpoint" { value = azurerm_cognitive_account.this.endpoint }
