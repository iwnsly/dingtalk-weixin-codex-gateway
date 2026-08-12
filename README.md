# DingTalk / Weixin Codex Gateway

一个可自部署的本地 Codex 消息网关：在钉钉和微信之间选择一个入口，把消息转发到宿主机上的 Codex CLI，并将回复发回原聊天平台。

[![CI](https://github.com/iwnsly/dingtalk-weixin-codex-gateway/actions/workflows/ci.yml/badge.svg)](https://github.com/iwnsly/dingtalk-weixin-codex-gateway/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Docker](https://img.shields.io/badge/Docker-Compose-2496ED?logo=docker&logoColor=white)](https://docs.docker.com/compose/)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

## 项目定位

`dingtalk-weixin-codex-gateway` 面向个人或小团队的工作场景，提供一个统一的管理界面和两种可选 IM 适配器：

- **钉钉**：使用官方 DingTalk Stream SDK 和企业内部应用凭据。
- **微信**：直接使用腾讯微信 iLink Bot API 二维码登录，不需要安装 OpenClaw CLI 或 Gateway。
- **本地 Codex**：宿主机运行受控 Bridge，调用 Codex CLI；Bridge 默认使用只读沙箱。
- **控制台**：配置入口、查看微信登录状态、修改管理密码和审计聊天记录。

两个 IM 入口共享同一个 Codex Bridge，但运行时只启用一个渠道，避免重复消费和会话混乱。

## 任务进度通知

微信和钉钉收到任务后会立即发送确认消息。若本地 Codex 在 `PROGRESS_INTERVAL_SECONDS` 秒内没有完成，网关会定期发送已处理时长，最终再发送结果或失败提示。

进度通知只表示任务状态，不会展示模型的内部推理过程，也不会写入会话历史。默认通知间隔为 30 秒，最小值为 10 秒。

## 架构

```text
钉钉 Stream ─┐
             ├─ dingtalk-weixin-codex-gateway ── 本地 Codex Bridge ── Codex CLI
微信 iLink ──┘                 │
                               └─ 管理控制台 / 聊天记录 / SQLite
```

Compose 默认启动三个职责明确的容器：

| 容器 | 作用 |
| --- | --- |
| `dingtalk-codex-bot-dingtalk` | 钉钉 Stream 适配器 |
| `dingtalk-codex-bot-weixin` | 微信 iLink 长轮询适配器 |
| `dingtalk-codex-bot-admin` | 配置和聊天记录控制台 |

三个服务共享 `data/`，但可以独立重启。`data/`、`.env` 和令牌均不会进入 Git。

## 快速开始

### 1. 配置环境

```bash
cp .env.example .env
```

至少设置本地 Codex Bridge Token。选择钉钉渠道时还需要填写：

```dotenv
DINGTALK_CLIENT_ID=你的钉钉应用 Client ID
DINGTALK_CLIENT_SECRET=你的钉钉应用 Client Secret
AI_BACKEND=codex
CODEX_BRIDGE_TOKEN=与宿主机 Bridge 相同的随机 Token
```

选择微信渠道时不需要填写钉钉凭据，登录控制台后扫码即可完成认证。

### 2. 启动

```bash
docker compose up -d --build
```

启动宿主机 Bridge（示例）：

```bash
CODEX_BRIDGE_TOKEN=你的随机Token \
CODEX_BRIDGE_HOST=0.0.0.0 \
python3 bridge.py
```

Compose 容器通过 `host.docker.internal` 访问宿主机，因此 Bridge 不能只绑定 `127.0.0.1`。Docker 部署时使用 `CODEX_BRIDGE_HOST=0.0.0.0`，并确保 `8787` 端口不向公网开放。Bridge 的聊天和状态接口都要求与容器一致的 Bearer Token。

默认 Bridge 使用 Codex 只读沙箱。登录管理控制台后，在“配置”页的“Codex 权限”中可以打开“完全权限”。

| 模式 | 能力 | 建议 |
| --- | --- | --- |
| 只读（默认） | 读取项目、分析代码、回答问题 | 日常使用 |
| 完全权限 | 修改文件、执行命令、访问 `CODEX_CWD` 下的宿主机资源 | 仅在可信的个人工作环境中临时开启 |

权限开关保存在 `data/runtime.json`，Bridge 会在每次请求时读取，关闭即可恢复只读模式。完全权限不会绕过 IM 网关鉴权，仍受 Bridge Token、管理员登录和渠道配置保护。

### 3. 打开控制台

访问 [http://127.0.0.1:8080](http://127.0.0.1:8080)，默认密码为 `12345`。登录后在“配置”选项卡选择钉钉或微信，也可以修改管理密码。

## 微信登录

选择微信并保存后，微信容器会直接访问腾讯 iLink API 获取二维码。二维码会出现在控制台“配置”页，手机扫码确认即可。

登录凭据保存在：

```text
data/weixin_token.json
```

微信消息通过 `getupdates` 长轮询接收，通过 `sendmessage` 回复。当前主要支持私聊文本消息；群聊、平台策略限制和账号权限以微信 iLink API 实际返回为准。

### 定时主动推送

微信适配器会读取 `data/scheduled_jobs.json`，支持按指定时区和时间主动发送每日内容。任务发送成功后会保存 `last_sent_date`，服务重启不会在同一天重复发送；生成或发送失败时每 30 秒重试。当前任务类型 `daily_fortune` 会通过本地 Codex Bridge 生成当日运势。

```json
[
  {
    "id": "daily-fortune",
    "type": "daily_fortune",
    "enabled": true,
    "session_id": "wechat:用户会话 ID",
    "timezone": "Asia/Shanghai",
    "time": "08:00",
    "start_date": "2026-08-13"
  }
]
```

### 媒体消息边界

- 微信和钉钉语音：优先使用平台回调提供的服务端转写文本；没有转写时会返回明确提示。
- 微信文件：支持通过 iLink CDN 下载到本地，并支持从 `CODEX_CWD` 内选择文件上传回微信。
- 微信图片：会通过微信 CDN 下载、解密并把本地路径传给 Codex；Codex 是否能进行视觉解析取决于当前模型能力。微信视频目前只确认收到，尚未接入完整媒体下载和解析。
- 音频没有服务端转写时仍会明确提示，不会静默丢弃。

文件支持接收和发送：

- 接收文件后，网关会通过微信 CDN 下载到 `data/wechat_files/`，并回复本地保存路径。
- 发送 `发送文件 /相对或绝对路径`，网关会从 `CODEX_CWD` 内读取文件并上传回微信。
- 单文件限制为 50 MB；发送路径必须位于 `CODEX_CWD` 内。
- 网关会校验解密后的文件大小和 MD5，并在 `data/wechat_media_keys.json` 中缓存已验证的 CDN 密文与密钥对应关系。
- 腾讯微信 iLink 当前存在 FILE 类型 CDN 去重缺陷：历史上传过的相同内容可能复用旧密文，却返回新的错误 AES 密钥，客户端无法解密。遇到明确的密钥不匹配提示时，需要改变文件内容后重发，例如压缩成 ZIP 并加入一个新的说明文件；只改文件名无效。参见 [Tencent/openclaw-weixin#193](https://github.com/Tencent/openclaw-weixin/issues/193)。
- 语音优先使用平台服务端转写；图片会下载、解密并把路径传给 Codex；视频目前仍只确认收到。

钉钉图片、文件和视频支持接收，文件和图片支持发送：

- 接收媒体后，网关通过钉钉 `downloadCode` 下载到 `data/dingtalk_files/`，并把本地路径追加到同一条 Codex 请求中。
- 发送 `发送文件 <路径>` 或 `发送图片 <路径>`，网关会从 `CODEX_CWD` 或 `data/dingtalk_files/` 内上传并回传到钉钉。
- 微信和钉钉接收文件的统一流程都是“下载、校验大小、保存、把路径传给 Codex”；单个媒体最大 50 MB。
- 钉钉媒体同样限制为单文件 50 MB，发送路径必须位于 `CODEX_CWD` 或 `data/dingtalk_files/` 内。

## 聊天记录

控制台的“聊天记录”选项卡支持：

- 微信 / 钉钉分开查看；
- 今天、昨天、本周、上周、本月、上月、本年度快捷筛选；
- 自定义开始时间和结束时间；
- 查看时间、渠道、角色、会话和消息内容。

记录保存在 `data/bot.db`，SQLite 表中的 `platform` 字段区分 `wechat` 和 `dingtalk`。

## 配置参考

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DINGTALK_CLIENT_ID` | 空 | 钉钉应用 Client ID |
| `DINGTALK_CLIENT_SECRET` | 空 | 钉钉应用 Client Secret |
| `AI_BACKEND` | `openai` | `codex` 或 `openai` |
| `CODEX_BRIDGE_URL` | `http://host.docker.internal:8787/v1/chat` | 本地 Bridge 地址 |
| `CODEX_BRIDGE_TOKEN` | 空 | Bridge Bearer Token |
| `CODEX_RUNTIME_CONFIG_PATH` | `./data/runtime.json` | 完全权限开关配置文件路径 |
| `CODEX_MEMORY_DIR` | `~/.codex/memories` | 本机 Codex 长期记忆目录；Bridge 会在每次微信/钉钉请求前读取其中的 Markdown 文件 |
| `CODEX_MEMORY_CONTEXT_CHARS` | `24000` | 注入单次 Codex 请求的长期记忆最大字符数 |
| `CODEX_BIN` | Codex Desktop CLI 路径 | Bridge 使用的 Codex 可执行文件 |
| `CODEX_CWD` | 当前目录 | Codex 工作目录 |
| `CODEX_BRIDGE_HOST` | `127.0.0.1` | Bridge 监听地址；Docker 容器调用宿主机 Bridge 时设为 `0.0.0.0` |
| `CODEX_BRIDGE_PORT` | `8787` | Bridge 监听端口 |
| `CODEX_BRIDGE_TIMEOUT_SECONDS` | `180` | 单次 Codex 请求超时时间 |
| `PROGRESS_INTERVAL_SECONDS` | `30` | IM 端任务进度通知间隔，最小 10 秒 |
| `ADMIN_PASSWORD` | `12345` | 控制台初始密码，可在界面修改 |
| `DB_PATH` | `/app/data/bot.db` | SQLite 路径 |

## 安全边界

- 管理控制台默认只绑定 `127.0.0.1:8080`。
- Bridge 默认只监听 `127.0.0.1`；Docker 部署需监听 `0.0.0.0`，但必须使用 Bearer Token，并通过系统防火墙避免将 `8787` 暴露到公网。
- Codex Bridge 默认使用只读沙箱；完全权限是显式开关，打开后会允许命令执行和文件修改。
- 完全权限开关只应对可信用户开放；不要把管理端口或 Bridge 端口暴露到公网。
- `CODEX_CWD` 决定完全权限模式下 Codex 可操作的工作目录，请不要指向包含无关敏感资料的目录。
- 不要提交 `.env`、数据库、微信 Token、钉钉 Secret 或 GitHub Token。
- 曾经在聊天中暴露的密钥应立即撤销并重新生成。

## 故障排查

### 容器无法连接 Bridge

如果微信或钉钉日志出现 `Cannot connect to host host.docker.internal:8787`：

1. 确认 Bridge 正在宿主机运行，并设置 `CODEX_BRIDGE_HOST=0.0.0.0`。
2. 从容器内请求 `http://host.docker.internal:8787/health`。
3. 确认 `.env` 中的 `CODEX_BRIDGE_TOKEN` 与启动 Bridge 时使用的 Token 一致。

### Codex 上游返回 502

Bridge 健康检查正常但任务仍失败，且日志包含 `502 Bad Gateway` 或 `Upstream request failed`，说明 Codex CLI 当前配置的模型 Provider 不可用或不兼容 Responses API。检查 `~/.codex/config.toml` 中的模型、`model_provider`、`base_url` 和 `wire_api`。这类故障不属于微信、钉钉或 Docker 连接问题。

## Codex 长期记忆

微信和钉钉请求经过同一个本地 Bridge。Bridge 默认读取宿主机 `~/.codex/memories` 下的 Markdown 记忆，并把完整内容注入每次临时 Codex 请求，因此两个渠道可以共享桌面 Codex 的长期记忆。由于记忆可能包含手机号、邮箱和履历等隐私信息，这些内容会随请求发送到 `~/.codex/config.toml` 配置的模型 Provider；启用前请确认 Provider 的数据处理策略。

Docker 部署时，必须把宿主机记忆目录以只读方式挂载给 Bridge，并设置 `CODEX_MEMORY_DIR`，例如：

```yaml
volumes:
  - ${CODEX_HOME:-$HOME/.codex}/memories:/codex-memories:ro
environment:
  CODEX_MEMORY_DIR: /codex-memories
```

如果没有挂载，Bridge 会明确记录“未读取到本机 Codex 长期记忆”，不会虚构记忆内容。

## 开发检查

```bash
python3 -m py_compile app.py admin.py bridge.py weixin.py
docker compose config
docker compose build
```

## License

[MIT](LICENSE)
