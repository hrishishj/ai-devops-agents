output "api_url" { value = "https://${module.containerapps.api_fqdn}" }
output "github_webhook_url" { value = "https://${module.containerapps.api_fqdn}/webhooks/github" }
output "azure_monitor_webhook_url" { value = "https://${module.containerapps.api_fqdn}/webhooks/azure-monitor" }
output "resource_group" { value = azurerm_resource_group.main.name }
output "migrate_job_name" { value = module.containerapps.migrate_job_name }
output "identity_client_id" { value = azurerm_user_assigned_identity.app.client_id }
output "key_vault_uri" { value = module.keyvault.vault_uri }
