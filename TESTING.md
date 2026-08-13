# Local Testing Guide (POC)

This documents how the BFF service was validated locally, outside of Azure, before any
cloud deployment. It covers two independent paths that were both exercised end-to-end:

1. **WSL (Ubuntu) + Python venv** — fastest inner-loop for code changes.
2. **Docker Desktop** — validates the actual container that ships to Azure Container Apps.

Both are optional for day-to-day development; Docker Desktop is the one that matters for
"does this deploy correctly," since it's the same image ACR/Container Apps will run.

---

## 1. WSL Ubuntu (venv-based test)

### 1.1 Prerequisites

- WSL2 with an Ubuntu distro installed (`wsl -l -v` to check; `Ubuntu-24.04` was used here).
- The distro's base Python image does **not** ship `pip`, `venv`, or `ensurepip`. Install
  them once per machine:

  ```bash
  sudo apt-get update
  sudo apt-get install -y python3-pip python3-venv
  ```

- **Filesystem note:** run this from the Linux filesystem (`~/`), not `/mnt/d/...`. Pip
  installs across the Windows↔WSL 9P bridge (`/mnt/*`) are noticeably slower than on native
  ext4. Copy the project in first:

  ```bash
  rm -rf ~/bff-demo && mkdir -p ~/bff-demo
  cp -r /mnt/d/bff-demo/. ~/bff-demo/
  cd ~/bff-demo
  ```

### 1.2 Install and smoke-test

```bash
cd ~/bff-demo
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt

# import check
.venv/bin/python -c "import app.main; print('IMPORT_OK')"
```

### 1.3 Run Redis (required for /health/ready and /token caching)

```bash
sudo apt-get install -y redis-server
redis-server --daemonize no --port 6379 &
redis-cli ping   # expect PONG
```

### 1.4 Run the app

```bash
.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
```

### 1.5 Test endpoints

```bash
curl -s http://localhost:8000/health          # {"status":"ok"}
curl -s http://localhost:8000/health/live      # {"status":"alive"}
curl -s http://localhost:8000/health/ready     # {"status":"ready"}  (503 if Redis is down)
curl -s http://localhost:8000/token            # 502 without a real Key Vault configured
```

`/token` requires `KEY_VAULT_URL` to point at a real Key Vault the caller can read (e.g.
`az login` locally satisfies `DefaultAzureCredential`); without it, it fails with a clean
502 and no secret is logged — this is expected in a sandbox with no Key Vault.

### 1.6 Cleanup

```bash
pkill -f 'uvicorn app.main'
pkill redis-server   # only if you started it manually; apt's systemd unit can stay running
```

### Known gotchas hit during setup

| Symptom | Cause | Fix |
|---|---|---|
| `ensurepip is not available` on `python3 -m venv` | Ubuntu 24.04's `python3` package excludes `venv`/`pip`/`ensurepip` | `sudo apt-get install python3-pip python3-venv` |
| `sudo: a password is required` | Non-interactive shell, no cached credential | Supply the password once via `echo <pw> \| sudo -S <cmd>`, don't script it into files |
| Very slow `pip install` | Project living under `/mnt/d/...` (9P filesystem) | Work from `~/` inside WSL instead |
| `python3 script.py` → `ModuleNotFoundError: No module named 'app'` | Script run by absolute path outside the project dir; Python only adds the *script's* directory to `sys.path`, not the cwd | Run the script from inside `~/bff-demo`, or `cd` there first |

---

## 2. Docker Desktop (container test)

This is the path that matters most: it builds and runs the exact image that ACR/Container
Apps will pull in Azure, wired to a real Redis over a container network — the closest local
approximation of the target architecture.

`docker` was used directly from PowerShell (Docker Desktop's engine), not via WSL — WSL
integration wasn't enabled for the Ubuntu distro, and it isn't needed since `docker` on
Windows talks to the same Docker Desktop engine.

### 2.1 Build the image

```powershell
docker build -t bff-auth:local .
```

### 2.2 Create an isolated network and start Redis

```powershell
docker network create bff-poc-net
docker run -d --name bff-redis --network bff-poc-net redis:7-alpine
```

### 2.3 Run the BFF container on the same network

```powershell
docker run -d --name bff-app --network bff-poc-net -p 8000:8000 `
  -e REDIS_HOST=bff-redis `
  -e REDIS_PORT=6379 `
  -e ENVIRONMENT=dev `
  bff-auth:local
```

`REDIS_HOST=bff-redis` resolves via Docker's embedded DNS — the same pattern used to reach
Azure Cache for Redis by hostname from a Container App.

### 2.4 Verify

```powershell
docker inspect --format "{{.State.Health.Status}}" bff-app   # expect: healthy
docker exec bff-app whoami                                    # expect: app (non-root)

Invoke-WebRequest http://localhost:8000/health -UseBasicParsing
Invoke-WebRequest http://localhost:8000/health/live -UseBasicParsing
Invoke-WebRequest http://localhost:8000/health/ready -UseBasicParsing   # 200, real Redis reachable

# expected to fail cleanly — no Key Vault in this sandbox
Invoke-WebRequest http://localhost:8000/token -UseBasicParsing   # 502, no secrets leaked
```

### 2.5 Cleanup

```powershell
docker rm -f bff-app bff-redis
docker network rm bff-poc-net
```

---

## 3. What these two paths do and don't prove

| Validated here | Not validated here (needs Azure) |
|---|---|
| App starts, imports, serves traffic | Managed Identity → Key Vault secret retrieval |
| Structured logging with secret redaction | Managed Identity → ACR pull |
| `/health`, `/health/live`, `/health/ready` behavior (up/down Redis) | Real OAuth provider round-trip |
| Dockerfile correctness (non-root user, healthcheck, exposed port) | GitHub OIDC → Azure federation |
| Redis token-cache key format via container-to-container Redis | Container Apps scaling/revision behavior |
| Graceful failure (502/503) without leaking secrets | TLS/custom domain routing per environment |

The Azure-specific gaps above are covered by the deployment plan in
[AZURE_DEPLOYMENT.md](AZURE_DEPLOYMENT.md).
