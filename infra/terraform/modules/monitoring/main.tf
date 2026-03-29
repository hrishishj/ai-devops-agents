resource "azurerm_log_analytics_workspace" "main" {
  name                = "law-${var.name_suffix}"
  location            = var.location
  resource_group_name = var.resource_group_name
  sku                 = "PerGB2018"
  retention_in_days   = 30
  tags                = var.tags
}

resource "azurerm_application_insights" "main" {
  name                = "appi-${var.name_suffix}"
  location            = var.location
  resource_group_name = var.resource_group_name
  workspace_id        = azurerm_log_analytics_workspace.main.id
  application_type    = "web"
  tags                = var.tags
}

# Smart detection alert — failure anomalies
resource "azurerm_monitor_smart_detector_alert_rule" "failure_anomalies" {
  name                = "Failure Anomalies - ${var.name_suffix}"
  resource_group_name = var.resource_group_name
  severity            = "Sev3"
  scope_resource_ids  = [azurerm_application_insights.main.id]
  frequency           = "PT1M"
  detector_type       = "FailureAnomaliesDetector"

  action_group {
    ids = [azurerm_monitor_action_group.ops.id]
  }
}

# Action group — sends to Service Bus webhook in production
resource "azurerm_monitor_action_group" "ops" {
  name                = "ag-${var.name_suffix}-ops"
  resource_group_name = var.resource_group_name
  short_name          = "ops"
  tags                = var.tags
}

# Store connection string in Key Vault
resource "azurerm_key_vault_secret" "appinsights_connection_string" {
  name         = "appinsights-connection-string"
  value        = azurerm_application_insights.main.connection_string
  key_vault_id = var.keyvault_id
}

resource "azurerm_key_vault_secret" "appinsights_instrumentation_key" {
  name         = "appinsights-instrumentation-key"
  value        = azurerm_application_insights.main.instrumentation_key
  key_vault_id = var.keyvault_id
}

output "appinsights_id"                  { value = azurerm_application_insights.main.id }
output "appinsights_connection_string"   { value = azurerm_application_insights.main.connection_string }
output "instrumentation_key"             { value = azurerm_application_insights.main.instrumentation_key
                                           sensitive = true }
output "log_analytics_workspace_id"      { value = azurerm_log_analytics_workspace.main.id }

variable "name_suffix"         { type = string }
variable "resource_group_name" { type = string }
variable "location"            { type = string }
variable "keyvault_id"         { type = string }
variable "tags"                { type = map(string) }
