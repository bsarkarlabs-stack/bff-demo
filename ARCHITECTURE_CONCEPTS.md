# Architecture Concepts — Why and What

This is the "explain it to someone else" document. [AZURE_DEPLOYMENT.md](AZURE_DEPLOYMENT.md)
and [CICD_SETUP.md](CICD_SETUP.md) tell you *what commands to run*. This tells you *why
each decision was made*, what the alternative would have been, and why it was rejected —
so if someone asks "why is it built this way," there's a real answer instead of "that's
just how it is." The full target-state architecture this document explains lives in
[GitHub issue #1](https://github.com/bsarkarlabs-stack/bff-demo/issues/1).

Read this top to bottom once; use it as a reference after that — each section stands on
its own.

---

## 1. Why this is being reviewed as enterprise, not just POC

A POC review asks "does it work?" An enterprise review asks a longer list for *every*
resource: why does it exist, is it actually required, what's missing, what's
over-engineered, and then security / RBAC / networking / secrets / monitoring / scaling /
HA / cost / naming / tags / prod-vs-nonprod / operational readiness — one at a time,
deliberately, not assumed.

The reason this matters: a POC that "works" can still be one stolen credential, one
missing RBAC scope, or one forgotten alert away from an incident. The classification tags
(`✅ Enterprise Ready`, `🟠 Production Gap`, `🔴 Critical`, etc.) exist so every finding has
an explicit severity instead of being buried in prose — anyone scanning the audit can find
the 🔴 items in seconds.

## 2. Why everything is manual right now (no Terraform/Bicep/Policy)

**What:** every resource is created by hand via `az cli`, tagged by hand, RBAC'd by hand.

**Why:** at this stage the architecture itself is still being validated — IaC written
against a design that's still changing is IaC you'll rewrite twice. Manual-first lets the
team get the *shape* right (naming, RBAC boundaries, network topology) before encoding it.
The explicit `managedBy=manual` tag exists precisely so nobody mistakes a hand-built
resource for one a pipeline would reconcile — a Terraform apply against a manually-drifted
resource is a classic source of surprise deletions.

**The cost of this choice:** nothing here is self-documenting or drift-detected. Every tag,
every RBAC grant has to be manually verified against a checklist (§8 of the audit) instead
of `terraform plan` telling you what changed. That's an accepted, temporary trade-off — not
a final state.

## 3. The environment model — and the most important decision in this doc

### 3.1 One shared identity for seven Non-Prod branches

**What:** 7 approved non-prod branches (dev/qa/uat/etc., exact names still TBD) all deploy
through a single `non-production` GitHub Environment into one Azure boundary, using one
deploy identity (`id-colorcon-bff-deploy-np-eus`).

**Why not seven identities:** an identity is a security boundary, not a per-branch label.
All seven branches are trusted to deploy into the *same* non-production Azure resources —
there's no isolation need between them (they're not seven separate customers or seven
separate blast radii). Seven identities would be seven more federated credentials to
maintain, seven more RBAC assignments to audit, and zero additional security — pure
operational overhead with no corresponding benefit. The boundary that *does* need to exist
— Non-Prod vs Production — gets its own identity pair, because that boundary is real
(different data, different blast radius, different approval requirements).

### 3.2 Production requires human approval, structurally

**What:** a push to `master` triggers a GitHub Actions run targeting the `production`
Environment, which has required reviewers configured — the workflow *cannot* complete the
deploy step until a human approves it in GitHub's UI.

