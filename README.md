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

至少设置本地 Codex Bridge Token。钉钉模式还需要填写：

```dotenv
DINGTALK_CLIENT_ID=你的钉钉应用 Client ID
DINGTALK_CLIENT_SECRET=你的钉钉应用 Client Secret
AI_BACKEND=codex
CODEX_BRIDGE_TOKEN=与宿主机 Bridge 相同的随机 Token
```

### 2. 启动

```bash
docker compose up -d --build
```

启动宿主机 Bridge（示例）：

```bash
CODEX_BRIDGE_TOKEN=你的随机Token \
python3 bridge.py
```

### 3. 打开控制台

访问 [http://127.0.0.1:8080](http://127.0.0.1:8080)，默认密码为 `12345`。登录后在“配置”选项卡选择钉钉或微信，也可以修改管理密码。

## 微信登录

选择微信并保存后，微信容器会直接访问腾讯 iLink API 获取二维码。二维码会出现在控制台“配置”页，手机扫码确认即可。

登录凭据保存在：

```text
data/weixin_token.json
```

微信消息通过 `getupdates` 长轮询接收，通过 `sendmessage` 回复。当前主要支持私聊文本消息。

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
| `CODEX_BIN` | Codex Desktop CLI 路径 | Bridge 使用的 Codex 可执行文件 |
| `CODEX_CWD` | 当前目录 | Codex 工作目录 |
| `ADMIN_PASSWORD` | `12345` | 控制台初始密码，可在界面修改 |
| `DB_PATH` | `/app/data/bot.db` | SQLite 路径 |

## 安全边界

- 管理控制台默认只绑定 `127.0.0.1:8080`。
- Bridge 默认只监听 `127.0.0.1`，必须使用 Bearer Token。
- Codex Bridge 使用只读沙箱，不提供终端、文件修改或系统管理能力。
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
