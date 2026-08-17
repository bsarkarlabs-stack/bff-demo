# Azure Deployment Plan — BFF Auth Service

Every command in this document has actually been run against a real Azure subscription and
verified working, for the Non-Prod environment. Section 4 is a clean execution runbook —
only the commands that actually worked, one step at a time, each with its own purpose,
verification, and go/no-go decision. Every failed attempt that happened along the way is
preserved in Section 6, not hidden — that's where the "why" behind some of Section 4's
steps comes from.

This supersedes the original POC-shaped plan: Non-Prod is now built to the same
architectural spec as Production (private networking, zone redundancy), just sized down
for cost — see [ARCHITECTURE_CONCEPTS.md](ARCHITECTURE_CONCEPTS.md) §3.3 for why, and
[issue #1](https://github.com/bsarkarlabs-stack/bff-demo/issues/1) for the full target
architecture this executes. All commands are `bash`, run via `az cli` (WSL Ubuntu in this
session) — not PowerShell.

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
| Log Analytics Workspace | `law-colorcon-bff-np-eus` | 30-day retention (platform minimum — see Step 5) |
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

**Known gap:** tags were not applied during the build documented here — see
[issue #3](https://github.com/bsarkarlabs-stack/bff-demo/issues/3), listed under §7
Optional/Deferred below.

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

---

## 4. Execution runbook

Each step is: **Purpose** (why it exists) → **Run** (the exact command) → **Verify** (how
to confirm it actually worked, not just that the CLI returned exit 0) → **Expected
result** → **Next** (explicit go/no-go). Steps that already exist are marked so you don't
recreate them blindly.

```bash
# shared across every step below
RG=rg-colourcon-bbf-np-eus
LOCATION=eastus
```

---

### Step 1 — Prerequisites

**Purpose:** Confirm you're pointed at the right tenant/subscription before creating
anything — the most expensive mistake in this whole runbook is doing all of it in the
wrong subscription.

**Run:**
```bash
az login
az account set --subscription "<subscription-id-or-name>"
```

**Verify:**
```bash
az account show --query "{name:name, id:id, tenantId:tenantId}" -o table
```

**Expected result:** The subscription name/ID/tenant match what you intend to deploy into.

**Next:** Safe to continue only if the subscription shown is correct. If it's wrong, run
`az account set` again before proceeding — every later step inherits this context silently.

---

### Step 2 — Resource Group (verify only — already exists)

**Purpose:** `rg-colourcon-bbf-np-eus` already exists. Do not run `az group create` again —
it's a harmless no-op on an existing group, but there's no reason to re-run a creation
command for something already confirmed present.

**Run:** *(nothing to create)*

**Verify:**
```bash
az group show -n "$RG" --query "{name:name, location:location, state:properties.provisioningState}" -o table
```

**Expected result:** `provisioningState: Succeeded`, `location: eastus`.

**Next:** Safe to continue.

---

### Step 3 — Virtual Network and subnets

**Purpose:** Non-Prod is built to the same private-networking spec as Production (§1) —
this VNet and its two subnets (one for Container Apps, one for private endpoints) are the
foundation everything else attaches to.

**Run:**
```bash
az network vnet create -g "$RG" -n vnet-colorcon-bff-np-eus -l "$LOCATION" \
  --address-prefix 10.60.0.0/16 \
  --subnet-name snet-colorcon-bff-aca-np-eus \
  --subnet-prefix 10.60.0.0/23

az network vnet subnet create -g "$RG" --vnet-name vnet-colorcon-bff-np-eus \
  -n snet-colorcon-bff-pe-np-eus \
  --address-prefix 10.60.2.0/24
```

**Verify:**
```bash
az network vnet subnet list -g "$RG" --vnet-name vnet-colorcon-bff-np-eus \
  --query "[].{name:name, prefix:addressPrefix}" -o table
```

**Expected result:** Two subnets listed — `snet-colorcon-bff-aca-np-eus` (`10.60.0.0/23`)
and `snet-colorcon-bff-pe-np-eus` (`10.60.2.0/24`).

**Next:** Safe to continue. The Container Apps subnet is not usable yet — Step 4 is
mandatory before Step 14.

---

### Step 4 — Delegate the Container Apps subnet

**Purpose:** Azure Container Apps requires its infrastructure subnet to carry a specific
service delegation. This is not applied automatically at subnet creation — it must be set
explicitly, and Step 14 (Container Apps Environment) will fail without it (see Section 6,
issue 1).

**Run:**
```bash
az network vnet subnet update -g "$RG" --vnet-name vnet-colorcon-bff-np-eus \
  -n snet-colorcon-bff-aca-np-eus \
  --delegations Microsoft.App/environments
```

**Verify:**
```bash
az network vnet subnet show -g "$RG" --vnet-name vnet-colorcon-bff-np-eus \
  -n snet-colorcon-bff-aca-np-eus --query "delegations[].serviceName" -o tsv
```

**Expected result:** `Microsoft.App/environments`

**Next:** Safe to continue.

---

### Step 5 — Log Analytics Workspace + Application Insights

**Purpose:** Central logging/telemetry destination for the Container Apps Environment and
the application (Application Insights connection string is injected into the app).

**Run:**
```bash
az monitor log-analytics workspace create -g "$RG" -n law-colorcon-bff-np-eus -l "$LOCATION"

az monitor app-insights component create -g "$RG" -a appi-colorcon-bff-np-eus -l "$LOCATION" \
  --workspace law-colorcon-bff-np-eus --application-type web
```

**Verify:**
```bash
az monitor log-analytics workspace show -g "$RG" -n law-colorcon-bff-np-eus \
  --query "{state:provisioningState, retention:retentionInDays}" -o table
```

**Expected result:** `state: Succeeded`, `retention: 30`. **Not 14** — 30 days is the
platform-enforced floor for the standard `PerGB2018` SKU. Don't attempt to lower it (see
Section 6, issue 5); this is the correct, final value.

**Next:** Safe to continue.

---

### Step 6 — Managed identities

**Purpose:** Two identities, deliberately kept separate — one for the *running app*
(read-only access to its own Key Vault, pull-only on the ACR), one for *GitHub Actions*
(push to the ACR, update the Container App). See ARCHITECTURE_CONCEPTS.md §5 for why
merging these would violate least-privilege.

**Run:**
```bash
az identity create -g "$RG" -n id-colorcon-bff-runtime-np-eus -l "$LOCATION"
az identity create -g "$RG" -n id-colorcon-bff-deploy-np-eus -l "$LOCATION"
```

**Verify:**
```bash
az identity list -g "$RG" --query "[].{name:name, clientId:clientId}" -o table
```

**Expected result:** Both `id-colorcon-bff-runtime-np-eus` and
`id-colorcon-bff-deploy-np-eus` listed, each with a `clientId`.

**Next:** Safe to continue.

---

### Step 7 — Key Vault (create, public network access still enabled)

**Purpose:** Stores `oauth-client-id`, `oauth-client-secret`, and later `redis-access-key`.
Created with public access on for now — it gets locked to private-only in Step 21, only
after the private path is proven working (never lock down before validating; see Step 21's
Purpose).

**Run:**
```bash
az keyvault create -g "$RG" -n kv-colorcon-bff-np-eus -l "$LOCATION" --enable-rbac-authorization true
```

**Verify:**
```bash
az keyvault show -g "$RG" -n kv-colorcon-bff-np-eus \
  --query "{rbac:properties.enableRbacAuthorization, publicAccess:properties.publicNetworkAccess}" -o table
```

**Expected result:** `rbac: True`, `publicAccess: Enabled`.

**Next:** Safe to continue.

---

### Step 8 — Key Vault RBAC (runtime identity + your own account)

**Purpose:** The runtime identity needs read access to secrets at runtime. Your own account
needs write access to actually populate those secrets in Step 9 — RBAC mode grants nothing
by default, not even to the vault's creator.

**Run:**
```bash
KV_ID=$(az keyvault show -g "$RG" -n kv-colorcon-bff-np-eus --query id -o tsv)
RUNTIME_PRINCIPAL=$(az identity show -g "$RG" -n id-colorcon-bff-runtime-np-eus --query principalId -o tsv)
SELF_ID=$(az ad signed-in-user show --query id -o tsv)

az role assignment create --assignee-object-id "$RUNTIME_PRINCIPAL" --assignee-principal-type ServicePrincipal \
  --role "Key Vault Secrets User" --scope "$KV_ID"
az role assignment create --assignee-object-id "$SELF_ID" --assignee-principal-type User \
  --role "Key Vault Secrets Officer" --scope "$KV_ID"
```

**Verify:**
```bash
az role assignment list --scope "$KV_ID" --query "[].{principal:principalId, role:roleDefinitionName}" -o table
```

**Expected result:** Two rows — the runtime identity's principal ID with `Key Vault Secrets
User`, and your own account with `Key Vault Secrets Officer`.

**Next:** Wait ~20 seconds before Step 9 — Key Vault's data-plane RBAC can lag behind the
control-plane confirmation, and setting secrets immediately can return a transient
`Forbidden`.

---

### Step 9 — Key Vault secrets

**Purpose:** Populate the OAuth placeholder credentials the application reads at runtime.
(Real OAuth provider credentials, when available, replace these same secret names — no
code or infrastructure change needed.)

**Run:**
```bash
az keyvault secret set --vault-name kv-colorcon-bff-np-eus --name oauth-client-id --value "<client-id>"
az keyvault secret set --vault-name kv-colorcon-bff-np-eus --name oauth-client-secret --value "<client-secret>"
```

**Verify:**
```bash
az keyvault secret list --vault-name kv-colorcon-bff-np-eus --query "[].name" -o tsv
```

**Expected result:** `oauth-client-id` and `oauth-client-secret` both listed.

**Next:** Safe to continue.

---

### Step 10 — Key Vault private endpoint + private DNS

**Purpose:** Wires the private network path Step 21 will eventually make the *only* path.
Four resources, always created together, verified as one unit: the private DNS zone, its
link to the VNet, the private endpoint itself, and the DNS zone group that auto-registers
the private A record.

**Run:**
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

**Verify:**
```bash
az network private-dns record-set a show -g "$RG" -z privatelink.vaultcore.azure.net \
  -n kv-colorcon-bff-np-eus --query aRecords -o json
```

**Expected result:** An IP address inside `10.60.2.0/24` (the private endpoint subnet) —
in this build, `10.60.2.4`. If the returned address is *not* inside that range, something
is wired to the wrong subnet — stop and investigate before continuing.

**Next:** Safe to continue.

---

### Step 11 — Azure Managed Redis cluster + database

**Purpose:** Token cache for OAuth access tokens (ARCHITECTURE_CONCEPTS.md §10). This step
has a real platform inconsistency baked into it — read the Verify step carefully, it's not
optional.

**Run:**
```bash
az redisenterprise create --cluster-name redis-colorcon-bff-np-eus -g "$RG" -l "$LOCATION" \
  --sku Balanced_B0 --minimum-tls-version "1.2" --public-network-access Enabled
```

**Verify:**
```bash
az redisenterprise database list -g "$RG" --cluster-name redis-colorcon-bff-np-eus -o json
```

**Expected result — one of two outcomes, both require action:**

- **Empty list** → no database was auto-created. Create it explicitly:
  ```bash
  az redisenterprise database create -g "$RG" --cluster-name redis-colorcon-bff-np-eus \
    --clustering-policy NoCluster --client-protocol Encrypted --eviction-policy VolatileLRU
  ```
- **One database exists, but `clusteringPolicy` is `OSSCluster`** (not `NoCluster`) → a
  default database was auto-created with the wrong policy. Clustering policy cannot be
  changed on an existing database (see Section 6, issue 3) — delete and recreate:
  ```bash
  az redisenterprise database delete -g "$RG" --cluster-name redis-colorcon-bff-np-eus --yes
  az redisenterprise database create -g "$RG" --cluster-name redis-colorcon-bff-np-eus \
    --clustering-policy NoCluster --client-protocol Encrypted --eviction-policy VolatileLRU
  ```

Re-run the Verify command after either fix. Do not proceed until `clusteringPolicy:
NoCluster` is confirmed — the app's plain `redis-py` client is not cluster-aware.

**Next:** Safe to continue only once `clusteringPolicy: NoCluster` is confirmed.

---

### Step 12 — Enable Redis access-key authentication

**Purpose:** Access keys default to `Disabled` on newly created databases as of this CLI
version, ahead of a scheduled breaking-change release. The app authenticates with a key
(via Key Vault), not Entra ID, so this must be turned on explicitly.

**Run:**
```bash
az redisenterprise database update -g "$RG" --cluster-name redis-colorcon-bff-np-eus \
  --access-keys-authentication Enabled
```

**Verify:**
```bash
az redisenterprise database show -g "$RG" --cluster-name redis-colorcon-bff-np-eus \
  --query accessKeysAuthentication -o tsv
```

**Expected result:** `Enabled`

**Next:** Safe to continue.

---

### Step 13 — Redis private endpoint + private DNS

**Purpose:** Same pattern as Step 10, for Redis instead of Key Vault. The private-link
group ID (`redisEnterprise`) is not obvious/documented in the generic private-link-resource
list command (see Section 6, issue 6) — it's given directly here rather than derived.

**Run:**
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

**Verify:**
```bash
az network private-dns record-set a show -g "$RG" -z privatelink.redis.azure.net \
  -n redis-colorcon-bff-np-eus.eastus --query aRecords -o json
```

**Expected result:** An IP inside `10.60.2.0/24` — in this build, `10.60.2.5`.

**Next:** Safe to continue.

---

### Step 14 — Container Apps Environment (zone-redundant, VNet-integrated)

**Purpose:** The compute environment the Container App runs inside — this is where zone
redundancy and VNet integration are actually configured; the Container App itself just
inherits them.

**Run:**
```bash
SUBNET_ID=$(az network vnet subnet show -g "$RG" --vnet-name vnet-colorcon-bff-np-eus \
  -n snet-colorcon-bff-aca-np-eus --query id -o tsv)
LAW_ID=$(az monitor log-analytics workspace show -g "$RG" -n law-colorcon-bff-np-eus --query customerId -o tsv)
LAW_KEY=$(az monitor log-analytics workspace get-shared-keys -g "$RG" -n law-colorcon-bff-np-eus --query primarySharedKey -o tsv)

az containerapp env create -g "$RG" -n cae-colorcon-bff-np-eus -l "$LOCATION" \
  --logs-workspace-id "$LAW_ID" --logs-workspace-key "$LAW_KEY" \
  --infrastructure-subnet-resource-id "$SUBNET_ID" \
  --zone-redundant
```

*(Requires Step 4's subnet delegation to already be in place — if this fails with
`ManagedEnvironmentSubnetDelegationError`, go back and confirm Step 4 actually completed.)*

**Verify:**
```bash
az containerapp env show -g "$RG" -n cae-colorcon-bff-np-eus \
  --query "{state:properties.provisioningState, zoneRedundant:properties.zoneRedundant}" -o table
```

**Expected result:** `state: Succeeded`, `zoneRedundant: True`.

**Next:** Safe to continue.

---

### Step 15 — RBAC on the shared ACR (interim `acrbffeus`)

**Purpose:** Grants the runtime identity pull access (to run the app's image) and the
deploy identity push access (for the CI/CD pipeline). This ACR is interim — it lives in the
old `rg-bff-shared-eus`, reused until a `colorcon`-named shared registry exists (§7).

**Run:**
```bash
ACR_ID=$(az acr show -n acrbffeus --query id -o tsv)
RUNTIME_PRINCIPAL=$(az identity show -g "$RG" -n id-colorcon-bff-runtime-np-eus --query principalId -o tsv)
DEPLOY_PRINCIPAL=$(az identity show -g "$RG" -n id-colorcon-bff-deploy-np-eus --query principalId -o tsv)

az role assignment create --assignee-object-id "$RUNTIME_PRINCIPAL" --assignee-principal-type ServicePrincipal \
  --role AcrPull --scope "$ACR_ID"
az role assignment create --assignee-object-id "$DEPLOY_PRINCIPAL" --assignee-principal-type ServicePrincipal \
  --role AcrPush --scope "$ACR_ID"
```

**Verify:**
```bash
az role assignment list --scope "$ACR_ID" --query "[].{principal:principalId, role:roleDefinitionName}" -o table
```

**Expected result:** The runtime identity's principal ID with `AcrPull`, the deploy
identity's with `AcrPush`.

**Next:** Safe to continue.

---

### Step 16 — Container App: initial create (no probes yet)

**Purpose:** Get a working, reachable Container App up first with plain CLI flags — proven
reliable — before attempting the more fragile custom-probes YAML in Step 17. Trying to do
both at once (hand-written YAML with probes on first creation) is exactly what failed in
Section 6, issue 4.

**Run:**
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

**Verify:**
```bash
curl https://<container-app-fqdn>/health
```

**Expected result:** `{"status":"ok"}`, HTTP 200.

**Next:** Safe to continue.

---

### Step 17 — Add the three health probes

**Purpose:** `az containerapp create`/`update` has no flags for custom health probes —
they only exist in the YAML schema. Rather than hand-write that YAML from memory (see
Section 6, issue 4), export the app's *real* current configuration and edit that.

**Run:**
```bash
az containerapp show -g "$RG" -n ca-colorcon-bff-np-eus -o yaml > /tmp/ca-update.yaml
```

Edit `/tmp/ca-update.yaml`:
1. Trim read-only/computed top-level fields — `id`, `systemData`, `provisioningState`,
   `outboundIpAddresses`, `eventStreamEndpoint`, `customDomainVerificationId`,
   `latestRevisionName`/`latestRevisionFqdn`.
2. Under `properties.template.containers[0]`, add:

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

Liveness deliberately checks nothing external (ARCHITECTURE_CONCEPTS.md §13) — a transient
Redis blip should never trigger a container restart.

```bash
az containerapp update -g "$RG" -n ca-colorcon-bff-np-eus --yaml /tmp/ca-update.yaml
```

**Verify:**
```bash
az containerapp show -g "$RG" -n ca-colorcon-bff-np-eus \
  --query "properties.template.containers[0].probes" -o json
```

**Expected result:** All three probe types (`Startup`, `Liveness`, `Readiness`) present
with the correct paths. **Don't trust a clean `update` exit code alone** — the probes have
silently failed to apply before with no error; always re-query and look.

**Next:** Safe to continue.

---

### Step 18 — GitHub OIDC federation

**Purpose:** Lets GitHub Actions authenticate as the deploy identity with no stored Azure
secret. The subject string must be fetched, never guessed (see CICD_SETUP.md §3.1 for the
full reasoning — the format includes immutable numeric org/repo IDs on this GitHub
platform version).

**Run:**
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

**Verify:**
```bash
az identity federated-credential list --identity-name id-colorcon-bff-deploy-np-eus \
  -g "$RG" --query "[].subject" -o tsv
```

**Expected result:** A subject ending in `:environment:non-production`, prefixed with
`repo:bsarkarlabs-stack@<numeric-id>/bff-demo@<numeric-id>` — not a plain
`repo:bsarkarlabs-stack/bff-demo` and not a `:ref:refs/heads/...` suffix (see Section 6,
issue 2 for what happens if either is wrong).

**Next:** Safe to continue.

---

### Step 19 — Deploy identity RBAC on the Container App

**Purpose:** Without this, the pipeline's `az containerapp update` fails with a 404-shaped
"does not exist" error that looks like a naming bug but is actually a permissions gap (see
Section 6, issue 8). ACR roles (Step 15) are not sufficient on their own.

**Run:**
```bash
CA_ID=$(az containerapp show -g "$RG" -n ca-colorcon-bff-np-eus --query id -o tsv)
DEPLOY_PRINCIPAL=$(az identity show -g "$RG" -n id-colorcon-bff-deploy-np-eus --query principalId -o tsv)

az role assignment create --assignee-object-id "$DEPLOY_PRINCIPAL" --assignee-principal-type ServicePrincipal \
  --role "Container Apps Contributor" --scope "$CA_ID"
```

**Verify:**
```bash
az role assignment list --scope "$CA_ID" --query "[].roleDefinitionName" -o tsv
```

**Expected result:** `Container Apps Contributor` listed.

**Next:** Safe to continue. Allow ~30 seconds for RBAC propagation before triggering a
pipeline run.

---

### Step 20 — GitHub Environment + secrets/variables

**Purpose:** Environment-scoped (not repo-wide) config, so a `master`-branch deploy can
never see the `non-production` credentials or vice versa. Only the three Azure OIDC
identifiers are actually sensitive — `ACR_NAME`, `RESOURCE_GROUP`, `CONTAINER_APP_NAME`,
and `CONTAINER_APP_URL` are ordinary config and belong in GitHub Environment **variables**,
not secrets (storing non-sensitive values as secrets doesn't add security, it just masks
them in logs and makes debugging harder). See CICD_SETUP.md §5 for the full reasoning.

**Run:**
```bash
REPO=bsarkarlabs-stack/bff-demo

gh api --method PUT "repos/$REPO/environments/non-production"

printf '%s' "$(az identity show -g "$RG" -n id-colorcon-bff-deploy-np-eus --query clientId -o tsv)" \
  | gh secret set AZURE_CLIENT_ID --env non-production --repo "$REPO"
printf '%s' "$(az account show --query tenantId -o tsv)" \
  | gh secret set AZURE_TENANT_ID --env non-production --repo "$REPO"
printf '%s' "$(az account show --query id -o tsv)" \
  | gh secret set AZURE_SUBSCRIPTION_ID --env non-production --repo "$REPO"

gh variable set ACR_NAME --env non-production --repo "$REPO" --body "acrbffeus"
gh variable set RESOURCE_GROUP --env non-production --repo "$REPO" --body "$RG"
gh variable set CONTAINER_APP_NAME --env non-production --repo "$REPO" --body "ca-colorcon-bff-np-eus"
FQDN=$(az containerapp show -g "$RG" -n ca-colorcon-bff-np-eus --query properties.configuration.ingress.fqdn -o tsv)
gh variable set CONTAINER_APP_URL --env non-production --repo "$REPO" --body "https://${FQDN}"
```

**Verify:**
```bash
gh secret list --env non-production --repo "$REPO"
gh variable list --env non-production --repo "$REPO"
```

**Expected result:** 3 secrets (`AZURE_CLIENT_ID`, `AZURE_TENANT_ID`,
`AZURE_SUBSCRIPTION_ID`) and 4 variables (`ACR_NAME`, `RESOURCE_GROUP`,
`CONTAINER_APP_NAME`, `CONTAINER_APP_URL`), all with recent `Updated` timestamps.

**Next:** Safe to continue. **Never use `echo` in place of `printf` here** — see Section 6,
issue 7.

---

### Step 21 — Validate the private path, then lock down Key Vault

**Purpose:** Prove the private endpoint actually works *before* removing the public
fallback — never disable public access on faith (ARCHITECTURE_CONCEPTS.md §9).

**Run:**
```bash
curl https://<container-app-fqdn>/token
```

**Verify:**
```bash
az containerapp logs show -g "$RG" -n ca-colorcon-bff-np-eus --tail 20
```

**Expected result:** The `curl` returns `502 {"detail":"Failed to obtain token"}` — that's
correct, since the OAuth secrets are still placeholders. What matters is the log trace:
it must show `httpx.UnsupportedProtocol` (the empty placeholder `OAUTH_TOKEN_URL`), which
only happens *after* a successful Key Vault read. If instead you see a Key Vault
connection/auth error, the private path isn't working — stop, do not proceed to disable
public access.

**Next — only if the above passed:**
```bash
az keyvault update -g "$RG" -n kv-colorcon-bff-np-eus --public-network-access Disabled
```
Re-run the `curl`/log check above. Same result (502 at the OAuth step, not a Key Vault
error) confirms Key Vault is now working over the private endpoint exclusively.

---

### Step 22 — Validate the private path, then lock down Redis

**Purpose:** Same discipline as Step 21, for Redis.

**Run:**
```bash
curl https://<container-app-fqdn>/health/ready
```

**Verify:** Response body and status code directly (no log inspection needed here).

**Expected result:** `{"status":"ready"}`, HTTP 200.

**Next — only if the above passed:**
```bash
az redisenterprise update -g "$RG" --cluster-name redis-colorcon-bff-np-eus --public-network-access Disabled
```
Re-run the `curl` above. Same `200 {"status":"ready"}` confirms Redis is now private-only.

Note for both Step 21 and 22: disabling public *data-plane* access does not lock you out of
managing the resource — `az keyvault update` / `az redisenterprise update` are
control-plane (ARM) calls and remain reachable regardless of the resource's own network
rules.

---

## 5. Verification checklist (cumulative status)

See [issue #3](https://github.com/bsarkarlabs-stack/bff-demo/issues/3) for the live,
authoritative checklist. As of the last update:

- [x] All 4 health endpoints (`/health`, `/health/startup`, `/health/live`, `/health/ready`) return 200
- [x] All 3 probes confirmed present on the live resource
- [x] `/token` and `/health/ready` both confirmed working with Key Vault and Redis public
      access fully disabled
- [x] GitHub Actions pipeline runs green end-to-end against `ca-colorcon-bff-np-eus`
- [x] Deployed image tag matches the triggering commit SHA
- [x] Zone redundancy confirmed active on both the Container Apps Environment and Redis
- [ ] Tag audit (mandatory tags applied across all resources) — not yet done
- [ ] Cross-environment isolation test — blocked on Production existing

---

## 6. Troubleshooting & command history (failed attempts)

Every one of these actually happened during this build. Kept separate from Section 4 so
the primary runbook stays clean, but preserved in full because the fix often isn't obvious
without seeing the failure first.

**ISSUE FOUND — 1**

Existing command:
```bash
az containerapp env create -g "$RG" -n cae-colorcon-bff-np-eus -l "$LOCATION" \
  --logs-workspace-id "$LAW_ID" --logs-workspace-key "$LAW_KEY" \
  --infrastructure-subnet-resource-id "$SUBNET_ID" --zone-redundant
# ...run before the subnet had been delegated
```
Problem: `ManagedEnvironmentSubnetDelegationError: The subnet of the environment must be
delegated to the service 'Microsoft.App/environments'`.
Correct command:
```bash
az network vnet subnet update -g "$RG" --vnet-name vnet-colorcon-bff-np-eus \
  -n snet-colorcon-bff-aca-np-eus --delegations Microsoft.App/environments
# then retry the containerapp env create
```
Why: Container Apps VNet integration requires this specific subnet delegation; it is never
applied automatically, regardless of when the subnet was created. This fix is now folded
into the primary runbook as Step 4, run before Step 14.

---

**ISSUE FOUND — 2**

Existing command:
```bash
az identity federated-credential create --name gh-oidc-np --identity-name id-colorcon-bff-deploy-np-eus \
  --resource-group "$RG" --issuer https://token.actions.githubusercontent.com \
  --subject "repo:bsarkarlabs-stack/bff-demo:ref:refs/heads/develop" \
  --audiences api://AzureADTokenExchange
```
Problem: `AADSTS700213: No matching federated identity record found for presented
assertion subject 'repo:bsarkarlabs-stack@316735306/bff-demo@1333575135:environment:non-production'`.
Two separate mistakes compounded: (a) the workflow's `deploy` job sets `environment:
non-production`, which changes GitHub's OIDC subject format entirely, from
`:ref:refs/heads/<branch>` to `:environment:<name>`; (b) the org/repo segment isn't the
plain name at all — it includes immutable numeric IDs.
Correct command:
```bash
SUB_PREFIX=$(gh api repos/bsarkarlabs-stack/bff-demo/actions/oidc/customization/sub --jq ".sub_claim_prefix")
az identity federated-credential create --name gh-oidc-np-environment \
  --identity-name id-colorcon-bff-deploy-np-eus --resource-group "$RG" \
  --issuer https://token.actions.githubusercontent.com \
  --subject "${SUB_PREFIX}:environment:non-production" \
  --audiences api://AzureADTokenExchange
```
Why: Never guess the OIDC subject — query it. This is Step 18 in the primary runbook.

---

**ISSUE FOUND — 3**

Existing command:
```bash
az redisenterprise database create -g "$RG" --cluster-name redis-colorcon-bff-np-eus \
  --clustering-policy NoCluster --client-protocol Encrypted --eviction-policy VolatileLRU
# run immediately after `az redisenterprise create`, assuming no database existed yet
```
Problem: `BadRequest: 'properties.clusteringPolicy' cannot be changed. Clustering policy
cannot be changed for an existing database.` — `az redisenterprise create` had already
auto-provisioned a default database with the `OSSCluster` policy; this command was
attempting to change it in place rather than create a new one.
Correct command:
```bash
az redisenterprise database list -g "$RG" --cluster-name redis-colorcon-bff-np-eus -o json   # check first
az redisenterprise database delete -g "$RG" --cluster-name redis-colorcon-bff-np-eus --yes    # if wrong policy found
az redisenterprise database create -g "$RG" --cluster-name redis-colorcon-bff-np-eus \
  --clustering-policy NoCluster --client-protocol Encrypted --eviction-policy VolatileLRU
```
Why: `az redisenterprise create`'s behavior around auto-creating a default database is
inconsistent across runs — sometimes none is created, sometimes one is created with the
wrong policy. Always check explicitly (Step 11) rather than assuming either outcome.
Clustering policy can only be set at creation time.

---

**ISSUE FOUND — 4**

Existing command: a hand-written full Container App YAML (including a `probes:` block),
passed directly to `az containerapp create --yaml`, composed from memory/documentation
rather than a real exported example.
Problem: `Bad Request: {"...":"The JSON value could not be converted to System.Boolean...
Path: $ | LineNumber: 0 | BytePositionInLine: 4."}` — an opaque schema error with no
indication of which field was malformed.
Correct command: Create the app first with plain flags (no probes) — Step 16. Then export
its real configuration with `az containerapp show -o yaml`, edit *that* file to add the
`probes:` block, and apply with `az containerapp update --yaml` — Step 17.
Why: The Container App YAML schema has enough easy-to-miss fields (exact key casing,
required-vs-computed properties) that hand-writing it from scratch is unreliable. Starting
from a real, platform-validated export removes that entire category of error.

---

**ISSUE FOUND — 5**

Existing command:
```bash
az monitor log-analytics workspace update -g "$RG" -n law-colorcon-bff-np-eus --retention-time 14
```
Problem: `InvalidParameter: 'RetentionInDays' property doesn't match the SKU limits` — 14
days is below the minimum retention allowed on the standard `PerGB2018` pricing tier.
Correct command: No override needed — the default (30 days) is already the correct,
final value. If explicit: `az monitor log-analytics workspace update -g "$RG" -n
law-colorcon-bff-np-eus --retention-time 30`.
Why: 30 days is the platform-enforced floor for this SKU. Sub-30-day retention exists only
on the legacy Free tier (500MB/day ingestion cap — not viable for a real workload). The
originally-planned "14 days for Non-Prod" target (issue #1 §20) was written without
knowing about this platform limit; 30 days is the corrected, accepted value.

---

**ISSUE FOUND — 6**

Existing command:
```bash
az network private-link-resource list --name redis-colorcon-bff-np-eus -g "$RG" \
  --type Microsoft.Cache/redisEnterprise
```
Problem: `'Microsoft.Cache/redisEnterprise' is not a valid value for '--type'` — this
generic command's client-side type validation list doesn't include Redis Enterprise in
this CLI version. This is an outdated allowlist in the command itself, not a sign private
endpoints aren't supported for Redis Enterprise.
Correct command: Skip this exploratory check entirely and create the private endpoint
directly with the documented group ID:
```bash
az network private-endpoint create -g "$RG" -n pe-redis-colorcon-bff-np-eus \
  --vnet-name vnet-colorcon-bff-np-eus --subnet snet-colorcon-bff-pe-np-eus \
  --private-connection-resource-id "$REDIS_ID" --group-id redisEnterprise \
  --connection-name pe-conn-redis-colorcon-bff-np-eus
```
Why: `redisEnterprise` is the correct, documented private-link group ID for
`Microsoft.Cache/redisEnterprise` — confirmed by the private endpoint creating
successfully and the DNS record resolving correctly (Step 13). This is now folded directly
into the primary runbook without the failed discovery step.

---

**ISSUE FOUND — 7**

Existing command:
```bash
gh secret set AZURE_CLIENT_ID --env non-production --repo bsarkarlabs-stack/bff-demo <<< "$CLIENT_ID"
# equivalent in effect to: echo "$CLIENT_ID" | gh secret set ...
```
Problem: `az containerapp update` in the pipeline failed with `ERROR: The containerapp
'***' does not exist` even though the app was demonstrably running. Root cause: `echo`
appends a trailing newline that becomes part of the stored secret value —
`CONTAINER_APP_NAME` silently became `"ca-colorcon-bff-np-eus\n"`, and every downstream
`az` lookup failed with the same not-found-shaped error, which reads like an RBAC or
naming bug rather than a whitespace one.
Correct command:
```bash
printf '%s' "$CLIENT_ID" | gh secret set AZURE_CLIENT_ID --env non-production --repo "$REPO"
```
Why: `printf '%s'` emits no trailing newline. This is Step 20 in the primary runbook —
`printf`, never `echo`, for every secret value.

---

**ISSUE FOUND — 8**

Existing command: (deploy identity had `AcrPush`/`AcrPull` from Step 15 only — no explicit
attempt, just a missing step)
Problem: `az containerapp update` in the pipeline failed with `ERROR: The containerapp
'***' does not exist`. Azure Resource Manager returns a 404-shaped error for RBAC-denied
reads rather than a 403, specifically to avoid confirming a resource's existence to
callers who can't see it — so the failure reads like a naming/scope bug, not a permissions
one.
Correct command:
```bash
az role assignment create --assignee-object-id "$DEPLOY_PRINCIPAL" \
  --assignee-principal-type ServicePrincipal --role "Container Apps Contributor" --scope "$CA_ID"
```
Why: ACR roles only cover pushing the image to the registry — a completely separate
permission is needed to actually update the Container App resource. This is Step 19 in the
primary runbook; easy to forget because the pipeline gets much further (through OIDC
login, ACR login, and image push) before failing on this specific gap.

---

**ISSUE FOUND — 9**

Existing command:
```bash
az containerapp exec -g "$RG" -n ca-colorcon-bff-np-eus \
  --command "python -c \"import socket; print(socket.gethostbyname('kv-colorcon-bff-np-eus.vault.azure.net'))\""
```
Problem: `termios.error: (25, 'Inappropriate ioctl for device')` — `az containerapp exec`
requires an interactive TTY and cannot be driven through a non-interactive automation
session.
Correct command:
```bash
az network private-dns record-set a show -g "$RG" -z privatelink.vaultcore.azure.net \
  -n kv-colorcon-bff-np-eus --query aRecords -o json
```
Why: Confirming the private DNS zone's A record resolves to an address inside the private
endpoint subnet is sufficient proof the private path is wired correctly, and needs no
interactive session at all. This is the Verify step used in Step 10 and Step 13.

---

## 7. Optional / deferred steps

Not part of the required path above — either genuinely optional, or blocked on something
outside this runbook's control.

- **Migrate off the interim `acrbffeus`** once `rg-colorcon-bff-shared-eus` /
  `acrcolorconbffeus` exist (issue #2). Update the image reference and RBAC grants (Steps
  15, 16, 20) to point at the new registry.
- **Apply the mandatory tag set** (`client`, `application`, `environment`, `owner`,
  `managedBy` — §2) across every resource in `rg-colourcon-bbf-np-eus`. Genuinely not done
  yet (issue #3).
- **Orphaned RBAC cleanup** — already performed once, for role assignments left over from
  an earlier, now-deleted `rg-bff-np-eus`:
  ```bash
  az role assignment list --scope "$ACR_ID" -o json   # entries with empty principalName are orphaned
  az role assignment delete --ids "<orphaned-assignment-id>"
  ```
  Re-check if any resource group holding identities with grants elsewhere is ever deleted
  again — those grants don't clean themselves up.
- **Production environment** (`rg-colorcon-bff-prod-eus`) — same pattern as this entire
  document, not started. Separate future work.
- **Custom domain / DNS / TLS binding**, **production alerting**, **production HA
  sizing**, **full CI/CD policy review** — all tracked as deferred-with-a-reason in
  [issue #1](https://github.com/bsarkarlabs-stack/bff-demo/issues/1), not overlooked.
