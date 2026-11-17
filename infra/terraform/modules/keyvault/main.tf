variable "name" { type = string }
variable "location" { type = string }
variable "resource_group_name" { type = string }
variable "tenant_id" { type = string }
variable "deployer_object_id" { type = string }
variable "reader_principal_id" { type = string }
variable "tags" { type = map(string) }
variable "secrets" {
  type      = map(string)
  sensitive = true
}

resource "random_string" "suffix" {
  length  = 4
  upper   = false
  special = false
}

resource "azurerm_key_vault" "this" {
  name                          = substr("kv-${var.name}-${random_string.suffix.result}", 0, 24)
  location                      = var.location
  resource_group_name           = var.resource_group_name
  tenant_id                     = var.tenant_id
  sku_name                      = "standard"
  rbac_authorization_enabled    = true
  purge_protection_enabled      = true
  soft_delete_retention_days    = 14
  public_network_access_enabled = true # tighten with private endpoints (see docs/architecture)
  tags                          = var.tags
}

resource "azurerm_role_assignment" "deployer" {
  scope                = azurerm_key_vault.this.id
  role_definition_name = "Key Vault Secrets Officer"
  principal_id         = var.deployer_object_id
}

resource "azurerm_role_assignment" "reader" {
  scope                = azurerm_key_vault.this.id
  role_definition_name = "Key Vault Secrets User"
  principal_id         = var.reader_principal_id
}

resource "azurerm_key_vault_secret" "this" {
  for_each     = toset(nonsensitive(keys(var.secrets)))
  name         = each.key
  value        = var.secrets[each.key]
  key_vault_id = azurerm_key_vault.this.id
  depends_on   = [azurerm_role_assignment.deployer]
}

output "vault_uri" { value = azurerm_key_vault.this.vault_uri }
output "secret_ids" { value = { for k, s in azurerm_key_vault_secret.this : k => s.versionless_id } }