**Why:** this is what makes "deploy on push" and "deploy safely" compatible. Without the
Environment-level gate, the only way to get a human checkpoint before production changes is
a manual step outside the pipeline (easy to skip under pressure) or a separate release
process (more moving parts, more places to drift from what's documented). The gate is
enforced by GitHub itself, not by convention — nobody can "forget" to ask for approval.

### 3.3 The decision that changes everything: treat Non-Prod as Production, sized down

**What:** the environment referred to as "non-prod" is not going to be a scaled-down,
simplified architecture. It gets the *same features* production gets — private VNet,
private endpoints for Key Vault and Redis, zone redundancy — just with cheaper SKUs
(smaller Redis tier, lower replica counts, shorter log retention).

**Why this matters:** the entire point of a non-prod environment is to be a faithful
rehearsal of production. If non-prod is public-networked and production is
private-networked, then a successful non-prod deployment doesn't actually validate the
thing that's most likely to break in production — the network path. Private endpoint DNS
resolution, NSG rules, subnet delegation — these are exactly the kind of environment-drift
issues that "worked in non-prod, broke in prod" incidents are made of. Building both
environments the same way, differing only in size/cost, means a green non-prod deployment
is real evidence the production deployment will also work.

**What this means practically:** the resource group name stays as-is
(`rg-bff-np-eus` — renaming a resource group isn't a supported Azure operation without a
full recreate anyway), but every resource *inside* it — Key Vault, Redis, Container App,
both managed identities — gets deleted and recreated under the finalized `colorcon` naming
convention (§4), with the full production-grade feature set (private endpoints, etc.)
rather than the simpler public-network POC setup described in the original
AZURE_DEPLOYMENT.md.

## 4. Naming and tagging — why bother without Azure Policy enforcing it

**What:** `<resource-type>-colorcon-bff-<environment>-<region>`, plus a mandatory tag set
(`client`, `application`, `environment`, `owner`, `managedBy`).

**Why a client prefix (`colorcon`) at all:** this Azure subscription may not be
single-tenant to this one workload forever. A resource named `kv-bff-np-eus` says nothing
about whose workload it is if someone else's BFF-shaped project shows up later, or if this
subscription is shared across clients. `colorcon` in the name means anyone looking at the
resource list — with no other context — knows immediately whose it is and what it's for.

**Why tags matter *more*, not less, without Azure Policy:** Policy would normally enforce
tag presence automatically and reject non-compliant resources at creation time. Without it,
every tag is opt-in discipline — which means tags are the *only* signal available for
"whose is this, what does it cost, who do I page." Skipping them here isn't a shortcut, it's
a hole with no automated backstop. `managedBy=manual` in particular exists to flag "don't
assume a pipeline reconciles this" to any future automation that gets introduced.

## 5. Identity architecture — why Runtime and Deploy are never the same identity

**What:** every environment has two managed identities: a **Runtime** identity (what the
running Container App uses) and a **Deploy** identity (what GitHub Actions uses via OIDC).

**Why separate:** these two identities need *disjoint* permission sets, and collapsing them
into one would give each more privilege than it needs:

- Runtime needs: read secrets from its own Key Vault, pull images from ACR. It should
  **never** be able to push images, update the Container App, or touch another
  environment's resources — if the running application were ever compromised (e.g. a
  dependency vulnerability, an SSRF), the blast radius is capped at "can read this one
  vault's secrets," not "can redeploy the application" or "can read prod secrets."
- Deploy needs: push images to ACR, update the Container App. It should **never** be able
  to read the OAuth client secret or the Redis key — a compromised CI/CD pipeline (a
  malicious PR, a supply-chain issue in an Action) shouldn't be able to exfiltrate runtime
  secrets just because it has deploy rights.

**Why this was caught as a real gap, not theoretical:** while wiring up CI/CD, the deploy
identity was initially only granted `AcrPush`/`AcrPull` — nothing on the Container App
itself. The fix (granting `Container Apps Contributor` scoped to just that one app) is
documented in CICD_SETUP.md §4.2. That near-miss is itself the argument for keeping the
identities separate: it's much easier to reason about "does Deploy have exactly the rights
it needs" when Deploy's rights don't also include everything Runtime can do.

**Isolation is tested, not assumed:** the audit explicitly calls for proving that NP's
Runtime identity can read the NP Key Vault and gets a hard `Forbidden` against the Prod Key
Vault (and vice versa). "We used separate identities" is a design intent; "we tried the
cross-environment call and it failed" is verified fact. The one deliberate exception is the
shared ACR — both environments' runtime identities can pull from it, because the whole
point of a shared registry is that the same image serves both (§8).

## 6. GitHub OIDC — why not a service principal with a stored secret

**What:** GitHub Actions authenticates to Azure by exchanging a short-lived, per-run OIDC
token for an Azure access token, via a federated credential on the deploy identity. No
Azure secret is ever stored in GitHub.

**Why this beats a stored secret:** a service principal's client secret is a long-lived
credential sitting in GitHub's secret store — if it leaks (logged accidentally, a malicious
Action, a compromised maintainer account), it's valid until someone manually rotates it,
and rotation is a manual step someone has to remember. An OIDC federated credential has no
secret to leak at all — GitHub mints a token scoped to *this specific workflow run*, Azure
validates it against a very narrow "trust tokens matching this exact subject claim" rule,
and the token expires in minutes. There's nothing sitting in a secret store for someone to
steal.

