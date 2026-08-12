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
python3 bridge.py
```

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

### 媒体消息边界

- 微信和钉钉语音：优先使用平台回调提供的服务端转写文本；没有转写时会返回明确提示。
- 微信文件：支持通过 iLink CDN 下载到本地，并支持从 `CODEX_CWD` 内选择文件上传回微信。
- 微信和钉钉图片、视频：可以识别并确认收到，但尚未接入完整媒体下载、视觉识别和二进制回传。
- 音频没有服务端转写时仍会明确提示，不会静默丢弃。

文件支持接收和发送：

- 接收文件后，网关会通过微信 CDN 下载到 `data/wechat_files/`，并回复本地保存路径。
- 发送 `发送文件 /相对或绝对路径`，网关会从 `CODEX_CWD` 内读取文件并上传回微信。
- 单文件限制为 50 MB；发送路径必须位于 `CODEX_CWD` 内。
- 语音、图片和视频仍只确认收到，尚未接入完整媒体下载/发送。

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
| `CODEX_BIN` | Codex Desktop CLI 路径 | Bridge 使用的 Codex 可执行文件 |
| `CODEX_CWD` | 当前目录 | Codex 工作目录 |
| `CODEX_BRIDGE_HOST` | `127.0.0.1` | Bridge 监听地址，建议保持本机回环地址 |
| `CODEX_BRIDGE_PORT` | `8787` | Bridge 监听端口 |
| `CODEX_BRIDGE_TIMEOUT_SECONDS` | `180` | 单次 Codex 请求超时时间 |
| `PROGRESS_INTERVAL_SECONDS` | `30` | IM 端任务进度通知间隔，最小 10 秒 |
| `ADMIN_PASSWORD` | `12345` | 控制台初始密码，可在界面修改 |
| `DB_PATH` | `/app/data/bot.db` | SQLite 路径 |

## 安全边界

- 管理控制台默认只绑定 `127.0.0.1:8080`。
- Bridge 默认只监听 `127.0.0.1`，必须使用 Bearer Token。
- Codex Bridge 默认使用只读沙箱；完全权限是显式开关，打开后会允许命令执行和文件修改。
- 完全权限开关只应对可信用户开放；不要把管理端口或 Bridge 端口暴露到公网。
- `CODEX_CWD` 决定完全权限模式下 Codex 可操作的工作目录，请不要指向包含无关敏感资料的目录。
- 不要提交 `.env`、数据库、微信 Token、钉钉 Secret 或 GitHub Token。
- 曾经在聊天中暴露的密钥应立即撤销并重新生成。

## 开发检查

```bash
python3 -m py_compile app.py admin.py bridge.py weixin.py
docker compose config
docker compose build
```

## License

[MIT](LICENSE)
