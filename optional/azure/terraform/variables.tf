variable "subscription_id" { type = string }

variable "environment" {
  type = string
  validation {
    condition     = contains(["dev", "staging", "prod"], var.environment)
    error_message = "environment must be dev, staging or prod."
  }
}

variable "location" {
  type    = string
  default = "eastus2"
}

variable "openai_location" {
  type        = string
  default     = "eastus2"
  description = "Region with GPT-4o quota."
}

variable "prefix" {
  type    = string
  default = "daagents"
}

variable "image" {
  type        = string
  description = "Full image reference, e.g. myacr.azurecr.io/devopsagents:<sha>"
}

variable "acr_login_server" { type = string }
variable "acr_id" {
  type        = string
  description = "Resource id of the container registry (grants AcrPull to the app identity)."
}

variable "github_app_id" { type = string }

variable "github_app_private_key" {
  type      = string
  sensitive = true
}

variable "github_webhook_secret" {
  type      = string
  sensitive = true
}

variable "alert_token" {
  type      = string
  sensitive = true
}

variable "admin_token" {
  type      = string
  sensitive = true
}

variable "admin_auth" {
  type        = string
  default     = "token"
  description = "token (shared secret) or easyauth (Entra principal; ingress must be reachable only via Easy Auth/APIM)."
  validation {
    condition     = contains(["token", "easyauth"], var.admin_auth)
    error_message = "admin_auth must be token or easyauth."
  }
}

variable "incident_repo" {
  type    = string
  default = ""
}

variable "incident_installation_id" {
  type    = number
  default = 0
}

variable "automation_account_url" {
  type    = string
  default = ""
}

variable "api_min_replicas" {
  type    = number
  default = 2
}

variable "worker_replicas" {
  type    = number
  default = 1
}

variable "postgres_sku" {
  type    = string
  default = "B_Standard_B2s"
}
