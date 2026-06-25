<div align="center">

<img alt="Grok2API" src="https://github.com/user-attachments/assets/037a0a6e-7986-41cc-b4af-04df612ee886" />

<h1>OpenAI-Compatible Gateway for Grok Web</h1>

<h3>Multi-Account Pool · Smart Selection · Auto Maintenance</h3>

<p>
Exposes <strong>grok.com</strong> and <strong>console.x.ai</strong> chat, image, and video capabilities<br>
through a unified <strong>OpenAI / Anthropic-compatible API</strong>.
</p>

<p>
<a href="https://www.python.org/"><img alt="Python" src="https://img.shields.io/badge/python-3.13%2B-3776AB?logo=python&logoColor=white"></a>
<a href="https://fastapi.tiangolo.com/"><img alt="FastAPI" src="https://img.shields.io/badge/FastAPI-0.119%2B-009688?logo=fastapi&logoColor=white"></a>
<a href="https://github.com/jiujiu532/grok2api/pkgs/container/grok2api"><img alt="Docker" src="https://img.shields.io/badge/ghcr.io-jiujiu532%2Fgrok2api-2496ED?logo=docker&logoColor=white"></a>
<a href="LICENSE"><img alt="License" src="https://img.shields.io/badge/license-MIT-16a34a"></a>
</p>

<p>
<a href="#core-features">Core Features</a> ·
<a href="#deployment">Deployment</a> ·
<a href="#model-list">Model List</a> ·
<a href="#account-setup">Account Setup</a> ·
<a href="#api-endpoints">API Endpoints</a> ·
<a href="#faq">FAQ</a>
</p>

</div>

**English** | [中文](./README.md)

> [!NOTE]
> This project is for educational and research purposes only. Please comply with Grok's terms of service and local laws.

