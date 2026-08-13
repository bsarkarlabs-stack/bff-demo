# Azure Deployment Plan — BFF Auth Service

Target: a secure, repeatable, enterprise-shaped-but-POC-sized Azure footprint for the
Python BFF, deployable via `az cli` today and liftable into Bicep/Terraform later without
renaming anything.

This is the execution plan for PRD sections 7–13, 18–19, 21, 26 and the checklist in
section 31. Read [TESTING.md](TESTING.md) first — this plan assumes the image already
builds, runs, and passes health checks locally (it does, as of the last container test).

---

## 1. Design decisions (and why)

A few calls were made beyond what the PRD spells out literally, aimed at "simple now,
doesn't need a redo later":

| Decision | Choice | Why |
|---|---|---|
| ACR: one shared registry vs. one per environment | **One shared ACR** (`acrbffeus`, in its own resource group) | The whole point of image tagging by git SHA (PRD §12) is *build once, promote the same artifact* from non-prod to prod. A separate ACR per environment forces rebuilding (or manually copying) the image for prod — that's exactly the drift the PRD is trying to avoid. Access is still segregated by RBAC: non-prod's identity gets `AcrPush`+`AcrPull`, prod's identity gets `AcrPull` only. |
| Runtime identity vs. deployment identity | **Two separate identities per environment** | The identity GitHub Actions federates into (needs to push images / update the app) is not the same identity the *running container* uses (needs to read Key Vault secrets). Collapsing them into one would give the app's runtime more privilege than it needs — violates least-privilege (PRD §26). |
| GitHub → Azure auth | **User-Assigned Managed Identity + federated credential (OIDC)**, not an App Registration/Service Principal | UAMIs support federated identity credentials directly since Azure added workload-identity federation to them — no client secret to rotate or leak, one less resource type to manage than a full App Registration. |
| Redis service | **Azure Managed Redis** (`az redisenterprise`, `Balanced_B0` SKU), not classic Azure Cache for Redis | Microsoft has been steering new deployments to Azure Managed Redis — it's built on the Redis Enterprise engine, generally better price/performance than the classic Basic/Standard/Premium tiers, and is the service Microsoft is investing in going forward. Deploying the classic tier today would mean a forced migration later; provisioning Managed Redis now is the "doesn't need a redo" choice. Created with `--clustering-policy NoCluster` so the app's plain (non-cluster-aware) `redis-py` client works unchanged. |
| Redis auth | Access key stored **in Key Vault**, container pulls it via its own managed identity's Key Vault access | Entra ID (passwordless) auth for Redis exists but isn't uniformly simple across every client SDK yet. Key-in-Key-Vault is the pragmatic POC-to-prod-safe default the PRD already describes (§13); it still means the key is never in source, env vars, or the image. |
| Networking | **Public endpoints, POC-wide** | Matches PRD §27 explicitly — VNet/Private Endpoint is called out as future hardening, not a POC blocker. Everything below is written so adding Private Endpoints later doesn't change resource names, only adds NICs/DNS. |
| Non-Prod topology | **One Container App, one Redis, one Key Vault shared by DEV/QA/UAT**, hostname-routed | Directly per PRD §7. Production always gets its own copy of everything. |

---

## 2. Naming convention

Pattern: `<resource-type>-<project>-<environment>-<region>` (no hyphens where the resource
type forbids them). Project = `bff`, region = `eus` (East US).

| Environment token | Meaning |
|---|---|
| `np` | Non-production resource group / shared resources (routes DEV, QA, UAT by hostname) |
| `prod` | Production |
| `shared` | Cross-environment platform resources (currently: ACR only) |

### Resource inventory

