resource "azurerm_key_vault" "main" {
  name                        = "kv-${var.name_suffix}"
  location                    = var.location
  resource_group_name         = var.resource_group_name
  tenant_id                   = var.tenant_id
  sku_name                    = "standard"
  soft_delete_retention_days  = 7
  purge_protection_enabled    = true
  enable_rbac_authorization   = true
  tags                        = var.tags
}

# Grant the deploying principal Key Vault Administrator
resource "azurerm_role_assignment" "deployer_kv_admin" {
  scope                = azurerm_key_vault.main.id
  role_definition_name = "Key Vault Administrator"
  principal_id         = var.object_id
}

output "keyvault_id" {
  value = azurerm_key_vault.main.id
}

output "keyvault_url" {
  value = azurerm_key_vault.main.vault_uri
}

variable "name_suffix"         { type = string }
variable "resource_group_name" { type = string }
variable "location"            { type = string }
variable "tenant_id"           { type = string }
variable "object_id"           { type = string }
variable "tags"                { type = map(string) }