This repository is a secondary development based on upstream [chenyme/grok2api](https://github.com/chenyme/grok2api), adding multi-account pool management, Console free models, quota rotation, anti-blocking deployment and more. PRs and Forks are welcome — please retain original author and frontend credits in derivative works.

---

## Core Features

| Capability | Description |
| :-- | :-- |
| OpenAI Compatible | `/v1/chat/completions`, `/v1/responses`, `/v1/images/generations`, `/v1/videos` |
| Anthropic Compatible | `/v1/messages` (direct Claude SDK integration) |
| Multi-Account Pool | `basic` / `super` / `heavy` tiers with auto load-balancing and quota sync |
| Free Accounts | `console.x.ai` SSO tokens, `*-console` models at zero cost |
| Media Generation | Text-to-image, image edit, text-to-video, image-to-video with local cache & proxy links |
| Anti-Blocking Built-in | `x-statsig-id` fingerprint fix, WARP + FlareSolverr one-click deployment |
| Admin Panel | Config management, account CRUD, Web Chat, Masonry gallery, ChatKit voice |

---

## Deployment

Two deployment modes are available:

| Mode | Description | Best For |
| :-- | :-- | :-- |
| **Standard** | grok2api only, direct connection to Grok | Clean IP, no Cloudflare blocking |
| **Anti-Blocking** | grok2api + WARP + Privoxy + FlareSolverr | IP blocked by Cloudflare, stable access needed |

> [!TIP]
> The current version includes built-in 403 compatibility fixes. Try standard mode first; switch to anti-blocking if 403s persist.

---

### Standard Deployment

**Docker Compose (recommended):**

```bash
git clone https://github.com/Dithob/grok2api
cd grok2api/grok2api-main/grok2api-main
cp .env.example .env
docker compose up -d
```

Check logs:

```bash
docker compose logs -f grok2api
```

**Docker standalone container:**

```bash
docker run -d --name grok2api \
  -p 8000:8000 \
  -e TZ=Asia/Shanghai \
  -e LOG_LEVEL=INFO \
  -e ACCOUNT_STORAGE=local \
  -v $(pwd)/data:/app/data \
  -v $(pwd)/logs:/app/logs \
  --restart unless-stopped \
  ghcr.io/jiujiu532/grok2api:latest
```

Windows PowerShell:

```powershell
docker run -d `
  --name grok2api `
  -p 8000:8000 `
  -e TZ=Asia/Shanghai `
  -e LOG_LEVEL=INFO `
  -e ACCOUNT_STORAGE=local `
  -v ${PWD}/data:/app/data `
  -v ${PWD}/logs:/app/logs `
  --restart unless-stopped `
  ghcr.io/jiujiu532/grok2api:latest
```

---

### Anti-Blocking Deployment

> **Prerequisites**: Server must support `NET_ADMIN` + `SYS_MODULE` capabilities (KVM/XEN virtualization supported, OpenVZ/LXC not supported).

```bash
git clone https://github.com/Dithob/grok2api
cd grok2api/grok2api-main/grok2api-main
docker compose -f docker-compose.warp.yml up -d
```

The anti-blocking setup automatically starts the following services:

| Service | Description |
| :-- | :-- |
| `warp-proxy` | Cloudflare WARP egress proxy for clean IP |
| `privoxy` | HTTP proxy forwarding traffic to WARP |
| `flaresolverr` | Auto-solves Cloudflare challenges, obtains cf_clearance |
| `init-config` | Init container, auto-writes proxy configuration |
| `grok2api` | Main service |

After startup, proxy configuration is complete — just add accounts in the Admin panel.

---

<details>
<summary><strong>Upgrade / Rollback / Uninstall / Migrate</strong></summary>

### Upgrade

For both standard and anti-blocking modes, only update the `grok2api` main image; anti-blocking components do not need updating.

**Standard upgrade:**

```bash
docker pull ghcr.io/jiujiu532/grok2api:latest
docker compose up -d --no-deps grok2api
```

**Anti-blocking upgrade (only update main service, keep WARP/FlareSolverr running):**

```bash
docker pull ghcr.io/jiujiu532/grok2api:latest
docker compose -f docker-compose.warp.yml up -d --no-deps grok2api
```

> `--no-deps` ensures only grok2api restarts; WARP/Privoxy/FlareSolverr continue without interruption.
>
> Config (`config.toml`) and database (`accounts.db`) in `./data/` are mounted as volumes and won't be overwritten on upgrade.

### Rollback

```bash
# View available versions: https://github.com/jiujiu532/grok2api/pkgs/container/grok2api
docker pull ghcr.io/jiujiu532/grok2api:<tag>

# Standard rollback
docker compose up -d --no-deps grok2api

# Anti-blocking rollback
docker compose -f docker-compose.warp.yml up -d --no-deps grok2api
```

### Uninstall

**Standard:**

```bash
cd grok2api/grok2api-main/grok2api-main
docker compose down
# To delete data (irreversible):
rm -rf ./data ./logs
```

**Anti-blocking:**

```bash
cd grok2api/grok2api-main/grok2api-main
docker compose -f docker-compose.warp.yml down
# To delete data (irreversible):
rm -rf ./data ./logs
```

### Migrate from Standard to Anti-Blocking

Data is fully preserved — no reconfiguration needed:

```bash
# Stop standard version
docker compose down

# Start with anti-blocking (auto-detects existing config, won't overwrite)
docker compose -f docker-compose.warp.yml up -d
```

</details>

---

### Local Source Deployment

Prerequisites: Python 3.13+, [uv](https://docs.astral.sh/uv/getting-started/installation/)

```bash
git clone https://github.com/Dithob/grok2api
cd grok2api/grok2api-main/grok2api-main
cp .env.example .env && uv sync
uv run granian --interface asgi --host 0.0.0.0 --port 8000 --workers 1 app.main:app
```

---

### First Boot

Visit `http://localhost:8000/admin/login`, default password is `grok2api`. After logging in, configure:

1. `app.app_key` — Admin password
2. `app.api_key` — API authentication key (leave empty to disable auth)
3. `app.app_url` — Public URL (required for image/video links)

> Config changes take effect immediately — no restart needed.

---

## Model List

### Chat (grok.com)

`basic` = free account, `super` and `heavy` = paid accounts.

| Model | Mode | Account Tier | Notes |
| :-- | :-- | :-- | :-- |
| `grok-4.20-fast` / `grok-4.3-fast` | fast | basic (prefers higher tier) | |
| `grok-4.20-auto` | auto | super | |
| `grok-4.20-expert` | expert | super | |
| `grok-4.20-heavy` | heavy | heavy | |
| `grok-4.3-beta` | grok-420-computer-use-sa | super | |
| `grok-4.20-multi-agent-0309` | heavy | heavy | |
| `grok-4.20-0309-non-reasoning` | fast | basic | |
| `grok-4.20-0309` | auto | super | |
| `grok-4.20-0309-reasoning` | expert | super | |
| `grok-4.20-0309-non-reasoning-super` | fast | super | |
| `grok-4.20-0309-super` | auto | super | |
| `grok-4.20-0309-reasoning-super` | expert | super | |
| `grok-4.20-0309-non-reasoning-heavy` | fast | heavy | |
| `grok-4.20-0309-heavy` | auto | heavy | |
| `grok-4.20-0309-reasoning-heavy` | expert | heavy | |

### Chat (console.x.ai)

Access via SSO token for free, no paid quota consumed. All free models use **basic** tier accounts.

| Model | Reasoning Effort | Account Tier |
| :-- | :-- | :-- |
| `grok-4.3-console` | User-specified (default: medium) | basic |
| `grok-4.3-low` | low (fixed) | basic |
| `grok-4.3-medium` | medium (fixed) | basic |
| `grok-4.3-high` | high (fixed) | basic |
| `grok-4.20-0309-console` | default | basic |
| `grok-4.20-0309-reasoning-console` | fixed reasoning | basic |
| `grok-4.20-0309-non-reasoning-console` | no reasoning | basic |
| `grok-4.20-multi-agent-console` | User-specified (default: medium) | basic |
| `grok-4.20-multi-agent-low` | low (fixed) → 4 agents | basic |
| `grok-4.20-multi-agent-medium` | medium (fixed) → 4 agents | basic |
| `grok-4.20-multi-agent-high` | high (fixed) → 16 agents | basic |
| `grok-4.20-multi-agent-xhigh` | xhigh (fixed) → 16 agents | basic |
| `grok-build-console` | default | basic |

**Console quota**: 30 requests / 15-minute window, with deferred-recovery rotation strategy (timer starts when remaining reaches 15, scoring-based auto-rotation to other accounts). Background inspection every 30 seconds auto-resets expired quotas.

### Image / Video (grok.com)

| Model | Capability | Account Tier |
| :-- | :-- | :-- |
| `grok-imagine-image-lite` | Text-to-image | basic |
| `grok-imagine-image` / `image-pro` | Text-to-image | super |
| `grok-imagine-image-edit` | Image edit | super |
| `grok-imagine-video` | Text-to-video | super |

---

## Account Setup

| Type | Tier | Compatible Models |
| :-- | :-- | :-- |
| Paid account (x.ai official) | super / heavy | `grok-4.20-*`, `grok-4.3-beta`, `grok-4.3-fast` |
| Free account (console.x.ai SSO) | basic | All `*-console` / `*-low` / `*-medium` / `*-high` / `*-xhigh` |

**How to get a free account**:

1. Open browser DevTools with F12
2. Visit `https://console.x.ai/`
3. In the Network tab, find any request and copy the `sso` value from Cookies
4. Go to Admin panel → Account Management → Add Account, paste the token

> SSO tokens are sensitive credentials — do not commit them to code or version control.

---

## API Endpoints

| Endpoint | Description |
| :-- | :-- |
| `GET /v1/models` | List available models |
| `POST /v1/chat/completions` | Unified chat / image / video entry |
| `POST /v1/responses` | OpenAI Responses API |
| `POST /v1/messages` | Anthropic Messages API |
| `POST /v1/images/generations` | Image generation |
| `POST /v1/images/edits` | Image editing |
| `POST /v1/videos` | Async video job creation |
| `GET /v1/videos/{id}` / `{id}/content` | Query / download video |

---

## Environment Variables

| Variable | Description | Default |
| :-- | :-- | :-- |
| `TZ` | Timezone | `Asia/Shanghai` |
| `LOG_LEVEL` | Log level | `INFO` |
| `LOG_FILE_ENABLED` | Write logs to local file | `true` |
| `SERVER_HOST` | Listen address | `0.0.0.0` |
| `SERVER_PORT` | Listen port | `8000` |
| `SERVER_WORKERS` | Granian worker count | `1` |
| `HOST_PORT` | Compose host port mapping | `8000` |
| `DATA_DIR` | Local data root directory | `./data` |
| `LOG_DIR` | Local log directory | `./logs` |
| `ACCOUNT_STORAGE` | Storage backend: `local` / `redis` / `mysql` / `postgresql` | `local` |
| `ACCOUNT_SYNC_INTERVAL` | Incremental sync interval (seconds) | `30` |
| `ACCOUNT_SYNC_ACTIVE_INTERVAL` | Active sync interval (seconds) | `3` |
| `ACCOUNT_LOCAL_PATH` | SQLite path | `${DATA_DIR}/accounts.db` |
| `ACCOUNT_REDIS_URL` | Redis DSN | `""` |
| `ACCOUNT_MYSQL_URL` | MySQL DSN | `""` |
| `ACCOUNT_POSTGRESQL_URL` | PostgreSQL DSN | `""` |
| `ACCOUNT_SQL_POOL_SIZE` | Connection pool core size | `5` |
| `ACCOUNT_SQL_MAX_OVERFLOW` | Max connection pool overflow | `10` |
| `ACCOUNT_SQL_POOL_TIMEOUT` | Wait timeout for idle connection (seconds) | `30` |
| `ACCOUNT_SQL_POOL_RECYCLE` | Max connection reuse time (seconds) | `1800` |

Runtime config supports `GROK_` prefix overrides, e.g. `GROK_APP_API_KEY` overrides `app.api_key`.

---

## Usage Examples

```bash
# Paid account chat
curl http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"grok-4.20-auto","stream":true,"messages":[{"role":"user","content":"Hello"}]}'

# Free account chat
curl http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"grok-4.3-console","stream":true,"messages":[{"role":"user","content":"Hello"}]}'
```

---

## FAQ

| Issue | Solution |
| :-- | :-- |
| Admin panel won't open | Check port mapping and firewall: `docker compose ps` |
| Image/video links return 403 | Set `app.app_url` to your public URL (including `https://`) |
| Cloudflare blocking | Switch proxy, deploy anti-blocking mode, or manually configure `proxy.clearance.mode` |
| Multi-worker conflicts | No conflicts — scheduler uses file lock for leader election |

---

## Acknowledgments

- Upstream: [chenyme/grok2api](https://github.com/chenyme/grok2api)
- DeepWiki: [chenyme/grok2api](https://deepwiki.com/chenyme/grok2api)
- Documentation: [blog.cheny.me](https://blog.cheny.me/blog/posts/grok2api)
- Community: [Linux.do](https://linux.do)

---

## Star History

[![Star History Chart](https://api.star-history.com/svg?repos=Dithob/grok2api&type=Date)](https://star-history.com/#Dithob/grok2api&Date)

---

<div align="center">

**MIT License**

</div>