**Why "don't guess the subject" is a hard rule, not a suggestion:** the subject claim
format isn't universal across all GitHub instances/configurations — this repo's actual
subject includes immutable numeric org/repo IDs
(`repo:org@316735306/repo@1333575135:environment:non-production`), which is *not* what
most OIDC tutorials show. Federating against a guessed subject fails with
`AADSTS700213: No matching federated identity record found` — not a helpful error pointing
at the real cause. The fix is procedural: always query
`gh api repos/<org>/<repo>/actions/oidc/customization/sub` and federate against exactly
what it returns, for *every* repo — this behavior is repo-specific configuration, not a
fact you can memorize once (CICD_SETUP.md §3.1).

**Why secrets are Environment-scoped, not repo-scoped:** a repo-level secret is visible to
every workflow run regardless of which branch or environment triggered it. Scoping
`AZURE_CLIENT_ID` etc. to the `non-production` GitHub Environment specifically means a
workflow run that somehow targets `production` structurally *cannot* see non-prod's
credentials, and vice versa — the isolation is enforced by GitHub's permission model, not
by hoping the workflow YAML never has a bug.

## 7. Container Registry — why one shared ACR, and why Production never rebuilds

**What:** a single ACR (`acrcolorconbffeus`) serves both environments. Images are tagged by
git SHA, never `latest`.

**Why shared instead of one ACR per environment:** the entire safety property of "what we
tested in non-prod is what runs in production" depends on promoting the *exact same
compiled artifact* — same base image layers, same dependency versions, same Docker build
output — rather than rebuilding from source a second time for prod. A prod-specific ACR
would either need the image copied over (extra step, extra place for drift) or rebuilt from
the same source (which isn't actually the same artifact — a `pip install` run twice can
resolve different transitive dependency versions if anything upstream moved). One registry,
one build, promoted by reference, removes that entire class of "it worked in staging but
not prod" bug.

**Why never `latest`:** `latest` is a mutable pointer — "what's running in production" and
"what `latest` points to right now" can silently diverge the moment someone pushes a newer
image, with no record of when that pointer moved. A git-SHA tag is immutable and
self-documenting: `bff-auth:aefcb5f` tells you exactly which commit is running, forever,
and lets you trace container → image → commit → PR → author without guessing.

**Why the ACR is public, for now:** the ACR is shared across three consumers — GitHub
Actions (from the internet), NP runtime, and Prod runtime. Making it private would mean all
three need a network path into it (a private endpoint from GitHub Actions isn't
straightforward — self-hosted runners inside the VNet, or a more complex setup), which is a
real cost/complexity trade-off, not a default "more private is always better" call. This is
explicitly flagged as revisit-if-required, not settled forever.

## 8. Monitoring — why not just turn everything on

**What:** separate Log Analytics + Application Insights per environment, selectively
enabled diagnostic settings, no blanket enablement of every available feature.

**Why not enable everything by default:** each monitoring feature (Smart Detection,
Availability Tests, every diagnostic category, long retention) has a cost — literal
Azure spend, but also signal-to-noise cost: more dashboards nobody looks at, more alerts
that get muted because they fire too often, more retained data that's never queried. The
discipline here is to enable each thing because it answers a specific operational question,
not because the toggle exists. Retention specifically (14 days NP, 30 days Prod initially)
is set to a reasonable starting point and increased only if an actual compliance or audit
requirement demands more — not preemptively, since retained log volume is a direct cost
driver.

**Why the "never log secrets" rule is absolute:** a token, secret, or auth header that ends
up in Application Insights or Log Analytics is now sitting in a system with a different
(often broader) access-control model than the vault it came from — anyone with Log Analytics
Reader can now see it, which was never true of the Key Vault secret itself. This is a
one-way door: you can rotate a leaked secret, but you generally can't be certain a log
platform's cached/indexed copies are fully gone. Treat it as a hard boundary, not a
best-effort.

## 9. Key Vault — why RBAC, why private, and why the *order* of hardening matters

**What:** RBAC-mode authorization (not the older access-policy model), Non-Prod currently
public, Production private via VNet + Private Endpoint + Private DNS, built in a specific
sequence.

**Why RBAC over access policies:** access policies are Key-Vault-specific and don't compose
with the rest of Azure's permission model — you manage them in a completely different UI/API
than every other resource's RBAC. RBAC mode means Key Vault permissions show up in the same
`az role assignment list` as everything else, audit the same way, and can be scoped as
tightly (per-vault, even per-operation-type like `Key Vault Secrets User` vs `...Officer`)
as any other resource.

**Why Production needs a Private Endpoint and Non-Prod (for now) doesn't:** production Key
Vault holds the real OAuth credentials and Redis key for the real environment. Every network
hop a public endpoint traverses is attack surface — even with strong authentication, a
public endpoint is discoverable and reachable from anywhere, relying entirely on identity to
be the only defense. A private endpoint removes that layer's exposure: the vault simply
isn't reachable except from inside the VNet at all, adding a second, independent barrier
beyond "you need the right identity."

