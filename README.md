<div align="center">

<img alt="Grok2API" src="https://github.com/user-attachments/assets/037a0a6e-7986-41cc-b4af-04df612ee886" />

<h1>Grok Web 能力的 OpenAI 兼容网关</h1>

<h3>多账号池、智能选号、自动维护</h3>

<p>
将 grok.com 与 console.x.ai 的聊天、图像、视频能力，<br>
以 <strong>OpenAI / Anthropic 兼容 API</strong> 统一对外提供。
</p>

<p>
<a href="https://www.python.org/"><img alt="Python" src="https://img.shields.io/badge/python-3.13%2B-3776AB?logo=python&logoColor=white"></a>
<a href="https://fastapi.tiangolo.com/"><img alt="FastAPI" src="https://img.shields.io/badge/FastAPI-0.119%2B-009688?logo=fastapi&logoColor=white"></a>
<a href="https://github.com/jiujiu532/grok2api/pkgs/container/grok2api"><img alt="Docker" src="https://img.shields.io/badge/ghcr.io-jiujiu532%2Fgrok2api-2496ED?logo=docker&logoColor=white"></a>
<a href="LICENSE"><img alt="License" src="https://img.shields.io/badge/license-MIT-16a34a"></a>
</p>

<p>
<a href="#核心特性">核心特性</a> ·
<a href="#部署指南">部署指南</a> ·
<a href="#模型列表">模型列表</a> ·
<a href="#账号配置">账号配置</a> ·
<a href="#api-端点">API 端点</a> ·
<a href="#常见问题">常见问题</a>
</p>

</div>

> [!NOTE]
> 本项目仅供学习与研究交流。请务必遵守 Grok 的使用条款及当地法律法规，不得用于非法用途。

