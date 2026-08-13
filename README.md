# BFF Auth Service (POC)

Python Backend-for-Frontend authentication service, scaffolded per the BFF Server POC
Cloud Architecture and DevOps PRD. Validates a secure, containerized deployment path to
Azure Container Apps with Azure Key Vault for secrets and Azure Cache for Redis for token
caching.

- Local test steps (WSL Ubuntu + Docker Desktop) → [TESTING.md](TESTING.md)
- Azure resource naming, RBAC/identity model, and step-by-step `az cli` setup → [AZURE_DEPLOYMENT.md](AZURE_DEPLOYMENT.md)

## Layout

```
app/
  main.py           FastAPI app: /health, /health/live, /health/ready, /token
  config.py         Environment-based settings (pydantic-settings)
  oauth.py          OAuth client-credentials token acquisition
  keyvault.py        Azure Key Vault access via DefaultAzureCredential (Managed Identity)
  redis_client.py    Redis connection
  token_cache.py     Redis token get/set, key format bff:{env}:{frontend}:{backend}:token
  logging_config.py  Structured logging with redaction of sensitive fields
Dockerfile
.github/workflows/ci-cd.yml   Build -> scan -> push to ACR -> deploy (GitHub OIDC)
```

## Local development

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env   # then edit values
uvicorn app.main:app --reload
```

Requires a reachable Redis instance and, for `/token`, an Azure Key Vault the caller's
credential can read (`az login` locally satisfies `DefaultAzureCredential`).

## Docker

```powershell
docker build -t bff-auth:local .
docker run -p 8000:8000 --env-file .env bff-auth:local
```

## Configuration

All non-sensitive settings are environment variables (see `.env.example`). Secrets
(`oauth-client-id`, `oauth-client-secret`) are read from Azure Key Vault at request time,
never from environment variables or source control.

## CI/CD

`.github/workflows/ci-cd.yml` builds the image, scans it with Trivy, pushes to Azure
Container Registry, and updates the Azure Container App revision, authenticating to Azure
via GitHub OIDC (no long-lived service principal secrets). Requires these repo/environment
secrets: `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID`, `ACR_NAME`,
`CONTAINER_APP_NAME`, `RESOURCE_GROUP`, `CONTAINER_APP_URL`.

`develop` deploys to the non-production Container App; `main` deploys to production
(gated by the GitHub `production` environment's required reviewers).

## Azure deployment

See [AZURE_DEPLOYMENT.md](AZURE_DEPLOYMENT.md) for the full resource naming convention,
identity/RBAC model, and ordered `az cli` steps to stand up Resource Groups, ACR, Key
Vault, Azure Cache for Redis, Log Analytics/App Insights, and the Container Apps
Environment/App for both non-production and production.
