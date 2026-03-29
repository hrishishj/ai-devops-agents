resource "azurerm_servicebus_namespace" "main" {
  name                = "sb-${var.name_suffix}"
  location            = var.location
  resource_group_name = var.resource_group_name
  sku                 = "Standard"
  tags                = var.tags
}

locals {
  queues = {
    coding-copilot-queue = {
      max_delivery_count              = 5
      lock_duration                   = "PT2M"
      message_ttl                     = "P14D"
      dead_lettering_on_msg_expiration = true
    }
    it-ops-queue = {
      max_delivery_count              = 3
      lock_duration                   = "PT5M"
      message_ttl                     = "P7D"
      dead_lettering_on_msg_expiration = true
    }
    test-security-queue = {
      max_delivery_count              = 5
      lock_duration                   = "PT2M"
      message_ttl                     = "P14D"
      dead_lettering_on_msg_expiration = true
    }
  }
}

resource "azurerm_servicebus_queue" "queues" {
  for_each = local.queues

  name                                 = each.key
  namespace_id                         = azurerm_servicebus_namespace.main.id
  max_delivery_count                   = each.value.max_delivery_count
  lock_duration                        = each.value.lock_duration
  default_message_ttl                  = each.value.message_ttl
  dead_lettering_on_message_expiration = each.value.dead_lettering_on_msg_expiration
  enable_partitioning                  = false
}

resource "azurerm_key_vault_secret" "servicebus_connection_string" {
  name         = "servicebus-connection-string"
  value        = azurerm_servicebus_namespace.main.default_primary_connection_string
  key_vault_id = var.keyvault_id
}

output "namespace_fqdn"      { value = "${azurerm_servicebus_namespace.main.name}.servicebus.windows.net" }
output "namespace_id"        { value = azurerm_servicebus_namespace.main.id }

variable "name_suffix"         { type = string }
variable "resource_group_name" { type = string }
variable "location"            { type = string }
variable "keyvault_id"         { type = string }
variable "tags"                { type = map(string) }