| Resource | Non-Prod name | Prod name | Notes |
|---|---|---|---|
| Resource Group | `rg-bff-np-eus` | `rg-bff-prod-eus` | plus `rg-bff-shared-eus` for ACR |
| Container Registry | `acrbffeus` (in `rg-bff-shared-eus`) | *(same, shared)* | globally unique, no hyphens |
| Container Apps Environment | `cae-bff-np-eus` | `cae-bff-prod-eus` | |
| Container App | `ca-bff-np-eus` | `ca-bff-prod-eus` | |
| Azure Managed Redis (redisenterprise cluster) | `redis-bff-np-eus` | `redis-bff-prod-eus` | `Balanced_B0`, `NoCluster` policy, default database |
| Key Vault | `kv-bff-np-eus` | `kv-bff-prod-eus` | globally unique, ≤24 chars |
| Log Analytics Workspace | `law-bff-np-eus` | `law-bff-prod-eus` | |
| Application Insights | `appi-bff-np-eus` | `appi-bff-prod-eus` | |
| Managed Identity (runtime) | `id-bff-runtime-np-eus` | `id-bff-runtime-prod-eus` | assigned to the Container App |
| Managed Identity (GitHub OIDC deploy) | `id-bff-deploy-np-eus` | `id-bff-deploy-prod-eus` | federated to GitHub Actions |

### Tagging standard (apply to every resource)

```
project=bff
environment=np|prod
managedBy=manual-poc            # switch to "iac" once Bicep/Terraform lands
owner=<team-or-email>
```

### Hostnames (PRD §7)

```
bff-dev.company.com   \
bff-qa.company.com     >  all resolve to ca-bff-np-eus, env selected by Host header
bff-uat.company.com   /
bff-prod.company.com  ->  ca-bff-prod-eus
```

---

## 3. Build order

Later steps depend on earlier ones (Key Vault before Container App, identities before RBAC
assignments, etc.), so create in this order:

```
1. Resource Groups
2. Managed Identities (runtime + deploy, both environments)
3. GitHub OIDC federation on the deploy identities
4. Azure Container Registry (shared) + RBAC for deploy identities
5. Log Analytics Workspace + Application Insights (per environment)
6. Key Vault (per environment) + secrets + RBAC for runtime identities
7. Azure Cache for Redis (per environment) -> access key into Key Vault
8. Container Apps Environment (per environment), wired to Log Analytics
9. Container App (per environment), with runtime identity attached
10. GitHub Actions repo/environment secrets
11. Validation
```

Steps 2–9 below are written once with `<ENV>` placeholders; run twice (`np`, `prod`) except
where marked shared.

---

## 4. Step-by-step (az CLI)

### 4.0 Prerequisites

```powershell
az login
az account set --subscription "<subscription-id-or-name>"
$LOCATION = "eastus"
```

### 4.1 Resource Groups

```powershell
az group create -n rg-bff-np-eus -l $LOCATION --tags project=bff environment=np
az group create -n rg-bff-prod-eus -l $LOCATION --tags project=bff environment=prod
az group create -n rg-bff-shared-eus -l $LOCATION --tags project=bff environment=shared
```

### 4.2 Managed Identities

```powershell
foreach ($ENV in @("np","prod")) {
  az identity create -g "rg-bff-$ENV-eus" -n "id-bff-runtime-$ENV-eus" -l $LOCATION
  az identity create -g "rg-bff-$ENV-eus" -n "id-bff-deploy-$ENV-eus" -l $LOCATION
}
```

### 4.3 GitHub OIDC federation (on each deploy identity)

Repeat per environment, mapping branch → environment (`develop` → np, `master` → prod), per
PRD §20:

```powershell
$RG = "rg-bff-np-eus"
$IDENTITY = "id-bff-deploy-np-eus"
$SUBJECT = "repo:<github-org>/<repo>:ref:refs/heads/develop"

az identity federated-credential create `
  --name "gh-oidc-np" `
  --identity-name $IDENTITY `
  --resource-group $RG `
  --issuer "https://token.actions.githubusercontent.com" `
  --subject $SUBJECT `
  --audiences "api://AzureADTokenExchange"
```

For prod, use identity `id-bff-deploy-prod-eus` and subject
`repo:<github-org>/<repo>:ref:refs/heads/master`. If deployments run from a GitHub
*Environment* (recommended for prod, so required reviewers gate it), use
`repo:<org>/<repo>:environment:production` as the subject instead.

### 4.4 Container Registry (shared)

