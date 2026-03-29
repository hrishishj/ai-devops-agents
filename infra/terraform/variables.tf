variable "prefix" {
  description = "Short prefix for all resource names"
  type        = string
  default     = "aidvops"
}

variable "environment" {
  description = "Deployment environment: dev | staging | production"
  type        = string
  validation {
    condition     = contains(["dev", "staging", "production"], var.environment)
    error_message = "environment must be one of: dev, staging, production."
  }
}

variable "location" {
  description = "Azure region for most resources"
  type        = string
  default     = "eastus2"
}

variable "openai_location" {
  description = "Azure region for Azure OpenAI (limited availability)"
  type        = string
  default     = "eastus"
}

variable "acr_login_server" {
  description = "Azure Container Registry login server (e.g. myacr.azurecr.io)"
  type        = string
}

variable "image_tag" {
  description = "Docker image tag to deploy"
  type        = string
  default     = "latest"
}

variable "github_webhook_secret" {
  description = "Secret for validating GitHub webhook HMAC signatures"
  type        = string
  sensitive   = true
}

variable "github_app_id" {
  description = "GitHub App ID for bot authentication"
  type        = string
  default     = ""
}

variable "github_app_private_key" {
  description = "GitHub App RSA private key (PEM)"
  type        = string
  sensitive   = true
  default     = ""
}
