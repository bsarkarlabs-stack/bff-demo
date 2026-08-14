# Azure Deployment Plan — BFF Auth Service

Every command in this document has actually been run against a real Azure subscription and
verified working, in this exact order, for the Non-Prod environment. Where the first
attempt failed, the failure and fix are documented inline rather than smoothed over —
that's what made this a reliable runbook instead of a guess.

This supersedes the original POC-shaped plan: Non-Prod is now built to the same
architectural spec as Production (private networking, zone redundancy), just sized down
for cost — see [ARCHITECTURE_CONCEPTS.md](ARCHITECTURE_CONCEPTS.md) §3.3 for why, and
[issue #1](https://github.com/bsarkarlabs-stack/bff-demo/issues/1) for the full target
architecture this executes. All commands are `bash`, run via `az cli` (WSL Ubuntu in this
session) — not PowerShell; every command here was actually executed this way.

Read [TESTING.md](TESTING.md) first — this plan assumes the image already builds, runs,
and passes health checks locally.

---

## 1. Design decisions (and why)

| Decision | Choice | Why |
|---|---|---|
| ACR: one shared registry vs. one per environment | **One shared ACR** (`acrbffeus`, interim — see §2 naming note) | Build once, promote the same artifact — a separate ACR per environment forces rebuilding for prod, which is exactly the drift this is trying to avoid. Access is segregated by RBAC instead: non-prod's identity gets `AcrPush`+`AcrPull`, prod's gets `AcrPull` only. |
| Runtime identity vs. deploy identity | **Two separate identities per environment** | The identity GitHub Actions federates into (push images / update the app) must never be the identity the *running container* uses (read Key Vault secrets) — collapsing them violates least-privilege. See ARCHITECTURE_CONCEPTS.md §5 for the near-miss that proved why this matters. |
| GitHub → Azure auth | **User-Assigned Managed Identity + federated credential (OIDC)** | No client secret to leak or rotate. See CICD_SETUP.md for the full OIDC subject-format gotchas. |
| Redis service | **Azure Managed Redis** (`az redisenterprise`, `Balanced_B0`), not classic Azure Cache for Redis | Microsoft's investment is in Managed Redis (Redis Enterprise engine) going forward; provisioning classic today just means a forced migration later. |
| Redis auth | Access key stored **in Key Vault** | Entra ID (passwordless) auth for Redis isn't uniformly simple across client SDKs yet; key-in-Key-Vault keeps the key out of source/env/image either way. |
| Networking | **Private VNet + Private Endpoints for Key Vault and Redis, in both Non-Prod and Prod** | Non-Prod's job is to prove Prod will work — if they have different network topology, a green Non-Prod deployment doesn't actually validate the thing most likely to break (the private DNS/network path). See ARCHITECTURE_CONCEPTS.md §3.3 and §11. |
| Non-Prod scaling | `minReplicas=0, maxReplicas=1` | Cost-optimized per explicit direction. Trade-off: with <2 replicas, the environment's zone-redundant *setting* doesn't translate into actual cross-zone resilience (that needs ≥2 replicas) — accepted for Non-Prod, revisit for Prod. |

---

## 2. Naming convention

Pattern: `<resource-type>-colorcon-bff-<environment>-<region>`. Project = `colorcon-bff`,
region = `eus` (East US).

**Resource group naming exception:** the Non-Prod resource group was created as
`rg-colourcon-bbf-np-eus` (a typo — "colourcon"/"bbf" instead of "colorcon"/"bff") and kept
as-is rather than recreated, since renaming a resource group isn't a supported Azure
operation without a full rebuild. **Every resource *inside* it uses the correct
`colorcon-bff` spelling** — the typo doesn't propagate. Don't copy the RG name's spelling
when naming anything else.

### Resource inventory (Non-Prod — built and verified)

| Resource | Name | Notes |
|---|---|---|
| Resource Group | `rg-colourcon-bbf-np-eus` | Typo preserved intentionally — see above |
| VNet | `vnet-colorcon-bff-np-eus` | `10.60.0.0/16` |
| Subnet (Container Apps) | `snet-colorcon-bff-aca-np-eus` | `10.60.0.0/23`, delegated to `Microsoft.App/environments` |
| Subnet (Private Endpoints) | `snet-colorcon-bff-pe-np-eus` | `10.60.2.0/24` |
| Container Registry | `acrbffeus` (in `rg-bff-shared-eus`) | **Interim** — old naming, reused until a `colorcon`-named shared RG/ACR exists (issue #2) |
| Container Apps Environment | `cae-colorcon-bff-np-eus` | Zone-redundant, VNet-integrated, `Consumption` workload profile |
| Container App | `ca-colorcon-bff-np-eus` | 3 health probes, `minReplicas=0/maxReplicas=1` |
| Azure Managed Redis | `redis-colorcon-bff-np-eus` | `Balanced_B0`, `NoCluster`, private-only |
| Key Vault | `kv-colorcon-bff-np-eus` | RBAC mode, private-only |
| Log Analytics Workspace | `law-colorcon-bff-np-eus` | 30-day retention (platform minimum — see §4.5) |
| Application Insights | `appi-colorcon-bff-np-eus` | |
| Managed Identity (runtime) | `id-colorcon-bff-runtime-np-eus` | |
| Managed Identity (GitHub OIDC deploy) | `id-colorcon-bff-deploy-np-eus` | |
| Private Endpoint (Key Vault) | `pe-kv-colorcon-bff-np-eus` | group-id `vault` |
| Private Endpoint (Redis) | `pe-redis-colorcon-bff-np-eus` | group-id `redisEnterprise` |
| Private DNS zone (Key Vault) | `privatelink.vaultcore.azure.net` | linked to the VNet |
| Private DNS zone (Redis) | `privatelink.redis.azure.net` | linked to the VNet |

Production (`rg-colorcon-bff-prod-eus`, correct spelling) is not built yet — separate future
work, same pattern.

### Tagging standard (per [issue #1](https://github.com/bsarkarlabs-stack/bff-demo/issues/1) §5)

```
client=colorcon
application=bff
environment=np|prod|shared
owner=<team-or-email>
managedBy=manual
```

**Known gap:** tags were not applied during the build documented here — this needs a
follow-up pass (tracked in issue #3's validation checklist) before calling the environment
enterprise-ready.

---

## 3. Build order

```
1. Resource Group (already exists: rg-colourcon-bbf-np-eus)
2. VNet + 2 subnets (Container Apps subnet delegated before use)
3. Log Analytics + Application Insights
4. Managed Identities (runtime + deploy)
5. Key Vault (public first) + secrets + RBAC
6. Key Vault private endpoint + private DNS zone + VNet link
7. Azure Managed Redis (public first) + database + access keys
8. Redis private endpoint + private DNS zone + VNet link
9. Container Apps Environment (zone-redundant, VNet-integrated)
10. RBAC on the (interim, shared) ACR for both identities
11. Container App — created with plain flags first, then probes patched in via YAML
12. GitHub OIDC federation + Container Apps Contributor for the deploy identity
13. GitHub Environment + secrets
14. Validate the private paths actually work end-to-end
15. Only then: disable public network access on Key Vault and Redis, re-validate
```

Steps 5/6 and 7/8 are interleaved above for narrative clarity, but in practice ran
concurrently (independent resources) to save wall-clock time — nothing here has a hard
ordering dependency until step 9 needs the VNet subnet, and step 11 needs everything before
it.

---

## 4. Step-by-step (verified `az cli` / `bash`)

### 4.0 Prerequisites

```bash
az login
az account set --subscription "<subscription-id-or-name>"
RG=rg-colourcon-bbf-np-eus
LOCATION=eastus
```

### 4.1 VNet + subnets

```bash
az network vnet create -g "$RG" -n vnet-colorcon-bff-np-eus -l "$LOCATION" \
  --address-prefix 10.60.0.0/16 \
  --subnet-name snet-colorcon-bff-aca-np-eus \
  --subnet-prefix 10.60.0.0/23

az network vnet subnet create -g "$RG" --vnet-name vnet-colorcon-bff-np-eus \
  -n snet-colorcon-bff-pe-np-eus \
  --address-prefix 10.60.2.0/24
```

**The Container Apps subnet must be delegated before the environment can use it** — this
was missed on the first attempt and failed with
`ManagedEnvironmentSubnetDelegationError: The subnet of the environment must be delegated
to the service 'Microsoft.App/environments'`:

```bash
az network vnet subnet update -g "$RG" --vnet-name vnet-colorcon-bff-np-eus \
  -n snet-colorcon-bff-aca-np-eus \
  --delegations Microsoft.App/environments
```

### 4.2 Log Analytics + Application Insights

```bash
az monitor log-analytics workspace create -g "$RG" -n law-colorcon-bff-np-eus -l "$LOCATION"

az monitor app-insights component create -g "$RG" -a appi-colorcon-bff-np-eus -l "$LOCATION" \
  --workspace law-colorcon-bff-np-eus --application-type web
```

**Retention defaults to 30 days and that's the platform floor for the standard `PerGB2018`
SKU** — attempting `--retention-time 14` (the originally-planned Non-Prod value) fails with
`InvalidParameter: 'RetentionInDays' property doesn't match the SKU limits`. 14 days is only
reachable on the legacy Free SKU (500MB/day ingestion cap — not viable for a real
workload). 30 days is the accepted value; the original "14 days for Non-Prod" target in
issue #1 §20 was written without knowing about this platform limit.

### 4.3 Managed identities

```bash
az identity create -g "$RG" -n id-colorcon-bff-runtime-np-eus -l "$LOCATION"
az identity create -g "$RG" -n id-colorcon-bff-deploy-np-eus -l "$LOCATION"
```

### 4.4 Key Vault (public first) + RBAC + secrets

```bash
az keyvault create -g "$RG" -n kv-colorcon-bff-np-eus -l "$LOCATION" --enable-rbac-authorization true

KV_ID=$(az keyvault show -g "$RG" -n kv-colorcon-bff-np-eus --query id -o tsv)
RUNTIME_PRINCIPAL=$(az identity show -g "$RG" -n id-colorcon-bff-runtime-np-eus --query principalId -o tsv)
az role assignment create --assignee-object-id "$RUNTIME_PRINCIPAL" --assignee-principal-type ServicePrincipal \
  --role "Key Vault Secrets User" --scope "$KV_ID"

# your own account needs write access too, to set secrets below
SELF_ID=$(az ad signed-in-user show --query id -o tsv)
az role assignment create --assignee-object-id "$SELF_ID" --assignee-principal-type User \
  --role "Key Vault Secrets Officer" --scope "$KV_ID"
```

RBAC on Key Vault's data plane can lag by ~20s after the role assignment — wait briefly
before setting secrets or you'll see a transient `Forbidden`:

```bash
sleep 20
az keyvault secret set --vault-name kv-colorcon-bff-np-eus --name oauth-client-id --value "<client-id>"
az keyvault secret set --vault-name kv-colorcon-bff-np-eus --name oauth-client-secret --value "<client-secret>"
```

### 4.5 Key Vault private endpoint + private DNS

```bash
az network private-dns zone create -g "$RG" -n privatelink.vaultcore.azure.net

az network private-dns link vnet create -g "$RG" \
  -n dnslink-kv-colorcon-bff-np-eus \
  -z privatelink.vaultcore.azure.net \
  -v vnet-colorcon-bff-np-eus \
  --registration-enabled false

KV_ID=$(az keyvault show -g "$RG" -n kv-colorcon-bff-np-eus --query id -o tsv)

az network private-endpoint create -g "$RG" \
  -n pe-kv-colorcon-bff-np-eus \
  --vnet-name vnet-colorcon-bff-np-eus \
  --subnet snet-colorcon-bff-pe-np-eus \
  --private-connection-resource-id "$KV_ID" \
  --group-id vault \
  --connection-name pe-conn-kv-colorcon-bff-np-eus

az network private-endpoint dns-zone-group create -g "$RG" \
  --endpoint-name pe-kv-colorcon-bff-np-eus \
  -n default \
  --private-dns-zone privatelink.vaultcore.azure.net \
  --zone-name vaultcore
```

Don't disable public access yet — that happens in §4.11, only after the Container App
exists and the private path is validated end-to-end.

### 4.6 Azure Managed Redis (public first)

```bash
az redisenterprise create --cluster-name redis-colorcon-bff-np-eus -g "$RG" -l "$LOCATION" \
  --sku Balanced_B0 --minimum-tls-version "1.2" --public-network-access Enabled
```

**Whether this alone provisions a usable default database is inconsistent** — in one run it
silently created nothing (`az redisenterprise database list` came back empty); in another
run it auto-created one with the wrong clustering policy (`OSSCluster` instead of
`NoCluster`). Either way, don't trust the auto-created database — check explicitly and fix
it:

```bash
az redisenterprise database list -g "$RG" --cluster-name redis-colorcon-bff-np-eus -o json
```

If empty, create it explicitly. **Note: this CLI extension takes no `--database-name`
flag** — there's exactly one database per cluster, implicitly named `default`:

```bash
az redisenterprise database create -g "$RG" --cluster-name redis-colorcon-bff-np-eus \
  --clustering-policy NoCluster --client-protocol Encrypted --eviction-policy VolatileLRU
```

If one already exists with the wrong clustering policy, **it can't be changed in place** —
`az redisenterprise database create` on an existing database returns
`BadRequest: 'properties.clusteringPolicy' cannot be changed... You must create a new
database`. Delete and recreate:

```bash
az redisenterprise database delete -g "$RG" --cluster-name redis-colorcon-bff-np-eus --yes
az redisenterprise database create -g "$RG" --cluster-name redis-colorcon-bff-np-eus \
  --clustering-policy NoCluster --client-protocol Encrypted --eviction-policy VolatileLRU
```

**Access-key authentication defaults to `Disabled`** as of this CLI version (ahead of a
scheduled breaking-change release) — enable it explicitly, since the app authenticates with
a key via Key Vault, not Entra ID:

```bash
az redisenterprise database update -g "$RG" --cluster-name redis-colorcon-bff-np-eus \
  --access-keys-authentication Enabled
```

### 4.7 Redis private endpoint + private DNS

The generic `az network private-link-resource list --type` command's client-side type
validation doesn't include `Microsoft.Cache/redisEnterprise` in this CLI version — that's
just an outdated allowlist in the command itself, not a sign private endpoints aren't
supported. Skip that check and create the endpoint directly with the documented group ID
(`redisEnterprise`), which works:

```bash
REDIS_ID=$(az redisenterprise show -g "$RG" --cluster-name redis-colorcon-bff-np-eus --query id -o tsv)

az network private-dns zone create -g "$RG" -n privatelink.redis.azure.net

az network private-dns link vnet create -g "$RG" \
  -n dnslink-redis-colorcon-bff-np-eus \
  -z privatelink.redis.azure.net \
  -v vnet-colorcon-bff-np-eus \
  --registration-enabled false

az network private-endpoint create -g "$RG" \
  -n pe-redis-colorcon-bff-np-eus \
  --vnet-name vnet-colorcon-bff-np-eus \
  --subnet snet-colorcon-bff-pe-np-eus \
  --private-connection-resource-id "$REDIS_ID" \
  --group-id redisEnterprise \
  --connection-name pe-conn-redis-colorcon-bff-np-eus

az network private-endpoint dns-zone-group create -g "$RG" \
  --endpoint-name pe-redis-colorcon-bff-np-eus \
  -n default \
  --private-dns-zone privatelink.redis.azure.net \
  --zone-name redis
```

To *prove* the private path is wired correctly before relying on it, check the DNS record's
actual IP is inside the private endpoint subnet's range (`10.60.2.0/24`), rather than just
assuming the zone-group creation worked:

```bash
az network private-dns record-set a show -g "$RG" -z privatelink.vaultcore.azure.net -n kv-colorcon-bff-np-eus --query aRecords -o json
az network private-dns record-set a show -g "$RG" -z privatelink.redis.azure.net -n redis-colorcon-bff-np-eus.eastus --query aRecords -o json
# both returned 10.60.2.x addresses, confirming correct wiring
```

### 4.8 Container Apps Environment (zone-redundant, VNet-integrated)

```bash
SUBNET_ID=$(az network vnet subnet show -g "$RG" --vnet-name vnet-colorcon-bff-np-eus -n snet-colorcon-bff-aca-np-eus --query id -o tsv)
LAW_ID=$(az monitor log-analytics workspace show -g "$RG" -n law-colorcon-bff-np-eus --query customerId -o tsv)
LAW_KEY=$(az monitor log-analytics workspace get-shared-keys -g "$RG" -n law-colorcon-bff-np-eus --query primarySharedKey -o tsv)

az containerapp env create -g "$RG" -n cae-colorcon-bff-np-eus -l "$LOCATION" \
  --logs-workspace-id "$LAW_ID" --logs-workspace-key "$LAW_KEY" \
  --infrastructure-subnet-resource-id "$SUBNET_ID" \
  --zone-redundant
```

`--zone-redundant` requires `--infrastructure-subnet-resource-id` — omitting it errors
immediately. Zone redundancy is fully supported on the default `Consumption` workload
profile (confirmed against Microsoft's reliability docs — no need to switch to a Dedicated
workload profile), provided the infrastructure subnet is `/23` or larger, which
`snet-colorcon-bff-aca-np-eus` already is.

### 4.9 RBAC on the shared ACR (interim `acrbffeus`)

```bash
ACR_ID=$(az acr show -n acrbffeus --query id -o tsv)
RUNTIME_PRINCIPAL=$(az identity show -g "$RG" -n id-colorcon-bff-runtime-np-eus --query principalId -o tsv)
DEPLOY_PRINCIPAL=$(az identity show -g "$RG" -n id-colorcon-bff-deploy-np-eus --query principalId -o tsv)

az role assignment create --assignee-object-id "$RUNTIME_PRINCIPAL" --assignee-principal-type ServicePrincipal \
  --role AcrPull --scope "$ACR_ID"
az role assignment create --assignee-object-id "$DEPLOY_PRINCIPAL" --assignee-principal-type ServicePrincipal \
  --role AcrPush --scope "$ACR_ID"
```

### 4.10 Container App — create first, patch in probes

`az containerapp create` has no flags for custom health probes — they only exist in the
YAML schema. Rather than hand-write that YAML from memory (the first attempt failed with an
opaque `The JSON value could not be converted to System.Boolean` schema error), create the
app first with plain flags — proven to work — then export its *real* YAML as ground truth
and patch probes into that.

```bash
RUNTIME_ID=$(az identity show -g "$RG" -n id-colorcon-bff-runtime-np-eus --query id -o tsv)
RUNTIME_CLIENT_ID=$(az identity show -g "$RG" -n id-colorcon-bff-runtime-np-eus --query clientId -o tsv)
REDIS_HOST=$(az redisenterprise show -g "$RG" --cluster-name redis-colorcon-bff-np-eus --query hostName -o tsv)
REDIS_PORT=$(az redisenterprise database show -g "$RG" --cluster-name redis-colorcon-bff-np-eus --query port -o tsv)
REDIS_KEY=$(az redisenterprise database list-keys -g "$RG" --cluster-name redis-colorcon-bff-np-eus --query primaryKey -o tsv)
APPI_CONN=$(az monitor app-insights component show -g "$RG" -a appi-colorcon-bff-np-eus --query connectionString -o tsv)

az containerapp create -g "$RG" -n ca-colorcon-bff-np-eus \
  --environment cae-colorcon-bff-np-eus \
  --image acrbffeus.azurecr.io/bff-auth:<git-sha> \
  --registry-server acrbffeus.azurecr.io \
  --registry-identity "$RUNTIME_ID" \
  --user-assigned "$RUNTIME_ID" \
  --target-port 8000 --ingress external \
  --min-replicas 0 --max-replicas 1 \
  --cpu 0.5 --memory 1Gi \
  --secrets "redis-key=$REDIS_KEY" \
  --env-vars \
    "ENVIRONMENT=non-production" \
    "REDIS_HOST=$REDIS_HOST" \
    "REDIS_PORT=$REDIS_PORT" \
    "REDIS_SSL=true" \
    "REDIS_PASSWORD=secretref:redis-key" \
    "KEY_VAULT_URL=https://kv-colorcon-bff-np-eus.vault.azure.net/" \
    "AZURE_CLIENT_ID=$RUNTIME_CLIENT_ID" \
    "APPLICATIONINSIGHTS_CONNECTION_STRING=$APPI_CONN"
```

`minReplicas=0/maxReplicas=1` was an explicit cost call for Non-Prod — note the zone
redundancy caveat in §1. `ENVIRONMENT=non-production` (not the original PRD's `dev`)
reflects the shared 7-branch Non-Prod platform this environment actually is — see
ARCHITECTURE_CONCEPTS.md §12 for the fuller reasoning, and the still-open question there
about eventually moving to a `PLATFORM_ENVIRONMENT` + hostname-routing scheme.

Now export the real YAML and patch in the three probes (Startup only checks the app is up;
Liveness deliberately checks nothing external — see ARCHITECTURE_CONCEPTS.md §13 for why
mixing in Redis/Key Vault checks there would turn a transient dependency blip into an
unnecessary container restart):

```bash
az containerapp show -g "$RG" -n ca-colorcon-bff-np-eus -o yaml > /tmp/ca-update.yaml
# Edit /tmp/ca-update.yaml in place: add a `probes:` list under
# properties.template.containers[0], alongside the existing fields (env, image, name,
# resources). Trim read-only/computed top-level fields first — id, systemData,
# provisioningState, outboundIpAddresses, eventStreamEndpoint, customDomainVerificationId,
# latestRevisionName/Fqdn — the update call can reject an unedited full export.
```

The probes block that worked:

```yaml
probes:
- type: Startup
  httpGet:
    path: /health/startup
    port: 8000
  initialDelaySeconds: 3
  periodSeconds: 5
  failureThreshold: 10
- type: Liveness
  httpGet:
    path: /health/live
    port: 8000
  initialDelaySeconds: 5
  periodSeconds: 10
  failureThreshold: 3
- type: Readiness
  httpGet:
    path: /health/ready
    port: 8000
  initialDelaySeconds: 5
  periodSeconds: 10
  failureThreshold: 3
```

Apply it and verify the probes actually landed — don't trust a clean exit code alone:

```bash
az containerapp update -g "$RG" -n ca-colorcon-bff-np-eus --yaml /tmp/ca-update.yaml

az containerapp show -g "$RG" -n ca-colorcon-bff-np-eus \
  --query "properties.template.containers[0].probes" -o json
```

`/health/startup` is a new endpoint (`app/main.py`) added specifically for this — it didn't
exist before this build, since the original 2-probe (`/health`, `/health/live`,
`/health/ready`) design had no dedicated startup check.

### 4.11 GitHub OIDC federation + Container Apps RBAC for the deploy identity

Same subject-format rules as CICD_SETUP.md §3 — fetch the real prefix, don't guess it:

```bash
SUB_PREFIX=$(gh api repos/bsarkarlabs-stack/bff-demo/actions/oidc/customization/sub --jq ".sub_claim_prefix")

az identity federated-credential create \
  --name gh-oidc-np-environment \
  --identity-name id-colorcon-bff-deploy-np-eus \
  --resource-group "$RG" \
  --issuer https://token.actions.githubusercontent.com \
  --subject "${SUB_PREFIX}:environment:non-production" \
  --audiences api://AzureADTokenExchange
```

The deploy identity also needs `Container Apps Contributor` scoped to the app itself —
without this, the pipeline's `az containerapp update` fails with a 404-shaped "does not
exist" error that masks the real RBAC cause (see CICD_SETUP.md §4.2 for the full
explanation):

```bash
CA_ID=$(az containerapp show -g "$RG" -n ca-colorcon-bff-np-eus --query id -o tsv)
DEPLOY_PRINCIPAL=$(az identity show -g "$RG" -n id-colorcon-bff-deploy-np-eus --query principalId -o tsv)

az role assignment create --assignee-object-id "$DEPLOY_PRINCIPAL" --assignee-principal-type ServicePrincipal \
  --role "Container Apps Contributor" --scope "$CA_ID"
```

### 4.12 GitHub secrets

Update the existing `non-production` GitHub Environment's secrets to point at the new
resources. **Use `printf`, not `echo`** — see CICD_SETUP.md §5 for why `echo | gh secret
set` silently corrupts the value with a trailing newline:

```bash
REPO=bsarkarlabs-stack/bff-demo

CLIENT_ID=$(az identity show -g "$RG" -n id-colorcon-bff-deploy-np-eus --query clientId -o tsv)
TENANT_ID=$(az account show --query tenantId -o tsv)
SUB_ID=$(az account show --query id -o tsv)

printf '%s' "$CLIENT_ID" | gh secret set AZURE_CLIENT_ID --env non-production --repo "$REPO"
printf '%s' "$TENANT_ID" | gh secret set AZURE_TENANT_ID --env non-production --repo "$REPO"
printf '%s' "$SUB_ID" | gh secret set AZURE_SUBSCRIPTION_ID --env non-production --repo "$REPO"
printf '%s' "acrbffeus" | gh secret set ACR_NAME --env non-production --repo "$REPO"
printf '%s' "$RG" | gh secret set RESOURCE_GROUP --env non-production --repo "$REPO"
printf '%s' "ca-colorcon-bff-np-eus" | gh secret set CONTAINER_APP_NAME --env non-production --repo "$REPO"
printf '%s' "https://<container-app-fqdn>" | gh secret set CONTAINER_APP_URL --env non-production --repo "$REPO"
```

### 4.13 Validate the private paths, then lock down public access

**Don't disable public access first and hope** — validate the private path actually works
while public access is still available as a fallback, per the hardening order in
ARCHITECTURE_CONCEPTS.md §9. Two ways to validate, both used here:

1. Confirm the private DNS A records resolve to addresses inside the private endpoint
   subnet (done in §4.7 above — `10.60.2.4` for Key Vault, `10.60.2.5` for Redis).
2. The definitive test: disable public access, then confirm the app still works.

```bash
az keyvault update -g "$RG" -n kv-colorcon-bff-np-eus --public-network-access Disabled
```

```bash
curl https://<container-app-fqdn>/token
# 502 "Failed to obtain token" is EXPECTED here (placeholder OAuth credentials) — what
# matters is that the failure trace (az containerapp logs show) proves it got past the
# Key Vault read via httpx.UnsupportedProtocol on the empty OAUTH_TOKEN_URL, not a Key
# Vault connection/auth error. That's proof the private path works.
```

```bash
az redisenterprise update -g "$RG" --cluster-name redis-colorcon-bff-np-eus --public-network-access Disabled
curl https://<container-app-fqdn>/health/ready
# 200 {"status":"ready"} confirms Redis over the private endpoint only
```

Note: control-plane operations (`az keyvault update`, `az redisenterprise update`) go
through ARM and are **not** blocked by the resource's own network rules — disabling public
data-plane access doesn't lock you out of managing the resource via `az cli`, only out of
reading/writing secrets or cache data from outside the VNet.

### 4.14 Housekeeping: orphaned RBAC after a resource group deletion

If a resource group holding identities that had RBAC grants elsewhere gets deleted (as
happened here — an earlier `rg-bff-np-eus` was deleted mid-project), the role assignments
on resources *outside* that group (the shared ACR) don't get cleaned up automatically —
they become orphaned, showing a blank `principalName` since the principal no longer
resolves:

```bash
ACR_ID=$(az acr show -n acrbffeus --query id -o tsv)
az role assignment list --scope "$ACR_ID" -o json
# entries with empty principalName are orphaned; cross-check principalId against
# currently-existing identities before deleting anything
az role assignment delete --ids "<orphaned-assignment-id>" "<another-orphaned-id>"
```

---

## 5. Validation checklist

See [issue #3](https://github.com/bsarkarlabs-stack/bff-demo/issues/3) for the full,
currently-in-progress checklist — cross-environment isolation, pipeline end-to-end trigger,
tag audit, and zone-redundancy sanity checks are still open.

Already confirmed in this build:

- [x] `/health`, `/health/startup`, `/health/live`, `/health/ready` all return 200
- [x] All 3 probes verified present on the live resource (not just assumed from a clean
      `update` exit code)
- [x] `/token` reaches Key Vault successfully via the runtime managed identity, over the
      private endpoint only (public access disabled), failing only at the placeholder
      OAuth call
- [x] `/health/ready` returns 200 against Redis over the private endpoint only (public
      access disabled)
- [x] No secrets appear in any log line across every failure trace inspected

---

## 6. Explicitly deferred

- Shared `colorcon`-named RG/ACR (`rg-colorcon-bff-shared-eus` / `acrcolorconbffeus`) — the
  interim `acrbffeus` is still in use; migrate once the new shared RG exists.
- Production environment — same pattern, not started.
- Custom domain / DNS / TLS, production alerting, production HA sizing, full CI/CD policy
  review — all tracked as deferred-with-a-reason in
  [issue #1](https://github.com/bsarkarlabs-stack/bff-demo/issues/1), not overlooked.
- Tag application (`client`, `application`, `environment`, `owner`, `managedBy`) across all
  resources built in this session — tracked in
  [issue #3](https://github.com/bsarkarlabs-stack/bff-demo/issues/3).
