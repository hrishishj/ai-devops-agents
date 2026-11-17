environment      = "dev"
location         = "eastus2"
api_min_replicas = 1
worker_replicas  = 1
postgres_sku     = "B_Standard_B1ms"

# Supply via -var or TF_VAR_*: subscription_id, image, acr_login_server, acr_id,
# github_app_id, github_app_private_key, github_webhook_secret, alert_token, admin_token
