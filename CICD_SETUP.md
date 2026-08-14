# CI/CD Setup Guide — BFF Auth Service

How to wire GitHub Actions to deploy this repo to Azure Container Apps via OIDC, with
every step actually run and verified against the real `bsarkarlabs-stack/bff-demo` repo
and the `np` (non-production) Azure environment. Production follows the identical pattern
against `rg-bff-prod-eus` — see [§8](#8-doing-this-for-production).

This assumes the Azure resources in [AZURE_DEPLOYMENT.md](AZURE_DEPLOYMENT.md) already
exist (`rg-bff-np-eus`, `acrbffeus`, `ca-bff-np-eus`, the `id-bff-deploy-np-eus` /
`id-bff-runtime-np-eus` identities, etc.). This doc is only about the GitHub↔Azure wiring
on top of that.

---

## 1. Architecture

```text
git push (develop)
      |
      v
GitHub Actions: build job
  - checkout, install deps, build Docker image
  - Trivy vulnerability scan (fails on fixable CRITICAL/HIGH)
  - upload image as a workflow artifact
      |
      v
GitHub Actions: deploy job (environment: non-production)
  - OIDC token requested for this job -----------------> Microsoft Entra ID
                                                                 |
                                          federated credential match (no stored secret)
                                                                 |
                                                                 v
  - az login (OIDC) <----------------------------------- short-lived Azure token
  - az acr login, docker push -----------------------------> acrbffeus
  - az containerapp update --image ... ----------------------> ca-bff-np-eus
  - curl .../health -----------------------------------------> confirms the new revision is live
```

No Azure credential is ever stored in GitHub. The `deploy` job exchanges a GitHub-issued
OIDC token for a short-lived Azure token, via a **federated identity credential** on the
`id-bff-deploy-np-eus` managed identity that says "trust tokens GitHub issues for this
repo's `non-production` environment."

---

## 2. Prerequisites

- `gh` CLI authenticated (`gh auth status`) with `repo` and `workflow` scopes.
- `az` CLI logged into the subscription that holds `rg-colourcon-bbf-np-eus`.
- The Azure resources from AZURE_DEPLOYMENT.md §4.1–4.10 already exist.

---

## 3. Federate the deploy identity with GitHub OIDC

### 3.1 Get the exact OIDC subject GitHub will present

Don't guess this string. On this GitHub platform version the subject includes **immutable
numeric org/repo IDs**, not plain names, even when `use_immutable_subject` is `false`:

```bash
gh api repos/bsarkarlabs-stack/bff-demo/actions/oidc/customization/sub
```

```json
{
  "use_default": true,
  "use_immutable_subject": false,
  "sub_claim_prefix": "repo:bsarkarlabs-stack@316735306/bff-demo@1333575135"
}
```

### 3.2 Know which subject format applies

The workflow's `deploy` job sets `environment: non-production` (or `production`). **Any
job that specifies `environment:` gets an environment-based OIDC subject**, not the more
commonly-documented ref-based one:

| Job behavior | Subject GitHub presents |
|---|---|
| No `environment:` key | `repo:<prefix>:ref:refs/heads/<branch>` |
| Has `environment: <name>` (this workflow, always) | `repo:<prefix>:environment:<name>` |

Federating against the ref-based subject — the pattern most tutorials show — will fail
with `AADSTS700213: No matching federated identity record found`, because GitHub never
actually presents that subject for a job that targets an environment.

### 3.3 Create the federated credential

```bash
SUB_PREFIX=$(gh api repos/bsarkarlabs-stack/bff-demo/actions/oidc/customization/sub --jq ".sub_claim_prefix")

az identity federated-credential create \
  --name gh-oidc-np-environment \
  --identity-name id-bff-deploy-np-eus \
  --resource-group rg-bff-np-eus \
  --issuer https://token.actions.githubusercontent.com \
  --subject "${SUB_PREFIX}:environment:non-production" \
  --audiences api://AzureADTokenExchange
```

---

## 4. Grant the deploy identity permission to actually deploy

Two separate grants — ACR push and Container App update — are both required. It's easy to
set up the first and forget the second, since the pipeline gets much further before
failing.

### 4.1 ACR push (build → registry)

```bash
ACR_ID=$(az acr show -n acrbffeus --query id -o tsv)
DEPLOY_PRINCIPAL=$(az identity show -g rg-bff-np-eus -n id-bff-deploy-np-eus --query principalId -o tsv)

az role assignment create --assignee-object-id "$DEPLOY_PRINCIPAL" \
  --assignee-principal-type ServicePrincipal --role AcrPush --scope "$ACR_ID"
```

### 4.2 Container Apps update (registry → running revision)

Easy to miss because the failure doesn't look like a permissions problem. Without this
grant, `az containerapp update` in the pipeline fails with:

```text
ERROR: The containerapp '***' does not exist
```

— even though the app is clearly running — because Azure Resource Manager returns a
404-shaped error for RBAC-denied reads rather than a 403, to avoid confirming a resource's
existence to callers who can't see it. Scope the grant to just this one app (least
privilege, not the whole resource group):