本仓库基于上游 [chenyme/grok2api](https://github.com/chenyme/grok2api) 二次开发，新增多账号池管理、Console 免费模型、配额轮换、防封部署等能力。欢迎 PR 和 Fork，二开请保留原作者与前端标识。

---

## 核心特性

| 能力 | 说明 |
| :-- | :-- |
| OpenAI 兼容 | `/v1/chat/completions`、`/v1/responses`、`/v1/images/generations`、`/v1/videos` |
| Anthropic 兼容 | `/v1/messages`（Claude SDK 直接对接） |
| 多账号池 | basic / super / heavy 三级池，自动负载均衡与配额同步 |
| 免费账号 | 支持 `console.x.ai` SSO Token，`*-console` 模型零成本使用 |
| 媒体生成 | 文生图、图像编辑、文生视频、图生视频，本地缓存与代理链接 |
| 防封内置 | `x-statsig-id` 兼容修复，WARP + FlareSolverr 一键部署 |
| 管理后台 | Admin 配置、账号管理、Web Chat、Masonry 画廊、ChatKit 语音 |

---

## 部署指南

本项目提供两种部署方式：

| 方式 | 说明 | 适用场景 |
| :-- | :-- | :-- |
| **标准版** | 仅 grok2api，直连 Grok | IP 干净、无 Cloudflare 拦截 |
| **防封版** | grok2api + WARP + Privoxy + FlareSolverr | IP 被 Cloudflare 拦截、需要稳定访问 |

> [!TIP]
> 当前版本已内置 403 兼容修复，标准版可直接验证。仍遇 403 时再切防封版。

---

### 标准版部署

**Docker Compose（推荐）：**

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

**Docker 单容器：**

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

---

### 防封版部署

> **前置要求**：服务器需支持 `NET_ADMIN` + `SYS_MODULE` 权限（KVM/XEN 虚拟化均支持，OpenVZ/LXC 不支持）。

```bash
git clone https://github.com/jiujiu532/grok2api
cd grok2api/grok2api-main/grok2api-main
docker compose -f docker-compose.warp.yml up -d
```

防封版自动启动以下服务：

| 服务 | 说明 |
| :-- | :-- |
| `warp-proxy` | Cloudflare WARP 出口代理，提供干净 IP |
| `privoxy` | HTTP 代理，将流量转发到 WARP |
| `flaresolverr` | 自动解 Cloudflare 挑战，获取 cf_clearance |
| `init-config` | 初始化容器，自动写入代理配置 |
| `grok2api` | 主服务 |

启动后代理配置已自动完成，进入 Admin 后台添加账号即可使用。

---

<details>
<summary><strong>升级 / 回滚 / 卸载 / 迁移</strong></summary>

### 升级

无论标准版还是防封版，升级时只需更新 `grok2api` 主镜像，防封组件不需要更新。

**标准版升级：**

```bash
docker pull ghcr.io/jiujiu532/grok2api:latest
docker compose up -d --no-deps grok2api
```

**防封版升级（只更新主服务，不动 WARP/FlareSolverr）：**

```bash
docker pull ghcr.io/jiujiu532/grok2api:latest
docker compose -f docker-compose.warp.yml up -d --no-deps grok2api
```

> `--no-deps` 确保只重启 grok2api，WARP/Privoxy/FlareSolverr 继续运行不中断。
> 
> `./data/` 中的配置（`config.toml`）和数据库（`accounts.db`）挂载在 volume 中，升级不会覆盖。

### 回滚

```bash
# 查看可用版本：https://github.com/jiujiu532/grok2api/pkgs/container/grok2api
docker pull ghcr.io/jiujiu532/grok2api:<tag>

# 标准版回滚
docker compose up -d --no-deps grok2api

# 防封版回滚
docker compose -f docker-compose.warp.yml up -d --no-deps grok2api
```

### 卸载

**标准版卸载：**

```bash
cd grok2api/grok2api-main/grok2api-main
docker compose down
# 如需删除数据（不可恢复）：
rm -rf ./data ./logs
```

**防封版卸载：**

```bash
cd grok2api/grok2api-main/grok2api-main
docker compose -f docker-compose.warp.yml down
# 如需删除数据（不可恢复）：
rm -rf ./data ./logs
```

### 从标准版迁移到防封版

数据完全保留，无需重新配置：

```bash
# 停止标准版
docker compose down

# 用防封版启动（自动检测已有配置，不覆盖）
docker compose -f docker-compose.warp.yml up -d
```

</details>

---

### 本地源码部署

前置：Python 3.13+、[uv](https://docs.astral.sh/uv/getting-started/installation/)

```bash
git clone https://github.com/jiujiu532/grok2api
cd grok2api/grok2api-main/grok2api-main
cp .env.example .env && uv sync
uv run granian --interface asgi --host 0.0.0.0 --port 8000 --workers 1 app.main:app
```

---

### 首次启动

访问 `http://localhost:8000/admin/login`，默认密码 `grok2api`，进入后设置：

1. `app.app_key` — Admin 密码
2. `app.api_key` — API 鉴权密钥（留空不鉴权）
3. `app.app_url` — 公网地址（图片/视频链接需要）

> 配置保存即时生效，无需重启。

---

## 模型列表

### Chat（ grok.com）

basic表示free账号，spuer和heavy 为付费

| 模型名 | mode | 账号等级 | 备注 |
| :-- | :-- | :-- | :-- |
| `grok-4.20-fast` / `grok-4.2-fast` / `grok-4.3-fast` | fast | basic（优先高等级） |
| `grok-4.20-auto` | auto | super | 
| `grok-4.20-expert` | expert | super | 
| `grok-4.20-heavy` | heavy | heavy | |
| `grok-4.3-beta` | grok-420-computer-use-sa | super | 
| `grok-4.20-multi-agent-0309` | heavy | heavy |
| `grok-4.20-0309-non-reasoning` | fast | basic |
| `grok-4.20-0309` | auto | super |
| `grok-4.20-0309-reasoning` | expert | super |
| `grok-4.20-0309-non-reasoning-super` | fast | super |
| `grok-4.20-0309-super` | auto | super |
| `grok-4.20-0309-reasoning-super` | expert | super |
| `grok-4.20-0309-non-reasoning-heavy` | fast | heavy |
| `grok-4.20-0309-heavy` | auto | heavy |
| `grok-4.20-0309-reasoning-heavy` | expert | heavy |

### Chat（ console.x.ai）

通过 SSO Token 免费访问，不消耗付费额度。所有免费模型使用 **basic** 等级账号。

| 模型名 | reasoning effort | 账号等级 |
| :-- | :-- | :-- |
| `grok-4.3-console` | 用户传入（默认 medium） | basic |
| `grok-4.3-low` | low（固定） | basic |
| `grok-4.3-medium` | medium（固定） | basic |
| `grok-4.3-high` | high（固定） | basic |
| `grok-4.20-0309-console` | 默认 | basic |
| `grok-4.20-0309-reasoning-console` | 固定 reasoning | basic |
| `grok-4.20-0309-non-reasoning-console` | 无 reasoning | basic |
| `grok-4.20-multi-agent-console` | 用户传入（默认 medium） | basic|
| `grok-4.20-multi-agent-low` | low（固定）→ 4 agents | basic|
| `grok-4.20-multi-agent-medium` | medium（固定）→ 4 agents | basic|
| `grok-4.20-multi-agent-high` | high（固定）→ 16 agents | basic |
| `grok-4.20-multi-agent-xhigh` | xhigh（固定）→ 16 agents | basic|
| `grok-build-console` | 默认 | basic |

**Console 配额**：30 次 / 15 分钟窗口，采用延迟恢复轮换策略（消耗至剩余 15 次时启动计时器，评分机制自动轮换到其他账号）。后台每 30 秒巡检并自动重置过期配额。

### Image / Video（ grok.com）

| 模型名 | 能力 | 账号等级 |
| :-- | :-- | :-- |
| `grok-imagine-image-lite` | 文生图 | basic |
| `grok-imagine-image` / `image-pro` | 文生图 | super |
| `grok-imagine-image-edit` | 图像编辑 | super |
| `grok-imagine-video` | 文生视频 | super |

---



## 账号配置

| 类型 | 等级 | 适用模型 |
| :-- | :-- | :-- |
| 付费账号（x.ai 官方） | super / heavy | `grok-4.20-*`、`grok-4.3-beta`、`grok-4.3-fast` |
| 免费账号（console.x.ai SSO） | basic | 所有 `*-console` / `*-low` / `*-medium` / `*-high` / `*-xhigh` |

**免费账号获取方式**：

1. 浏览器 F12 打开开发者工具
2. 访问 `https://console.x.ai/`
3. Network 面板找任意请求，Cookie 中复制 `sso` 值
4. Admin 后台 → 账号管理 → 添加账号，粘贴 token

> SSO Token 属于敏感凭证，请勿写入代码或提交到版本库。

---

## API 端点

| 端点 | 说明 |
| :-- | :-- |
| `GET /v1/models` | 列出可用模型 |
| `POST /v1/chat/completions` | 聊天 / 图像 / 视频统一入口 |
| `POST /v1/responses` | OpenAI Responses API |
| `POST /v1/messages` | Anthropic Messages API |
| `POST /v1/messages/count_tokens` | Anthropic / Claude Code Token 计数 |
| `POST /v1/images/generations` | 图像生成 |
| `POST /v1/images/edits` | 图像编辑 |
| `POST /v1/videos` | 异步视频任务 |
| `GET /v1/videos/{id}` / `{id}/content` | 查询 / 下载视频 |

---

## 环境变量

| 变量名 | 说明 | 默认值 |
| :-- | :-- | :-- |
| `TZ` | 时区 | `Asia/Shanghai` |
| `LOG_LEVEL` | 日志级别 | `INFO` |
| `LOG_FILE_ENABLED` | 写入本地文件日志 | `true` |
| `SERVER_HOST` | 监听地址 | `0.0.0.0` |
| `SERVER_PORT` | 监听端口 | `8000` |
| `SERVER_WORKERS` | Granian worker 数量 | `1` |
| `HOST_PORT` | Compose 宿主机映射端口 | `8000` |
| `DATA_DIR` | 本地数据根目录 | `./data` |
| `LOG_DIR` | 本地日志目录 | `./logs` |
| `ACCOUNT_STORAGE` | 存储后端：`local` / `redis` / `mysql` / `postgresql` | `local` |
| `ACCOUNT_SYNC_INTERVAL` | 增量同步间隔（秒） | `30` |
| `ACCOUNT_SYNC_ACTIVE_INTERVAL` | 活跃同步间隔（秒） | `3` |
| `ACCOUNT_LOCAL_PATH` | SQLite 路径 | `${DATA_DIR}/accounts.db` |
| `ACCOUNT_REDIS_URL` | Redis DSN | `""` |
| `ACCOUNT_MYSQL_URL` | MySQL DSN | `""` |
| `ACCOUNT_POSTGRESQL_URL` | PostgreSQL DSN | `""` |
| `ACCOUNT_SQL_POOL_SIZE` | 连接池核心连接数 | `5` |
| `ACCOUNT_SQL_MAX_OVERFLOW` | 连接池最大溢出 | `10` |
| `ACCOUNT_SQL_POOL_TIMEOUT` | 等待空闲连接超时（秒） | `30` |
| `ACCOUNT_SQL_POOL_RECYCLE` | 连接最大复用时间（秒） | `1800` |

运行时配置支持 `GROK_` 前缀覆盖，如 `GROK_APP_API_KEY` 覆盖 `app.api_key`。

---

## 调用示例

```bash
# 付费账号对话
curl http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"grok-4.20-auto","stream":true,"messages":[{"role":"user","content":"你好"}]}'

# 免费账号对话
curl http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"grok-4.3-console","stream":true,"messages":[{"role":"user","content":"你好"}]}'
```

---

## 常见问题

| 问题 | 解决方案 |
| :-- | :-- |
| Admin 打不开 | 确认端口映射和防火墙：`docker compose ps` |
| 图片/视频链接 403 | 设置 `app.app_url` 为公网地址（含 `https://`） |
| Cloudflare 拦截 | 更换代理，或者切换防封版部署，再或者手动配置 `proxy.clearance.mode` |
| 多 Worker 冲突 | 无冲突，调度器通过文件锁选举 leader |

---

## 更新日志

### v0.1.7

**新功能**

- 🔌 **Console 原生工具调用支持**（[PR#24](https://github.com/jiujiu532/grok2api/pull/24)，感谢 @daoguademeng）
  - Console 模型支持 OpenAI 兼容的 `tools` / `tool_choice` 参数
  - 客户端 function tools（如 bash、read）可稳定产出 `tool_calls`
  - Grok 内置工具（web_search、x_search 等 19 个）保持内部语义，不泄露为客户端 tool_calls
  - 支持多轮 tool-call 上下文（assistant tool_calls + tool result 正确转换）
  - 新增 738 行回归测试覆盖核心逻辑

**优化**

- 🔧 **Console 配额参数调整**：恢复周期从 15 分钟改为 30 分钟，轮换阈值从 15 改为 20，降低单号负载
- 💓 **SSE 心跳保活**：所有流式接口在数据开始前发送心跳注释，防止思考期间连接超时
- ⚡ **TXT 导入异步化**：大批量导入不再阻塞，接口立即返回，刷新在后台进行
- 🔒 **依赖安全升级**：cryptography 48.0.1+、starlette 1.1.0+、python-multipart 0.0.31+

**修复**

- 🐛 修复 NSFW 初始化时生日已锁定的 429 报错（[PR#25](https://github.com/jiujiu532/grok2api/pull/25)，感谢 @Xaihi-nun）
- 🐛 修复批量刷新结果未区分异常与临时失败的问题

### v0.1.5 (2025-06-13)

**优化**

- 🔧 **批量刷新结果优化**：刷新账户时区分"凭证失效（异常）"和"临时失败（网络波动）"两种状态
  - 前端提示更清晰：`刷新完成：成功 906，异常 40，临时失败 54`
  - 后端性能优化：批量查询失败账户状态（从 N 次数据库查询降为 1 次）
  - 异常账户自动进入"异常"筛选组，临时失败不影响账户状态

- 🎯 **导入账户交互改进**（基于 [PR#13](https://github.com/jiujiu532/grok2api/pull/13)）
  - 新增/导入弹窗中加入"导入后自动开启 NSFW"复选框（默认不勾选）
  - 工具栏按钮互斥显示：勾选账号时显示批量操作按钮，未勾选显示全局按钮
  - 去除全局配置 `account.auto_nsfw_on_import`，改为每次导入时手动选择

- ⚙️ **并发数限制调整**
  - 批量操作硬限制从 50 调整为 80
  - 配置页并发数输入框添加 `min: 1, max: 80` 强制限制
  - 防止用户输入超出范围的并发值导致后端压力过大

**修复**

- 🐛 修复配置页数字输入框 `min/max` 属性未生效的问题
- 🐛 补充 `tokens.py` 缺失的 `Query` 导入（导致服务启动失败）
- 🐛 补充翻译文件中缺失的 `autoNsfwOnImport` 和 `autoNsfwHint` 键

---

## 致谢

- 上游：[chenyme/grok2api](https://github.com/chenyme/grok2api)
- DeepWiki：[chenyme/grok2api](https://deepwiki.com/chenyme/grok2api)
- 项目文档：[blog.cheny.me](https://blog.cheny.me/blog/posts/grok2api)
- 社区：[Linux.do](https://linux.do)

---

## Star History

[![Star History Chart](https://api.star-history.com/svg?repos=jiujiu532/grok2api&type=Date)](https://star-history.com/#jiujiu532/grok2api&Date)

---

<div align="center">

**MIT License**

</div>
