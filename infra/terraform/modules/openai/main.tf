resource "azurerm_cognitive_account" "openai" {
  name                  = "oai-${var.name_suffix}"
  location              = var.location
  resource_group_name   = var.resource_group_name
  kind                  = "OpenAI"
  sku_name              = "S0"
  custom_subdomain_name = "oai-${var.name_suffix}"
  tags                  = var.tags

  network_acls {
    default_action = "Allow"
  }
}

resource "azurerm_cognitive_deployment" "gpt4o" {
  name                 = "gpt-4o"
  cognitive_account_id = azurerm_cognitive_account.openai.id

  model {
    format  = "OpenAI"
    name    = "gpt-4o"
    version = "2024-11-20"
  }

  scale {
    type     = "Standard"
    capacity = 40   # TPM in thousands
  }
}

# Store endpoint and key in Key Vault
resource "azurerm_key_vault_secret" "openai_endpoint" {
  name         = "azure-openai-endpoint"
  value        = azurerm_cognitive_account.openai.endpoint
  key_vault_id = var.keyvault_id
}

resource "azurerm_key_vault_secret" "openai_key" {
  name         = "azure-openai-api-key"
  value        = azurerm_cognitive_account.openai.primary_access_key
  key_vault_id = var.keyvault_id
}

output "openai_endpoint"   { value = azurerm_cognitive_account.openai.endpoint }
output "openai_account_id" { value = azurerm_cognitive_account.openai.id }

variable "name_suffix"         { type = string }
variable "resource_group_name" { type = string }
variable "location"            { type = string }
variable "keyvault_id"         { type = string }
variable "tags"                { type = map(string) }
