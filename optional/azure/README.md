# Optional: deploy to Azure

**You do not need this.** The project runs for free with GitHub Actions or Docker Compose (see `docs/free-setup.md` and
`docs/local-docker.md`). This folder is for teams that want a managed, always-on deployment on Azure and accept the cost.

What is here: Terraform (`terraform/`) for a resource group, PostgreSQL, Service Bus, Azure OpenAI (Entra-only), Key Vault,
Container Apps (API, three workers, a migration job) and monitoring, with `dev`/`staging`/`prod` variable files. The deploy workflow
is `.github/workflows/deploy-azure.yml` (GitHub OIDC, no stored credentials); it is skipped until `AZURE_CLIENT_ID` is set.

Status: passes `terraform fmt` and `validate`; **never planned or applied** against a real subscription. Expect small fixes on the first
apply. Not covered: private networking, queue-driven autoscaling, an Easy Auth front door for the admin endpoints.

The Azure code adapters stay in `src/` as optional extras (`pip install ".[azure]"`; enable with `DA_LLM_PROVIDER=azure`,
`DA_SERVICEBUS_NAMESPACE`, `DA_AUTOMATION_ACCOUNT_URL`, `DA_LOG_ANALYTICS_WORKSPACE_ID`, `DA_SANDBOX_KIND=azure_sessions`).

```bash
cd optional/azure/terraform
terraform init -backend-config=envs/<env>.backend.hcl     # copy envs/backend.hcl.example first
terraform plan -var-file=envs/dev.tfvars
```
