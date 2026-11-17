variable "name" { type = string }
variable "location" { type = string }
variable "resource_group_name" { type = string }
variable "principal_id" { type = string }
variable "tags" { type = map(string) }

resource "azurerm_servicebus_namespace" "this" {
  name                = "sb-${var.name}"
  location            = var.location
  resource_group_name = var.resource_group_name
  sku                 = "Standard"
  local_auth_enabled  = false # Entra only
  minimum_tls_version = "1.2"
  tags                = var.tags
}

# Messages carry only a job id; the database is the source of truth, so sessions are not needed.
resource "azurerm_servicebus_queue" "this" {
  for_each                             = toset(["review", "security", "incident"])
  name                                 = each.key
  namespace_id                         = azurerm_servicebus_namespace.this.id
  max_delivery_count                   = 5
  dead_lettering_on_message_expiration = true
  default_message_ttl                  = "P1D"
  lock_duration                        = "PT2M"
}

resource "azurerm_role_assignment" "data_owner" {
  scope                = azurerm_servicebus_namespace.this.id
  role_definition_name = "Azure Service Bus Data Owner"
  principal_id         = var.principal_id
}

output "fqdn" { value = "${azurerm_servicebus_namespace.this.name}.servicebus.windows.net" }