**Why the hardening order is a hard sequence, not a suggestion:**

```text
create → RBAC → private endpoint → private DNS → validate the private path works
                                                        ↓
                                          only THEN disable public access
                                                        ↓
                                                validate again
```

Disabling public access *before* confirming the private path actually resolves and connects
is how you lock yourself out of your own Key Vault — the Container App would suddenly have
no working path to its secrets, and diagnosing "is this a DNS problem, an NSG problem, or a
private endpoint provisioning problem" from a fully-locked-down state is much harder than
proving the private path first while public access is still there as a fallback.

## 10. Redis — why it's a cache, not a database, and why keys are namespaced

**What:** Azure Managed Redis holds only short-lived OAuth access tokens, keyed as
`bff:<environment>:<backend>:<client-id>:token`, TTL derived from the OAuth token's own
`expires_in`.

**Why Redis can never hold the OAuth client secret or any permanent credential:** Redis is
optimized for speed and volatility, not durability or fine-grained access control — it's
one flat keyspace with one shared credential, not a per-secret ACL system like Key Vault.
Anything that ends up in Redis is only as protected as "whoever holds the Redis key can read
everything in the cache." Long-lived, high-value secrets belong somewhere with real
per-secret access control and audit logging — Key Vault. Redis's job is purely to avoid
re-requesting an OAuth token on every single BFF request.

**Why namespacing the keys matters here specifically:** because §3.1 puts all 7 non-prod
branches on one shared Redis instance, a flat key like `oauth_token` would mean DEV and QA
and UAT all overwrite each other's cached token — worse, a token cached under one client's
credentials could get served to a request meant for a different backend. Namespacing by
environment *and* backend *and* client ID makes collision structurally impossible instead of
relying on discipline.

**Why TTL is derived, not a round number:** picking "1 day" or "7 days" arbitrarily means
the cache can serve a token *after* the OAuth provider has already expired it (a stale-token
bug that only shows up as mysterious 401s downstream), or it expires the cache *before* the
token actually needed replacing (wasted OAuth round-trips). `TTL = expires_in - safety
buffer` keeps the cache's notion of "valid" tied to the actual source of truth.

## 11. Networking — why a dedicated VNet, and why NAT Gateway/Firewall aren't there yet

**What:** Production gets its own VNet with two subnets — one for the Container Apps
Environment, one for private endpoints — rather than reusing an existing shared VNet.

**Why a dedicated VNet over reusing an existing one:** a shared/existing VNet means this
workload's network requirements (subnet sizing, delegation, NSG rules) are entangled with
whatever else lives on that VNet, and getting permission to make changes often means asking
for `Network Contributor` over the whole thing — far more access than this workload needs.
A dedicated VNet keeps the blast radius and the permission surface scoped to just this
workload. (Reusing an existing VNet remains a documented fallback option — with the
recommendation to scope RBAC to just the required subnet, not the whole network resource
group, if that path is chosen instead.)

**Why private endpoints get their own subnet, separate from the Container Apps subnet:**
Container Apps Environment subnet delegation has specific platform requirements around what
else can live there; mixing concerns (compute + private endpoint NICs in the same subnet)
makes both harder to reason about independently and complicates future CIDR/NSG changes.
Separating them means each subnet's purpose — and its IP sizing — is unambiguous.

**Why CIDR isn't just picked:** guessing a range like `10.20.0.0/16` risks colliding with
ranges Colorcon already uses elsewhere (on-prem, another Azure VNet, a VPN/ExpressRoute
peering) — a collision discovered *after* deployment means re-addressing a live network,
which is far more disruptive than asking the question up front.

**Why NAT Gateway and Azure Firewall aren't provisioned right now:** both solve a specific
problem — a fixed outbound IP for allowlisting, or centralized egress inspection — that
doesn't currently exist here (frontend and backend are both public, nothing requires the
BFF's outbound traffic to originate from a known static IP). Provisioning them anyway would
be paying for and operating infrastructure with no corresponding requirement. The moment a
real requirement shows up (e.g. a backend that IP-allowlists callers), this gets revisited —
it's deferred because it's unneeded, not because it was overlooked.

## 12. Container Apps scaling and zone redundancy

**What:** Non-Prod scales to zero (`minReplicas=0, maxReplicas=3`); Production never scales
to zero, with exact limits still pending real load data. Zone redundancy is required for
*both* environments.

