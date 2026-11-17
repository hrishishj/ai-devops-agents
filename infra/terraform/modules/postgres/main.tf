variable "name" { type = string }
variable "location" { type = string }
variable "resource_group_name" { type = string }
variable "sku" { type = string }
variable "zone_redundant" { type = bool }
variable "tags" { type = map(string) }

resource "random_password" "admin" {
  length  = 32
  special = false
}

resource "azurerm_postgresql_flexible_server" "this" {
  name                          = "psql-${var.name}"
  location                      = var.location
  resource_group_name           = var.resource_group_name
  version                       = "16"
  sku_name                      = var.sku
  storage_mb                    = 32768
  administrator_login           = "daadmin"
  administrator_password        = random_password.admin.result
  backup_retention_days         = 14
  public_network_access_enabled = true # Azure-services firewall rule below; use a private endpoint when hardening
  tags                          = var.tags

  dynamic "high_availability" {
    for_each = var.zone_redundant ? [1] : []
    content {
      mode = "ZoneRedundant"
    }
  }

  lifecycle {
    ignore_changes = [zone]
  }
}

resource "azurerm_postgresql_flexible_server_database" "this" {
  name      = "devopsagents"
  server_id = azurerm_postgresql_flexible_server.this.id
  charset   = "UTF8"
  collation = "en_US.utf8"
}

resource "azurerm_postgresql_flexible_server_firewall_rule" "azure" {
  name             = "allow-azure-services"
  server_id        = azurerm_postgresql_flexible_server.this.id
  start_ip_address = "0.0.0.0"
  end_ip_address   = "0.0.0.0"
}

output "connection_url" {
  value     = "postgresql+psycopg://daadmin:${random_password.admin.result}@${azurerm_postgresql_flexible_server.this.fqdn}:5432/devopsagents?sslmode=require"
  sensitive = true
}
