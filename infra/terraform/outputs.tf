output "resource_group_name" {
  description = "Resource group name"
  value       = azurerm_resource_group.main.name
}

output "keyvault_url" {
  description = "Azure Key Vault URL"
  value       = module.keyvault.keyvault_url
}

output "cosmos_endpoint" {
  description = "Cosmos DB endpoint"
  value       = module.cosmosdb.cosmos_endpoint
}

output "servicebus_namespace" {
  description = "Service Bus namespace FQDN"
  value       = module.servicebus.namespace_fqdn
}

output "api_app_fqdn" {
  description = "Webhook API Container App FQDN"
  value       = module.containerapp.api_fqdn
}

output "consumer_app_fqdn" {
  description = "Service Bus Consumer Container App FQDN"
  value       = module.containerapp.consumer_fqdn
}

output "appinsights_instrumentation_key" {
  description = "Application Insights instrumentation key"
  value       = module.monitoring.instrumentation_key
  sensitive   = true
}