**Why scale-to-zero is fine for Non-Prod but not Production:** non-prod traffic is
intermittent (developers testing, CI smoke tests) — paying for an always-on replica that
sits idle most of the day is pure waste, and the cost of that trade-off (a cold start on the
first request after idle) is something developers can tolerate. Production traffic is
presumably continuous and user-facing — a cold start there is a real user-facing latency
spike, unacceptable for the same reason it's fine in non-prod.

**Why zone redundancy is required even for Non-Prod, when everything else about Non-Prod is
cost-optimized:** this is a direct consequence of §3.3 — non-prod is a rehearsal for
production's *architecture*, including its resilience characteristics. If zone redundancy
were skipped in non-prod, a zone-failure-related bug (a resource that doesn't fail over
correctly) would never surface until it happened for real in production. It's cheap to
enable and directly validates something that's expensive to discover is broken later.

## 13. Health probes — why liveness must never depend on Redis, Key Vault, or OAuth

**What:** three separate probes — Startup, Liveness, Readiness — each with a distinct job.
Liveness checks only that the application process itself is running; it never checks
external dependencies.

**Why this specific split matters — walk through the failure it prevents:** imagine Redis
has a transient blip (a failover, a brief network partition) for 30 seconds. If liveness
checked Redis connectivity, Container Apps would see the liveness probe fail and conclude
the *container* is broken — and kill and restart it. But the container wasn't broken at
all; Redis had a normal, self-recovering hiccup. Now you've turned a 30-second Redis blip
into a full container restart (losing in-flight requests, cold-starting again) — the
"health check" made the outage worse, not better. Readiness is the correct place to check
dependencies: a readiness failure just means "don't route new traffic here right now," which
is the proportionate response to "a dependency is temporarily unavailable."

**Why probe timing isn't picked upfront:** `initialDelaySeconds`, `periodSeconds`, etc. only
make sense relative to how long this specific application actually takes to start (Key
Vault client init, Redis connection setup, normal vs cold start). Setting these before
measuring real numbers means either probes that are too aggressive (false-positive restarts
during normal slow starts) or too lenient (a genuinely hung container takes too long to get
detected and replaced).

## 14. Secrets and credential lifecycle — the underlying principle

Across Key Vault, Redis, and OAuth, the same rule keeps appearing: **long-lived, high-value
credentials live in Key Vault with per-secret RBAC and audit logging; nothing else is
allowed to become a shadow copy of them.** Not GitHub secrets (OIDC removes the need
entirely), not Redis (cache only, token-only, TTL'd), not logs (never, under any
circumstance), not a CLI variable passed by hand into another system's secret store when a
direct Key Vault reference would work instead. Every "why can't this just live in X"
question in this architecture traces back to that one rule.

## 15. Why some things are explicitly deferred, not skipped

Custom domains/DNS/TLS, production alerting, production HA sizing, and a full CI/CD policy
review are all marked deferred, not done. The distinction matters: each of these depends on
an input this team doesn't own yet — Colorcon's approved domain/naming standard, the actual
Action Group/notification owners, real production load numbers, or simply sequencing (get
the Azure resource/network foundation right before optimizing the pipeline built on top of
it). Building any of these now would mean guessing at someone else's requirement and likely
redoing it — deferred-with-a-reason is a decision, not a gap that was overlooked.

---

## Quick-reference: likely questions and short answers

**"Why can't we just use one managed identity for everything?"**
Because Runtime and Deploy need disjoint, non-overlapping permissions — one identity means
either over-privileging the running app or under-privileging the pipeline. See §5.

**"Why does non-prod need a private VNet — isn't that a production-only concern?"**
Because non-prod's job is to prove production will work, and network topology is exactly
the kind of thing that silently differs between environments if you let it. See §3.3.

**"Why not just use `latest` for the image tag, it's simpler?"**
Because "simpler to write" trades away "know exactly what's running" — see §7.

**"Why is the Key Vault public in Non-Prod if Production is private?"**
Non-Prod doesn't hold production credentials, and it's mid-rebuild toward the same private
model — see §3.3's migration note and §9.

**"Can we just store the Azure credential as a GitHub secret instead of dealing with OIDC?"**
You could, but it's a long-lived, leakable, manually-rotated credential vs. a short-lived
token with nothing to steal. See §6.

**"Why do we need seven branches to share one identity instead of seven separate ones?"**
Because an identity should map to a security boundary, and all seven non-prod branches
share the same boundary. See §3.1.

**"Why did the Redis TTL choice matter — couldn't we just cache tokens for a day?"**
Because a fixed TTL can outlive or underlive the actual OAuth token's real expiry — see §10.
