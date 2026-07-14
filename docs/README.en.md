<img alt="Grok2API" src="https://github.com/user-attachments/assets/037a0a6e-7986-41cc-b4af-04df612ee886" />

[![Python](https://img.shields.io/badge/python-3.13%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.119%2B-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![Version](https://img.shields.io/badge/version-2.0.4.rc4-111827)](../grok2api-main/grok2api-main/pyproject.toml)
[![License](https://img.shields.io/badge/license-MIT-16a34a)](../LICENSE)
[![Docker](https://img.shields.io/badge/ghcr.io-jiujiu532%2Fgrok2api-2496ED?logo=docker&logoColor=white)](https://github.com/jiujiu532/grok2api/pkgs/container/grok2api)
[![中文](https://img.shields.io/badge/%E4%B8%AD%E6%96%87-DC2626?logo=bookstack&logoColor=white)](../README.md)

> [!NOTE]
> This project is for learning and research only. You must comply with Grok's Terms of Service and your local laws. Do not use it for unlawful purposes. Forks and PRs should preserve original author and frontend attribution.

<br>

Grok2API is a **FastAPI**-based Grok gateway that exposes Grok's web capabilities through OpenAI-compatible APIs. Highlights:

- OpenAI-compatible endpoints: `/v1/models`, `/v1/chat/completions`, `/v1/responses`, `/v1/images/generations`, `/v1/images/edits`, `/v1/videos`, `/v1/videos/{video_id}`, `/v1/videos/{video_id}/content`
- Anthropic-compatible endpoint: `/v1/messages`
- Streaming and non-streaming chat, explicit reasoning output, function tools passthrough, unified token / usage accounting
- Multi-account pool, tiered selection, failure feedback, quota sync and auto maintenance
- Local image / video caching with reverse-proxied URLs
- Text-to-image, image edit, text-to-video, image-to-video
- Built-in dashboard, Admin console, Web Chat, Masonry image gallery, ChatKit voice page
- Request IDs, full streaming-response latency, and process-local 30-minute request/latency trends
- `console.x.ai` free account support with a dedicated `*-console` model family

<br>

## Image Info

This repository builds on top of [chenyme/grok2api](https://github.com/chenyme/grok2api) and ships a prebuilt Docker image:

| Field | Value |
| :-- | :-- |
| Image | `ghcr.io/jiujiu532/grok2api:latest` |
| Architecture | `linux/amd64` |
| Base image | `python:3.13-alpine` |
| Default port | `8000` |
| Data dir | `/app/data` |
| Logs dir | `/app/logs` |

<br>

## Quick Start

### Option 1: Docker Compose (recommended)

```bash
git clone https://github.com/jiujiu532/grok2api
cd grok2api/grok2api-main/grok2api-main
cp .env.example .env
docker compose up -d
```

Tail logs:

```bash
docker compose logs -f grok2api
```

> The included `docker-compose.yml` already pulls `ghcr.io/jiujiu532/grok2api:latest`. No local build is required.

### Option 2: Plain Docker

```bash
docker run -d \
  --name grok2api \
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

### Option 3: From source

Prerequisites: Python 3.13+ and [uv](https://docs.astral.sh/uv/getting-started/installation/).

```bash
git clone https://github.com/jiujiu532/grok2api
cd grok2api/grok2api-main/grok2api-main
cp .env.example .env
uv sync
uv run granian --interface asgi --host 0.0.0.0 --port 8000 --workers 1 app.main:app
```

### First-time setup

After the service is up, open `http://localhost:8000/admin/login`. Default password is `grok2api`. Then:

1. Change `app.app_key` (Admin console password)
2. Set `app.api_key` (API auth key; leave empty to disable auth)
3. Set `app.app_url` (publicly reachable base URL; otherwise image / video links return 403)

> Runtime config is persisted to `${DATA_DIR}/config.toml` and applied immediately. No container restart is required.

<br>

## Upgrade and Rollback

```bash
# Upgrade to latest
docker compose pull
docker compose up -d

# Pull a specific tag (see GHCR for available versions)
docker pull ghcr.io/jiujiu532/grok2api:latest

# Rollback
docker run -d ... ghcr.io/jiujiu532/grok2api:<tag>
```

<br>

## Reverse Proxy (Nginx example)

```nginx
server {
    listen 443 ssl http2;
    server_name your.domain.com;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # Required for streaming
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 600s;
        proxy_send_timeout 600s;
    }
}
```

After enabling the reverse proxy, set `app.app_url` to `https://your.domain.com` in the Admin console.

<br>

## WebUI

| Page | Path |
| :-- | :-- |
| Admin login | `/admin/login` |
| Dashboard | `/admin/dashboard` |
| Account management | `/admin/account` |
| Config management | `/admin/config` |
| Cache management | `/admin/cache` |
| WebUI login | `/webui/login` |
| Web Chat | `/webui/chat` |
| Masonry | `/webui/masonry` |
| ChatKit | `/webui/chatkit` |

### Authentication

| Scope | Config | Rule |
| :-- | :-- | :-- |
| `/v1/*` | `app.api_key` | No auth when empty |
| `/admin/*` | `app.app_key` | Default `grok2api` |
| `/webui/*` | `app.webui_enabled`, `app.webui_key` | Disabled by default; no extra check when `webui_key` is empty |

<br>

## Account Management

### Account types

| Type | Description | Models |
| :-- | :-- | :-- |
| **Paid account** | Official x.ai paid account | All `grok-4.20-*`, `grok-4.3-beta` |
| **Free account** | Free account via `console.x.ai` | All `*-console` models |

### Free account setup

To use free accounts you need both an SSO Token and a CF Clearance:

1. Open browser DevTools (F12)
2. Visit `https://console.x.ai/`
3. In the Network tab, inspect any request's cookies and copy:
   - the `sso` value
   - the `cf_clearance` value
4. In Admin → Account → Add account, paste both values into the matching fields

> SSO Token and CF Clearance are sensitive credentials. Never commit them to source control.

<br>

## Environment Variables

Bootstrap-time variables (`.env` / Compose / `docker run -e`):

| Name | Description | Default |
| :-- | :-- | :-- |
| `TZ` | Timezone | `Asia/Shanghai` |
| `LOG_LEVEL` | Log level | `INFO` |
| `LOG_FILE_ENABLED` | Write file logs | `true` |
| `ACCOUNT_SYNC_INTERVAL` | Account directory sync interval (s) | `30` |
| `ACCOUNT_SYNC_ACTIVE_INTERVAL` | Active sync interval after a change (s) | `3` |
| `SERVER_HOST` | Listen host | `0.0.0.0` |
| `SERVER_PORT` | Listen port | `8000` |
| `SERVER_WORKERS` | Granian workers | `1` |
| `HOST_PORT` | Compose host port mapping | `8000` |
| `DATA_DIR` | Data root | `./data` |
| `LOG_DIR` | Logs dir | `./logs` |
| `ACCOUNT_STORAGE` | Backend: `local` / `redis` / `mysql` / `postgresql` | `local` |
| `ACCOUNT_LOCAL_PATH` | SQLite path for `local` mode | `${DATA_DIR}/accounts.db` |
| `ACCOUNT_REDIS_URL` | DSN for `redis` mode | `""` |
| `ACCOUNT_MYSQL_URL` | DSN for `mysql` mode | `""` |
| `ACCOUNT_POSTGRESQL_URL` | DSN for `postgresql` mode | `""` |
| `ACCOUNT_SQL_POOL_SIZE` | SQL pool core size | `5` |
| `ACCOUNT_SQL_MAX_OVERFLOW` | SQL pool max overflow | `10` |
| `ACCOUNT_SQL_POOL_TIMEOUT` | Pool checkout timeout (s) | `30` |
| `ACCOUNT_SQL_POOL_RECYCLE` | Connection recycle time (s) | `1800` |
| `CONFIG_LOCAL_PATH` | Runtime config file path | `${DATA_DIR}/config.toml` |

Runtime config can also be overridden via `GROK_`-prefixed env vars, e.g. `GROK_APP_API_KEY` overrides `app.api_key`, `GROK_FEATURES_STREAM` overrides `features.stream`.

<br>

## Models

> Use `GET /v1/models` to fetch the live list.

### Chat (paid)

| Model | mode | tier |
| :-- | :-- | :-- |
| `grok-4.20-0309-non-reasoning` | `fast` | `basic` |
| `grok-4.20-0309` | `auto` | `super` |
| `grok-4.20-0309-reasoning` | `expert` | `super` |
| `grok-4.20-0309-non-reasoning-super` | `fast` | `super` |
| `grok-4.20-0309-super` | `auto` | `super` |
| `grok-4.20-0309-reasoning-super` | `expert` | `super` |
| `grok-4.20-0309-non-reasoning-heavy` | `fast` | `heavy` |
| `grok-4.20-0309-heavy` | `auto` | `heavy` |
| `grok-4.20-0309-reasoning-heavy` | `expert` | `heavy` |
| `grok-4.20-multi-agent-0309` | `heavy` | `heavy` |
| `grok-4.20-fast` | `fast` | `basic`, prefers higher-tier accounts |
| `grok-4.20-auto` | `auto` | `super`, prefers higher-tier accounts |
| `grok-4.20-expert` | `expert` | `super`, prefers higher-tier accounts |
| `grok-4.20-heavy` | `heavy` | `heavy` |
| `grok-4.3-beta` | `grok-420-computer-use-sa` | `super` |

### Chat (console.x.ai free)

| Model | reasoning effort | Notes |
| :-- | :-- | :-- |
| `grok-4-console` | default | Free account |
| `grok-4.3-console` | medium | Free account |
| `grok-4.3-low-console` | low | Free account |
| `grok-4.3-medium-console` | medium | Free account |
| `grok-4.3-high-console` | high | Free account |
| `grok-4.20-0309-console` | default | Free account |
| `grok-4.20-0309-reasoning-console` | fixed reasoning | Free account |
| `grok-4.20-multi-agent-console` | default | Free account, multi-agent |

### Image / Image Edit / Video

| Model | mode | tier |
| :-- | :-- | :-- |
| `grok-imagine-image-lite` | `fast` | `basic` |
| `grok-imagine-image` | `auto` | `super` |
| `grok-imagine-image-pro` | `auto` | `super` |
| `grok-imagine-image-edit` | `auto` | `super` |
| `grok-imagine-video` | `auto` | `super` |

<br>

## API Reference

| Endpoint | Auth | Description |
| :-- | :-- | :-- |
| `GET /v1/models` | yes | List enabled models |
| `GET /v1/models/{model_id}` | yes | Get a single model |
| `POST /v1/chat/completions` | yes | Unified chat / image / video entry |
| `POST /v1/responses` | yes | OpenAI Responses API subset |
| `POST /v1/messages` | yes | Anthropic Messages API |
| `POST /v1/images/generations` | yes | Standalone image generation |
| `POST /v1/images/edits` | yes | Standalone image editing |
| `POST /v1/videos` | yes | Async video job creation |
| `GET /v1/videos/{video_id}` | yes | Query a video job |
| `GET /v1/videos/{video_id}/content` | yes | Download the final video |
| `GET /v1/files/video?id=...` | no | Locally cached video |
| `GET /v1/files/image?id=...` | no | Locally cached image |

Every HTTP response includes `X-Request-ID`. Clients may supply the same request header and use it to correlate server logs; unsafe IDs are replaced. The header is also exposed to browser CORS clients. Dashboard request counters are process-local, reset on restart, and represent the worker that served the dashboard request.

<br>

## Examples

### Paid account chat

```bash
curl http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $GROK2API_API_KEY" \
  -d '{
    "model": "grok-4.20-auto",
    "stream": true,
    "reasoning_effort": "high",
    "messages": [
      {"role":"user","content":"Hello"}
    ]
  }'
```

### Free account chat

```bash
curl http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $GROK2API_API_KEY" \
  -d '{
    "model": "grok-4.3-high-console",
    "stream": true,
    "messages": [
      {"role":"user","content":"Hello"}
    ]
  }'
```

### Image generation

```bash
curl http://localhost:8000/v1/images/generations \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $GROK2API_API_KEY" \
  -d '{
    "model": "grok-imagine-image",
    "prompt": "A cat floating in outer space",
    "n": 1,
    "size": "1792x1024",
    "response_format": "url"
  }'
```

### Video generation

```bash
curl http://localhost:8000/v1/videos \
  -H "Authorization: Bearer $GROK2API_API_KEY" \
  -F "model=grok-imagine-video" \
  -F "prompt=Neon rainy night street, cinematic slow-motion tracking shot" \
  -F "seconds=10" \
  -F "size=1792x1024" \
  -F "resolution_name=720p" \
  -F "preset=normal"
```

For full field references see the upstream [API docs](https://github.com/chenyme/grok2api#api-%E4%B8%80%E8%A7%88).

<br>

## FAQ

**Q: `/admin/login` is unreachable after the container starts.**
Check the port mapping with `docker compose ps` (expect `0.0.0.0:8000->8000/tcp`) and verify your host firewall allows it.

**Q: Image / video URLs return 403.**
`app.app_url` is missing or wrong. It must be a fully qualified URL that clients can reach (e.g. `https://api.example.com`).

**Q: Cloudflare keeps blocking requests.**
In Admin → Config → Proxy, switch `proxy.clearance.mode` to `manual` and provide matching `cf_cookies` + `user_agent`, or deploy FlareSolverr and switch to the `flaresolverr` mode.

**Q: Multi-worker deployment.**
When `SERVER_WORKERS > 1`, the account refresh scheduler elects a single leader via a file lock; other workers only run lightweight syncing. On Windows, single-worker mode is recommended.

<br>

## Credits

- Upstream: [chenyme/grok2api](https://github.com/chenyme/grok2api)
- DeepWiki: [chenyme/grok2api](https://deepwiki.com/chenyme/grok2api)
- Project blog: [blog.cheny.me](https://blog.cheny.me/blog/posts/grok2api)

<br>

## License

MIT
