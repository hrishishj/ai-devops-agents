resource "azurerm_cosmosdb_account" "main" {
  name                = "cosmos-${var.name_suffix}"
  location            = var.location
  resource_group_name = var.resource_group_name
  offer_type          = "Standard"
  kind                = "GlobalDocumentDB"
  tags                = var.tags

  consistency_policy {
    consistency_level       = "Session"
    max_interval_in_seconds = 5
    max_staleness_prefix    = 100
  }

  geo_location {
    location          = var.location
    failover_priority = 0
  }

  backup {
    type                = "Periodic"
    interval_in_minutes = 240
    retention_in_hours  = 720
    storage_redundancy  = "Geo"
  }

  capabilities {
    name = "EnableServerless"
  }
}

resource "azurerm_cosmosdb_sql_database" "main" {
  name                = "ai_agents_db"
  resource_group_name = var.resource_group_name
  account_name        = azurerm_cosmosdb_account.main.name
}

locals {
  containers = {
    code_reviews  = "/repo"
    it_incidents  = "/alert_id"
    test_reports  = "/repo"
  }
}

resource "azurerm_cosmosdb_sql_container" "containers" {
  for_each = local.containers

  name                  = each.key
  resource_group_name   = var.resource_group_name
  account_name          = azurerm_cosmosdb_account.main.name
  database_name         = azurerm_cosmosdb_sql_database.main.name
  partition_key_path    = each.value
  partition_key_version = 1

  # TTL enabled at account level; per-item ttl field controls actual expiry
  default_ttl = -1

  indexing_policy {
    indexing_mode = "consistent"

    included_path { path = "/*" }
    excluded_path { path = "/\"_etag\"/?" }
  }
}

# Store connection details in Key Vault
resource "azurerm_key_vault_secret" "cosmos_endpoint" {
  name         = "cosmos-endpoint"
  value        = azurerm_cosmosdb_account.main.endpoint
  key_vault_id = var.keyvault_id
}

resource "azurerm_key_vault_secret" "cosmos_key" {
  name         = "cosmos-key"
  value        = azurerm_cosmosdb_account.main.primary_key
  key_vault_id = var.keyvault_id
}

output "cosmos_endpoint"    { value = azurerm_cosmosdb_account.main.endpoint }
output "cosmos_account_id"  { value = azurerm_cosmosdb_account.main.id }

variable "name_suffix"         { type = string }
variable "resource_group_name" { type = string }
variable "location"            { type = string }
variable "keyvault_id"         { type = string }
variable "tags"                { type = map(string) }
