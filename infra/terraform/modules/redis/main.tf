resource "azurerm_redis_cache" "main" {
  name                = "redis-${var.name_suffix}"
  location            = var.location
  resource_group_name = var.resource_group_name
  capacity            = 1
  family              = "C"
  sku_name            = "Standard"
  enable_non_ssl_port = false
  minimum_tls_version = "1.2"
  tags                = var.tags

  redis_configuration {
    maxmemory_reserved = 50
    maxmemory_delta    = 50
    maxmemory_policy   = "allkeys-lru"
  }

  patch_schedule {
    day_of_week    = "Sunday"
    start_hour_utc = 2
  }
}

resource "azurerm_key_vault_secret" "redis_host" {
  name         = "redis-host"
  value        = azurerm_redis_cache.main.hostname
  key_vault_id = var.keyvault_id
}

resource "azurerm_key_vault_secret" "redis_password" {
  name         = "redis-password"
  value        = azurerm_redis_cache.main.primary_access_key
  key_vault_id = var.keyvault_id
}

output "redis_hostname"    { value = azurerm_redis_cache.main.hostname }
output "redis_port"        { value = azurerm_redis_cache.main.ssl_port }
output "redis_cache_id"    { value = azurerm_redis_cache.main.id }

variable "name_suffix"         { type = string }
variable "resource_group_name" { type = string }
variable "location"            { type = string }
variable "keyvault_id"         { type = string }
variable "tags"                { type = map(string) }