```powershell
az acr create -g rg-bff-shared-eus -n acrbffeus --sku Standard --location $LOCATION

# non-prod deploy identity: push + pull
$ACR_ID = az acr show -n acrbffeus --query id -o tsv
$DEPLOY_NP_PRINCIPAL = az identity show -g rg-bff-np-eus -n id-bff-deploy-np-eus --query principalId -o tsv
az role assignment create --assignee $DEPLOY_NP_PRINCIPAL --role AcrPush --scope $ACR_ID

# prod deploy identity: pull only (prod promotes an image already pushed by non-prod's pipeline)
$DEPLOY_PROD_PRINCIPAL = az identity show -g rg-bff-prod-eus -n id-bff-deploy-prod-eus --query principalId -o tsv
az role assignment create --assignee $DEPLOY_PROD_PRINCIPAL --role AcrPull --scope $ACR_ID

# both runtime identities: pull only (Container Apps pulls the image at revision creation)
foreach ($ENV in @("np","prod")) {
  $P = az identity show -g "rg-bff-$ENV-eus" -n "id-bff-runtime-$ENV-eus" --query principalId -o tsv
  az role assignment create --assignee $P --role AcrPull --scope $ACR_ID
}
```

### 4.5 Log Analytics + Application Insights

```powershell
foreach ($ENV in @("np","prod")) {
  $RG = "rg-bff-$ENV-eus"
  az monitor log-analytics workspace create -g $RG -n "law-bff-$ENV-eus" -l $LOCATION
  az monitor app-insights component create -g $RG -a "appi-bff-$ENV-eus" -l $LOCATION `
    --workspace "law-bff-$ENV-eus" --application-type web
}
```

### 4.6 Key Vault + RBAC

```powershell
foreach ($ENV in @("np","prod")) {
  $RG = "rg-bff-$ENV-eus"
  az keyvault create -g $RG -n "kv-bff-$ENV-eus" -l $LOCATION --enable-rbac-authorization true

  $KV_ID = az keyvault show -g $RG -n "kv-bff-$ENV-eus" --query id -o tsv
  $RUNTIME_PRINCIPAL = az identity show -g $RG -n "id-bff-runtime-$ENV-eus" --query principalId -o tsv
  az role assignment create --assignee $RUNTIME_PRINCIPAL --role "Key Vault Secrets User" --scope $KV_ID
}
```

Populate the OAuth secrets (values come from whoever owns the OAuth provider registration
— never hardcode them in a script that gets committed):

```powershell
az keyvault secret set --vault-name kv-bff-np-eus --name oauth-client-id --value "<client-id>"
az keyvault secret set --vault-name kv-bff-np-eus --name oauth-client-secret --value "<client-secret>"
```

### 4.7 Azure Managed Redis

Uses the `redisenterprise` extension (auto-installs on first use) rather than the classic
`az redis create` — see §1's design decision on why. `--clustering-policy NoCluster` keeps
the wire protocol compatible with the app's plain `redis-py` client (no cluster-aware
client needed for a single key per request, which is all this app does).

```powershell
foreach ($ENV in @("np","prod")) {
  $RG = "rg-bff-$ENV-eus"

  # 1. Cluster (this alone does NOT reliably provision the default database — create it explicitly next)
  az redisenterprise create --cluster-name "redis-bff-$ENV-eus" -g $RG -l $LOCATION `
    --sku Balanced_B0 --minimum-tls-version "1.2" --public-network-access Enabled

  # 2. Database — note: this CLI extension takes no --database-name flag; there is exactly
  #    one database per cluster and it's implicitly named "default"
  az redisenterprise database create -g $RG --cluster-name "redis-bff-$ENV-eus" `
    --clustering-policy NoCluster --client-protocol Encrypted --eviction-policy VolatileLRU

  # 3. Access keys default to Disabled as of this CLI version — enable explicitly, since the
  #    app authenticates with a key (§1 design decision), not Entra ID
  az redisenterprise database update -g $RG --cluster-name "redis-bff-$ENV-eus" `
    --access-keys-authentication Enabled

  $HOST = az redisenterprise show -g $RG --cluster-name "redis-bff-$ENV-eus" --query hostName -o tsv
  $PORT = az redisenterprise database show -g $RG --cluster-name "redis-bff-$ENV-eus" --query port -o tsv
  $KEY = az redisenterprise database list-keys -g $RG --cluster-name "redis-bff-$ENV-eus" --query primaryKey -o tsv

  az keyvault secret set --vault-name "kv-bff-$ENV-eus" --name redis-access-key --value $KEY
  Write-Output "redis-bff-$ENV-eus -> $HOST`:$PORT"
}
```

`Balanced_B0` is the smallest Managed Redis SKU — a reasonable POC size, still undersized
for prod concurrency. Revisit tier/size once real load numbers exist (PRD §21 sizing note).
Unlike classic Azure Cache for Redis, the port is dynamically assigned per database (not a
fixed 6380) — capture it from `az redisenterprise database show` rather than hardcoding it,
and set `REDIS_PORT` on the Container App accordingly (§4.9). The app also needs the key
itself as `REDIS_PASSWORD` (via `secretref:redis-key`, §4.9) — Managed Redis requires
authentication on connect, unlike a bare local Redis container.

### 4.8 Container Apps Environment

```powershell
foreach ($ENV in @("np","prod")) {
  $RG = "rg-bff-$ENV-eus"
  $LAW_ID = az monitor log-analytics workspace show -g $RG -n "law-bff-$ENV-eus" --query customerId -o tsv
  $LAW_KEY = az monitor log-analytics workspace get-shared-keys -g $RG -n "law-bff-$ENV-eus" --query primarySharedKey -o tsv

  az containerapp env create -g $RG -n "cae-bff-$ENV-eus" -l $LOCATION `
    --logs-workspace-id $LAW_ID --logs-workspace-key $LAW_KEY
}
```

### 4.9 Container App

```powershell
$ENV = "np"; $RG = "rg-bff-np-eus"
$RUNTIME_ID = az identity show -g $RG -n "id-bff-runtime-$ENV-eus" --query id -o tsv
$RUNTIME_CLIENT_ID = az identity show -g $RG -n "id-bff-runtime-$ENV-eus" --query clientId -o tsv
$APPI_CONN = az monitor app-insights component show -g $RG -a "appi-bff-$ENV-eus" --query connectionString -o tsv
$REDIS_KEY = az keyvault secret show --vault-name "kv-bff-$ENV-eus" --name redis-access-key --query value -o tsv
$REDIS_HOST = az redisenterprise show -g $RG --cluster-name "redis-bff-$ENV-eus" --query hostName -o tsv
$REDIS_PORT = az redisenterprise database show -g $RG --cluster-name "redis-bff-$ENV-eus" --query port -o tsv

az containerapp create -g $RG -n "ca-bff-$ENV-eus" `
  --environment "cae-bff-$ENV-eus" `
  --image "acrbffeus.azurecr.io/bff-auth:bootstrap" `
  --registry-server acrbffeus.azurecr.io `
  --registry-identity $RUNTIME_ID `
  --user-assigned $RUNTIME_ID `
  --target-port 8000 --ingress external `
  --min-replicas 1 --max-replicas 3 `
  --cpu 0.5 --memory 1Gi `
  --secrets "redis-key=$REDIS_KEY" `
  --env-vars `
    "ENVIRONMENT=dev" `
    "REDIS_HOST=$REDIS_HOST" `
    "REDIS_PORT=$REDIS_PORT" `
    "REDIS_SSL=true" `
    "REDIS_PASSWORD=secretref:redis-key" `
    "KEY_VAULT_URL=https://kv-bff-$ENV-eus.vault.azure.net/" `
    "AZURE_CLIENT_ID=$RUNTIME_CLIENT_ID" `
    "APPLICATIONINSIGHTS_CONNECTION_STRING=$APPI_CONN"
```

Note: `az redisenterprise database show`/`create` in the current CLI extension take no `--database-name`
flag — the database name is implicit (there's exactly one per cluster, named `default`).
Also note the Managed Redis database defaults to `accessKeysAuthentication: Disabled` as of
this CLI version — enable it explicitly right after creating the database:
`az redisenterprise database update -g $RG --cluster-name "redis-bff-$ENV-eus" --access-keys-authentication Enabled`.

Notes:
- `--image ...:bootstrap` is a placeholder — the real image tag lands via the GitHub Actions
  deploy job (`az containerapp update --image ...:<git-sha>`), never `latest` (PRD §11).
- `AZURE_CLIENT_ID` tells `DefaultAzureCredential` which user-assigned identity to use, since
  the app has more than one identity available at the environment level once both runtime
  and deploy identities exist in the same resource group.
- `REDIS_PORT` is pulled dynamically (Managed Redis assigns it per database, unlike classic
  Azure Cache for Redis's fixed 6380) and `REDIS_SSL=true` since the client protocol was
  created as `Encrypted` (§4.7) — the app's `config.py` already exposes both as settings, so
  no code change needed.
- Repeat for `prod`, pointed at `rg-bff-prod-eus` resources, and drop `bootstrap`/`dev`
  accordingly once the pipeline exists.

Domain binding (custom hostnames from PRD §7) is a separate, one-time step per environment
via `az containerapp hostname add` + `az containerapp hostname bind`, done once DNS/TLS
cert ownership is sorted — not blocking for POC (Container Apps' default
`*.azurecontainerapps.io` URL is enough to validate the checklist in PRD §31).

### 4.10 GitHub repo/environment secrets

Set these in the GitHub repo (Settings → Secrets and variables → Actions), split into a
`non-production` and `production` **Environment** so `main` deploys can require reviewer
approval (PRD §20):

| Secret | Non-Prod value | Prod value |
|---|---|---|
| `AZURE_CLIENT_ID` | `id-bff-deploy-np-eus` client ID | `id-bff-deploy-prod-eus` client ID |
| `AZURE_TENANT_ID` | tenant ID (same both envs) | same |
| `AZURE_SUBSCRIPTION_ID` | subscription ID | same |
| `ACR_NAME` | `acrbffeus` | `acrbffeus` |
| `RESOURCE_GROUP` | `rg-bff-np-eus` | `rg-bff-prod-eus` |
| `CONTAINER_APP_NAME` | `ca-bff-np-eus` | `ca-bff-prod-eus` |
| `CONTAINER_APP_URL` | its `*.azurecontainerapps.io` FQDN | its FQDN |

These map directly onto the secrets already referenced in
[.github/workflows/ci-cd.yml](.github/workflows/ci-cd.yml).

---

## 5. Validation checklist

Mirrors PRD §31, scoped to what's newly testable once the above exists:

- [ ] `az acr login` + manual `docker push` of a test tag succeeds using the deploy identity's
      federated OIDC token (i.e., run the GitHub Actions build job once by hand-triggering).
- [ ] Container App revision comes up healthy after `az containerapp update --image ...`.
- [ ] Runtime identity can read `oauth-client-id` / `oauth-client-secret` / `redis-access-key`
      from its own Key Vault only (confirm it gets `Forbidden` against the *other*
      environment's Key Vault — proves isolation).
- [ ] `/health/ready` returns 200 against the real `redis-bff-np-eus.redis.cache.windows.net`.
- [ ] `/token` returns a real access token end-to-end (Key Vault → OAuth provider → Redis
      cache write), and a second call is a cache hit (check Application Insights / logs for
      `Redis cache HIT`).
- [ ] No secret value appears in Log Analytics / App Insights logs.
- [ ] Non-prod's deploy identity **cannot** push to anything scoped to `rg-bff-prod-eus`
      (RBAC scoped correctly).

---

## 6. Explicitly out of scope for this POC (PRD §27–28)

Deferred, not forgotten — none of the naming or resource shape above needs to change when
these land:

- VNet integration + Private Endpoints for Key Vault/Redis/ACR.
- Azure Front Door or API Management in front of Container Apps.
- IP allow-listing / restricted ingress.
- Terraform/Bicep translation of section 4 (this doc *is* the spec to translate from).
- Production alerting (5xx rate, latency, Redis unavailability, restart spikes — PRD §25).