```bash
CA_ID=$(az containerapp show -g rg-bff-np-eus -n ca-bff-np-eus --query id -o tsv)

az role assignment create --assignee-object-id "$DEPLOY_PRINCIPAL" \
  --assignee-principal-type ServicePrincipal --role "Container Apps Contributor" --scope "$CA_ID"
```

RBAC propagation can lag by up to ~30s after either grant — if the very next pipeline run
still 404s, wait and re-run before assuming something else is wrong.

---

## 5. Create the GitHub Environment and secrets

The Environment name must exactly match what the workflow computes
(`github.ref_name == 'master' && 'production' || 'non-production'`):

```bash
gh api --method PUT repos/bsarkarlabs-stack/bff-demo/environments/non-production
```

Then set these seven secrets, scoped to that Environment (not repo-wide — so a `master`
deploy can never see non-prod's identity, or vice versa):

| Secret | Value (non-production) |
|---|---|
| `AZURE_CLIENT_ID` | `id-bff-deploy-np-eus` client ID |
| `AZURE_TENANT_ID` | the Azure AD tenant ID |
| `AZURE_SUBSCRIPTION_ID` | the subscription ID |
| `ACR_NAME` | `acrbffeus` |
| `RESOURCE_GROUP` | `rg-bff-np-eus` |
| `CONTAINER_APP_NAME` | `ca-bff-np-eus` |
| `CONTAINER_APP_URL` | the app's `https://...azurecontainerapps.io` FQDN |

**Use `printf`, not `echo`, piping into `gh secret set`.** `echo` appends a trailing
newline that becomes part of the stored secret value (`CONTAINER_APP_NAME` silently becomes
`"ca-bff-np-eus\n"`), and every downstream `az` lookup then fails with the same
not-found-shaped error as §4.2 — easy to mis-diagnose as another RBAC gap.

```bash
REPO=bsarkarlabs-stack/bff-demo

printf '%s' "$(az identity show -g rg-bff-np-eus -n id-bff-deploy-np-eus --query clientId -o tsv)" \
  | gh secret set AZURE_CLIENT_ID --env non-production --repo "$REPO"
printf '%s' "$(az account show --query tenantId -o tsv)" \
  | gh secret set AZURE_TENANT_ID --env non-production --repo "$REPO"
printf '%s' "$(az account show --query id -o tsv)" \
  | gh secret set AZURE_SUBSCRIPTION_ID --env non-production --repo "$REPO"
printf '%s' "acrbffeus" | gh secret set ACR_NAME --env non-production --repo "$REPO"
printf '%s' "rg-bff-np-eus" | gh secret set RESOURCE_GROUP --env non-production --repo "$REPO"
printf '%s' "ca-bff-np-eus" | gh secret set CONTAINER_APP_NAME --env non-production --repo "$REPO"
printf '%s' "https://ca-bff-np-eus.<env-id>.eastus.azurecontainerapps.io" \
  | gh secret set CONTAINER_APP_URL --env non-production --repo "$REPO"

gh secret list --env non-production --repo "$REPO"
```

---

## 6. The workflow file

[.github/workflows/ci-cd.yml](.github/workflows/ci-cd.yml), two jobs:

**`build`** (always runs, on push and PR): checkout → install deps → `docker build` →
Trivy scan → save the image as a workflow artifact. Doesn't touch Azure — a PR from a fork
can run this safely with no credentials at all.

**`deploy`** (push events only, `needs: build`): downloads the artifact, logs into Azure
via OIDC, pushes the image to ACR, updates the Container App, then curls `/health` to
confirm the new revision actually came up before declaring success.

Two config details worth knowing about, both learned by hitting them:

- **Trivy tag format**: `aquasecurity/trivy-action` releases are tagged `v0.36.0`, not
  `0.36.0` — the unprefixed form fails immediately with `Unable to resolve action`, before
  any real work happens.
- **`ignore-unfixed: true`**: the Debian base image will always carry some CVEs with no
  published fix yet (blank `Fixed Version` in Trivy's own output). Gating `exit-code: 1`
  on those blocks CI permanently. `ignore-unfixed: true` scopes the gate to CVEs that are
  actually fixable — which is also how the one real finding on this repo got caught: an
  outdated `starlette`, pinned transitively via `fastapi==0.115.0`, with known CVEs fixed
  in later `starlette` releases. Fixed by bumping `fastapi`.

---

## 7. Trigger and validate

```bash
git checkout develop
git merge master --ff-only   # or whatever change you're actually shipping
git push

gh run list --branch develop --limit 1
gh run watch <run-id> --exit-status
```

Expect both jobs green, ending with the `deploy` job's `Health check` step succeeding.
Confirm independently:

```bash
curl https://ca-bff-np-eus.<env-id>.eastus.azurecontainerapps.io/health
```

---

## Troubleshooting quick reference

All five of these were hit for real getting this repo's pipeline green — not
hypothetical.

| Symptom | Cause | Fix |
|---|---|---|
| `Unable to resolve action 'aquasecurity/trivy-action@0.24.0'` | Tag missing `v` prefix | Use `@v0.36.0` (or current latest) |
| `AADSTS700213: No matching federated identity record found`, subject ends `:ref:refs/heads/<branch>` | Federated against ref-based subject, but the job sets `environment:` | Re-federate against `...:environment:<name>` (§3) |
| Same AADSTS700213, subject shows `org/repo` not `org@id/repo@id` | Guessed the subject instead of reading it from the API | Use `gh api .../actions/oidc/customization/sub` (§3.1) |
| `ERROR: The containerapp '***' does not exist` — but it demonstrably does | Deploy identity has no RBAC on the Container App (only ACR roles) | Grant `Container Apps Contributor` scoped to the app (§4.2) |
| Same "does not exist" error, persists after granting RBAC | Secret value has a trailing `\n` from `echo \| gh secret set` | Re-set with `printf '%s'`, not `echo` (§5) |
| Trivy scan step fails, findings all show blank `Fixed Version` | Blocking on CVEs with no upstream fix yet | Add `ignore-unfixed: true` |
| Trivy scan step fails, findings show a real `Fixed Version` | An actual outdated, vulnerable dependency | Bump that dependency (verify the app still imports/runs before pushing again) |

---

## 8. Doing this for production

Identical steps, swapped inputs — not yet done for this repo (no `production` GitHub
Environment, secrets, or federated credential exist yet):

- Identity: `id-bff-deploy-prod-eus` in `rg-bff-prod-eus`
- Federated credential subject: `<sub_claim_prefix>:environment:production`
- GitHub Environment: `production` — add **required reviewers** here (Settings →
  Environments → production → Deployment protection rules), so `master` pushes pause for
  approval before deploying, per PRD §20's promotion-gate requirement. This is the one
  meaningful difference from non-prod's setup.
- RBAC: `AcrPull` only on `acrbffeus` (prod doesn't build its own images — PRD §12/§1's
  build-once-promote model means it deploys an image non-prod's pipeline already pushed),
  plus `Container Apps Contributor` scoped to `ca-bff-prod-eus`.
- Secrets: same seven keys, scoped to the `production` Environment, pointed at
  `rg-bff-prod-eus` / `ca-bff-prod-eus` / that app's FQDN.
