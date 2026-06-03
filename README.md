<img alt="Grok2API" src="https://github.com/user-attachments/assets/037a0a6e-7986-41cc-b4af-04df612ee886" />

[![Python](https://img.shields.io/badge/python-3.13%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.119%2B-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![License](https://img.shields.io/badge/license-MIT-16a34a)](LICENSE)
[![Docker](https://img.shields.io/badge/ghcr.io-jiujiu532%2Fgrok2api-2496ED?logo=docker&logoColor=white)](https://github.com/jiujiu532/grok2api/pkgs/container/grok2api)

> [!NOTE]
> 本项目仅供学习与研究交流。请务必遵守 Grok 的使用条款及当地法律法规，不得用于非法用途。二开与 PR 请保留原作者与前端标识。

<br>

Grok2API 是一个基于 **FastAPI** 构建的 Grok 网关，将 Grok Web 能力以 OpenAI 兼容 API 的方式对外提供。核心特性：

- OpenAI 兼容接口：`/v1/models`、`/v1/chat/completions`、`/v1/responses`、`/v1/images/generations`、`/v1/images/edits`、`/v1/videos`、`/v1/videos/{video_id}`、`/v1/videos/{video_id}/content`
- Anthropic 兼容接口：`/v1/messages`
- 支持流式与非流式对话、显式思考输出、函数工具结构透传，统一的 token / usage 统计
- 支持多账号池、层级选号、失败反馈、额度同步与自动维护
- 支持本地缓存图片、视频与本地代理链接返回
- 支持文生图、图像编辑、文生视频、图生视频
- 内置 Admin 后台管理、Web Chat、Masonry 生图、ChatKit 语音页面
- 支持 `console.x.ai` 免费账号，新增 `*-console` 模型系列
- 支持 `x-statsig-id` 动态签名（无头浏览器方案），解决 Grok 反爬校验 403 问题

<br>

## 镜像说明

本仓库基于上游 [chenyme/grok2api](https://github.com/chenyme/grok2api) 二次构建，提供预编译的 Docker 镜像：

### grok2api 主镜像

| 项 | 值 |
| :-- | :-- |
| 镜像地址 | `ghcr.io/jiujiu532/grok2api:latest` |
| 架构 | `linux/amd64`, `linux/arm64` |
| 基础镜像 | `python:3.13-alpine` |
| 默认端口 | `8000` |
| 默认数据目录 | `/app/data` |
| 默认日志目录 | `/app/logs` |

### privoxy-warp 镜像（防封版专用）

| 项 | 值 |
| :-- | :-- |
| 镜像地址 | `ghcr.io/jiujiu532/privoxy-warp:latest` |
| 架构 | `linux/amd64`, `linux/arm64` |
| 说明 | 预配置好 WARP SOCKS5 转发规则的 Privoxy，与 `caomingjun/warp` 配合使用 |

<br>

## 快速开始

本项目提供两种部署方式，按需选择：

| 方式 | 说明 | 适用场景 |
| :-- | :-- | :-- |
| **标准版** | 仅 grok2api，直连 Grok | IP 干净、无 Cloudflare 拦截问题 |
| **防封版** | grok2api + WARP + Privoxy + FlareSolverr | IP 被 Cloudflare 拦截、需要稳定访问 |

### 方式一：标准版（Docker Compose）

```bash
git clone https://github.com/jiujiu532/grok2api
cd grok2api/grok2api-main/grok2api-main
cp .env.example .env
docker compose up -d
```

查看日志：

```bash
docker compose logs -f grok2api
```

> 使用 `docker-compose.yml`，仅启动 grok2api 容器，代理配置默认为空（直连）。

### 方式二：防封版（WARP + FlareSolverr 一键部署）

> **前置要求**：服务器需支持 `NET_ADMIN` + `SYS_MODULE` 权限（KVM/XEN 虚拟化均支持，OpenVZ/LXC 不支持）。

```bash
git clone https://github.com/jiujiu532/grok2api
cd grok2api/grok2api-main/grok2api-main
docker compose -f docker-compose.warp.yml up -d
```

防封版会自动启动以下服务并完成配置：

| 服务 | 说明 |
| :-- | :-- |
| `warp-proxy` | Cloudflare WARP 出口代理，提供干净的 Cloudflare IP |
| `privoxy` | HTTP 代理，将流量转发到 WARP（已预配置，无需手动操作） |
| `flaresolverr` | 自动解 Cloudflare 挑战，获取 cf_clearance |
| `statsig-signer` | 无头浏览器签名服务，动态获取真实 `x-statsig-id`（首次启动约 60-90s） |
| `grok2api` | 主服务，代理配置由 init 容器自动写入 |

启动后代理配置已自动完成，进入 Admin 后台添加账号即可使用。

### 方式三：Docker 单容器

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

Windows PowerShell：

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

### 方式四：本地源码部署

前置：Python 3.13+、[uv](https://docs.astral.sh/uv/getting-started/installation/)。

```bash
git clone https://github.com/jiujiu532/grok2api
cd grok2api/grok2api-main/grok2api-main
cp .env.example .env
uv sync
uv run granian --interface asgi --host 0.0.0.0 --port 8000 --workers 1 app.main:app
```

### 首次启动

服务启动后访问 `http://localhost:8000/admin/login`，默认密码为 `grok2api`，进入后依次完成：

1. 修改 `app.app_key`（Admin 后台登录密码）
2. 设置 `app.api_key`（API 调用鉴权密钥，留空则不鉴权）
3. 设置 `app.app_url`（公网地址，否则图片、视频链接会 403）

> 运行时配置写入 `${DATA_DIR}/config.toml`，保存后即时生效，无需重启容器。

<br>

## 升级与回滚

无论标准版还是防封版，升级时只需要更新 `grok2api` 主镜像即可。WARP、Privoxy、FlareSolverr 等防封组件基本不需要更新。

### 标准版升级

```bash
docker pull ghcr.io/jiujiu532/grok2api:latest
docker compose up -d --no-deps grok2api
```

### 防封版升级（只更新 grok2api，不动防封组件）

```bash
docker pull ghcr.io/jiujiu532/grok2api:latest
docker compose -f docker-compose.warp.yml up -d --no-deps grok2api
```

> `--no-deps` 参数确保只重启 grok2api 容器，WARP、Privoxy、FlareSolverr 不受影响，继续运行。

> `./data/` 目录中的配置文件（`config.toml`）和账号数据库（`accounts.db`）挂载在 volume 中，升级不会覆盖。

### 回滚到指定版本

```bash
# 查看可用版本：https://github.com/jiujiu532/grok2api/pkgs/container/grok2api
docker pull ghcr.io/jiujiu532/grok2api:<tag>
docker compose up -d --no-deps grok2api
# 或防封版：
docker compose -f docker-compose.warp.yml up -d --no-deps grok2api
```

### 从标准版迁移到防封版

已有标准版部署的用户，迁移到防封版无需重新配置，数据完全保留：

```bash
# 1. 停止并删除当前 grok2api 容器（数据不受影响）
docker stop grok2api && docker rm grok2api

# 2. 进入项目目录（与标准版相同目录）
cd grok2api/grok2api-main/grok2api-main

# 3. 用防封版 compose 启动（会自动启动 WARP、Privoxy、FlareSolverr）
docker compose -f docker-compose.warp.yml up -d
```

> 防封版的 `init-config` 容器会检测 `data/config.toml` 是否已有代理配置：
> - 若已有配置（如之前手动配过代理）：跳过，不覆盖
> - 若无代理配置：自动写入 WARP + FlareSolverr 配置

迁移完成后，进入 Admin 后台确认代理配置已生效即可。

<br>

## 反向代理示例（Nginx）

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

        # 流式响应必备
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 600s;
        proxy_send_timeout 600s;
    }
}
```

完成反代后，记得在 Admin 后台把 `app.app_url` 改为 `https://your.domain.com`。

<br>

## WebUI

| 页面 | 路径 |
| :-- | :-- |
| Admin 登录页 | `/admin/login` |
| 账号管理 | `/admin/account` |
| 配置管理 | `/admin/config` |
| 缓存管理 | `/admin/cache` |
| WebUI 登录页 | `/webui/login` |
| Web Chat | `/webui/chat` |
| Masonry | `/webui/masonry` |
| ChatKit | `/webui/chatkit` |

### 鉴权规则

| 范围 | 配置项 | 规则 |
| :-- | :-- | :-- |
| `/v1/*` | `app.api_key` | 为空则不额外鉴权 |
| `/admin/*` | `app.app_key` | 默认值 `grok2api` |
| `/webui/*` | `app.webui_enabled`, `app.webui_key` | 默认关闭；`webui_key` 为空则不额外校验 |

<br>

## 账号管理

### 账号类型

| 类型 | 说明 | 适用模型 |
| :-- | :-- | :-- |
| **付费账号** | x.ai 官方付费账号 | 所有 `grok-4.20-*`、`grok-4.3-beta` |
| **免费账号** | 通过 `console.x.ai` 访问的免费账号 | 所有 `*-console` 模型 |

### 免费账号配置

使用免费账号需要提供 SSO Token 与 CF Clearance：

1. 浏览器打开开发者工具（F12）
2. 访问 `https://console.x.ai/`
3. 在 Network 中找到任意请求，查看 Cookie：
   - 复制 `sso` 值
   - 复制 `cf_clearance` 值
4. 在 Admin 后台 → 账号管理 → 添加账号，将上述值填入对应字段

> SSO Token 与 CF Clearance 属于敏感凭证，请勿写入代码或提交到版本库。

<br>

## 环境变量

启动期变量（`.env` / Compose / `docker run -e`）：

| 变量名 | 说明 | 默认值 |
| :-- | :-- | :-- |
| `TZ` | 时区 | `Asia/Shanghai` |
| `LOG_LEVEL` | 日志级别 | `INFO` |
| `LOG_FILE_ENABLED` | 写入本地文件日志 | `true` |
| `ACCOUNT_SYNC_INTERVAL` | 账号目录增量同步间隔（秒） | `30` |
| `ACCOUNT_SYNC_ACTIVE_INTERVAL` | 检测到变化后的活跃同步间隔（秒） | `3` |
| `SERVER_HOST` | 监听地址 | `0.0.0.0` |
| `SERVER_PORT` | 监听端口 | `8000` |
| `SERVER_WORKERS` | Granian worker 数量 | `1` |
| `HOST_PORT` | Compose 宿主机映射端口 | `8000` |
| `DATA_DIR` | 本地数据根目录 | `./data` |
| `LOG_DIR` | 本地日志目录 | `./logs` |
| `ACCOUNT_STORAGE` | 账号存储后端：`local` / `redis` / `mysql` / `postgresql` | `local` |
| `ACCOUNT_LOCAL_PATH` | `local` 模式 SQLite 路径 | `${DATA_DIR}/accounts.db` |
| `ACCOUNT_REDIS_URL` | `redis` 模式 DSN | `""` |
| `ACCOUNT_MYSQL_URL` | `mysql` 模式 DSN | `""` |
| `ACCOUNT_POSTGRESQL_URL` | `postgresql` 模式 DSN | `""` |
| `ACCOUNT_SQL_POOL_SIZE` | SQL 连接池核心连接数 | `5` |
| `ACCOUNT_SQL_MAX_OVERFLOW` | SQL 连接池最大溢出 | `10` |
| `ACCOUNT_SQL_POOL_TIMEOUT` | 等待空闲连接超时（秒） | `30` |
| `ACCOUNT_SQL_POOL_RECYCLE` | 连接最大复用时间（秒） | `1800` |
| `CONFIG_LOCAL_PATH` | 运行时配置文件路径 | `${DATA_DIR}/config.toml` |
| `GROK_STATSIG_SIGNER_URL` | Statsig 签名服务地址（防封版默认自动配置） | `http://statsig-signer:3000/sign` |

运行时配置也支持 `GROK_` 前缀环境变量覆盖，例如 `GROK_APP_API_KEY` 覆盖 `app.api_key`，`GROK_FEATURES_STREAM` 覆盖 `features.stream`。

### Statsig 签名配置

Grok 近期加强了反爬机制，新增对 `x-statsig-id` 请求头的严格校验。本项目通过 `statsig-signer` 无头浏览器签名服务解决此问题。

`config.toml` 中的 `[statsig]` 配置段：

| 配置项 | 说明 | 默认值 |
| :-- | :-- | :-- |
| `statsig.signer_url` | 签名服务地址；留空则回退到内置假值 | `""` |
| `statsig.timeout` | 调用签名服务超时（秒） | `5` |
| `statsig.cache_ttl` | 客户端签名缓存 TTL（秒），按 path+method 缓存 | `20` |
| `statsig.fail_cooldown` | 签名失败后冷却期（秒），期间直接走假值 | `5` |

> 防封版部署时签名服务自动启动，`GROK_STATSIG_SIGNER_URL` 环境变量已默认指向 `http://statsig-signer:3000/sign`，无需手动配置。标准版部署如需启用，自行搭建 statsig-signer 容器并配置 `statsig.signer_url`。

<br>

## 模型支持

> 通过 `GET /v1/models` 获取当前启用的模型列表。

### Chat（付费账号）

| 模型名 | mode | tier |
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
| `grok-4.20-fast` | `fast` | `basic`，优先使用高等级账号池 |
| `grok-4.20-auto` | `auto` | `super`，优先使用高等级账号池 |
| `grok-4.20-expert` | `expert` | `super`，优先使用高等级账号池 |
| `grok-4.20-heavy` | `heavy` | `heavy` |
| `grok-4.3-beta` | `grok-420-computer-use-sa` | `super` |

### Chat（console.x.ai 免费账号）

通过 `console.x.ai` 路由，使用 SSO Token 免费访问，不消耗付费账号额度。

| 模型名 | reasoning effort | 说明 |
| :-- | :-- | :-- |
| `grok-4.3-console` | 用户传入（默认 medium） | 免费账号 |
| `grok-4.3-low` | low（固定） | 免费账号 |
| `grok-4.3-medium` | medium（固定） | 免费账号 |
| `grok-4.3-high` | high（固定） | 免费账号 |
| `grok-4.20-0309-console` | 默认 | 免费账号 |
| `grok-4.20-0309-reasoning-console` | 固定 reasoning | 免费账号 |
| `grok-4.20-0309-non-reasoning-console` | 无 reasoning | 免费账号 |
| `grok-4.20-multi-agent-console` | 用户传入（默认 medium） | 免费账号，多智能体，agent 数量由 effort 决定 |
| `grok-4.20-multi-agent-low` | low（固定）→ 4 agents | 免费账号，多智能体 |
| `grok-4.20-multi-agent-medium` | medium（固定）→ 4 agents | 免费账号，多智能体 |
| `grok-4.20-multi-agent-high` | high（固定）→ 16 agents | 免费账号，多智能体 |
| `grok-4.20-multi-agent-xhigh` | xhigh（固定）→ 16 agents | 免费账号，多智能体 |
| `grok-build-console` | 默认 | 免费账号，Grok Build 0.1 |

> multi-agent 模型：`low`/`medium` 使用 4 个 agent（快速研究），`high`/`xhigh` 使用 16 个 agent（深度研究）。

### Console 模型配额

| 配额类型 | 次数 | 窗口 | 说明 |
| :-- | :-- | :-- | :-- |
| C（Console） | 30 次 | 15 分钟 | 所有 `*-console` / `*-low` / `*-medium` / `*-high` / `*-xhigh` 模型共享 |

<sub>以上数值基于简单压测得出（单账号约 40-50 次/5 分钟触发服务端限制），设为 30 次/15 分钟留有余量，避免触发上游真实 429。实际限制可能随 xAI 策略调整而变化。</sub>

### Image / Image Edit / Video

| 模型名 | mode | tier |
| :-- | :-- | :-- |
| `grok-imagine-image-lite` | `fast` | `basic` |
| `grok-imagine-image` | `auto` | `super` |
| `grok-imagine-image-pro` | `auto` | `super` |
| `grok-imagine-image-edit` | `auto` | `super` |
| `grok-imagine-video` | `auto` | `super` |

<br>

## API 一览

| 接口 | 鉴权 | 说明 |
| :-- | :-- | :-- |
| `GET /v1/models` | 是 | 列出当前启用模型 |
| `GET /v1/models/{model_id}` | 是 | 获取单个模型信息 |
| `POST /v1/chat/completions` | 是 | 对话 / 图像 / 视频统一入口 |
| `POST /v1/responses` | 是 | OpenAI Responses API 兼容子集 |
| `POST /v1/messages` | 是 | Anthropic Messages API 兼容接口 |
| `POST /v1/images/generations` | 是 | 独立图像生成接口 |
| `POST /v1/images/edits` | 是 | 独立图像编辑接口 |
| `POST /v1/videos` | 是 | 异步视频任务创建 |
| `GET /v1/videos/{video_id}` | 是 | 查询视频任务 |
| `GET /v1/videos/{video_id}/content` | 是 | 获取最终视频文件 |
| `GET /v1/files/video?id=...` | 否 | 获取本地缓存视频 |
| `GET /v1/files/image?id=...` | 否 | 获取本地缓存图片 |

<br>

## 调用示例

### 付费账号对话

```bash
curl http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $GROK2API_API_KEY" \
  -d '{
    "model": "grok-4.20-auto",
    "stream": true,
    "reasoning_effort": "high",
    "messages": [
      {"role":"user","content":"你好"}
    ]
  }'
```

### 免费账号对话

```bash
curl http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $GROK2API_API_KEY" \
  -d '{
    "model": "grok-4.3-high-console",
    "stream": true,
    "messages": [
      {"role":"user","content":"你好"}
    ]
  }'
```

### 图像生成

```bash
curl http://localhost:8000/v1/images/generations \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $GROK2API_API_KEY" \
  -d '{
    "model": "grok-imagine-image",
    "prompt": "一只在太空漂浮的猫",
    "n": 1,
    "size": "1792x1024",
    "response_format": "url"
  }'
```

### 视频生成

```bash
curl http://localhost:8000/v1/videos \
  -H "Authorization: Bearer $GROK2API_API_KEY" \
  -F "model=grok-imagine-video" \
  -F "prompt=霓虹雨夜街头，电影感慢镜头追拍" \
  -F "seconds=10" \
  -F "size=1792x1024" \
  -F "resolution_name=720p" \
  -F "preset=normal"
```

更完整的字段说明见上游 [接口文档](https://github.com/chenyme/grok2api#api-%E4%B8%80%E8%A7%88)。

<br>

## 常见问题

**Q: 镜像启动后 `/admin/login` 打不开？**
确认容器端口映射正确：`docker compose ps` 查看 `0.0.0.0:8000->8000/tcp`，且宿主机防火墙未拦截。

**Q: 图片或视频链接返回 403？**
没有正确设置 `app.app_url`。该字段必须是用户能访问的公网地址（含协议），例如 `https://api.example.com`。

**Q: 提示 Cloudflare 拦截？**
在 Admin 后台 → 配置管理 → 代理配置，将 `proxy.clearance.mode` 设为 `manual` 并填入有效 `cf_cookies` + `user_agent`，或部署 FlareSolverr 后切到 `flaresolverr` 模式。

**Q: 多 worker 部署？**
`SERVER_WORKERS` 大于 1 时，账号刷新调度器会通过文件锁选举出唯一 leader，其他 worker 仅做轻量同步，安全可用。Windows 下建议保持单 worker。

**Q: API 返回 403 且日志无 Cloudflare 拦截？**
Grok 新增了 `x-statsig-id` 反爬校验。确保 `statsig-signer` 容器正常运行（`docker ps | grep statsig`），首次启动需 60-90s 加载页面和注入签名。可在签名机日志中确认 `signer ready` 字样（`docker logs statsig-signer`）。

<br>

## 致谢

- 上游：[chenyme/grok2api](https://github.com/chenyme/grok2api)
- statsig-signer 签名方案：[duanzhenyu/grok2api](https://github.com/duanzhenyu/grok2api)
- DeepWiki：[chenyme/grok2api](https://deepwiki.com/chenyme/grok2api)
- 项目文档：[blog.cheny.me](https://blog.cheny.me/blog/posts/grok2api)
- 社区：[Linux.do](https://linux.do)

<br>

## Star History

[![Star History Chart](https://api.star-history.com/svg?repos=jiujiu532/grok2api&type=Date)](https://star-history.com/#jiujiu532/grok2api&Date)

<br>

## License

MIT
