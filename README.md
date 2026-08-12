# DingTalk Codex Bot

[![CI](https://github.com/ke-huang-cn/dingtalk-codex-bot/actions/workflows/ci.yml/badge.svg)](https://github.com/ke-huang-cn/dingtalk-codex-bot/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![DingTalk Stream](https://img.shields.io/badge/DingTalk-Stream-1677FF)](https://github.com/open-dingtalk/dingtalk-stream-sdk-python)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

一个轻量、可自部署的钉钉 AI 工作助手。项目直接使用钉钉官方 Stream SDK 连接机器人，不需要公网回调地址，也不依赖 LangBot 等付费连接服务。

> 项目名中的 Codex 表示面向工作任务的智能助手。当前版本调用 OpenAI 兼容 API，不连接或依赖 Codex 桌面客户端。

## 项目定位

| 分类 | 说明 |
| --- | --- |
| 使用场景 | 工作问答、会议纪要整理、邮件草拟、文档总结、方案分析 |
| 消息平台 | 钉钉企业内部应用机器人，Stream 模式双向通信 |
| AI 接口 | OpenAI Chat Completions 兼容接口，可配置模型与网关地址 |
| 部署方式 | Docker Compose，数据保存在本地 SQLite |
| 安全边界 | 可配置用户白名单、命令前缀、输入长度、超时和单会话并发限制 |
| 项目范围 | 纯文本工作助手，不提供终端执行、文件操作或系统管理能力 |

## 核心功能

- 使用钉钉官方 `dingtalk-stream` SDK，网络异常时自动重连。
- 支持 OpenAI 及兼容 Chat Completions API。
- 按钉钉会话保存上下文，支持 `/reset` 或“重置会话”。
- 使用 SQLite 持久化聊天记录，容器升级不会丢失数据。
- 支持钉钉 Staff ID 白名单和可选命令前缀。
- 同一会话只允许一个请求执行，避免重复消耗和回复乱序。
- 提供 Docker Compose、环境变量示例和 GitHub Actions 检查。

## 工作流程

```text
钉钉用户
   │
   ▼
钉钉 Stream 长连接
   │
   ▼
DingTalk Codex Bot
   ├── 用户白名单 / 前缀检查
   ├── SQLite 会话上下文
   └── 并发、长度与超时限制
   │
   ▼
OpenAI 兼容 API
   │
   ▼
钉钉文本回复
```

## 前置条件

- Docker Desktop 或支持 Docker Compose 的 Linux 主机。
- 一个钉钉组织及企业内部应用。
- 已为应用添加机器人能力，并将消息接收模式设置为 **Stream 模式**。
- OpenAI API Key，或其他兼容 Chat Completions 的服务凭据。

## 快速开始

### 1. 获取项目

```bash
git clone https://github.com/ke-huang-cn/dingtalk-codex-bot.git
cd dingtalk-codex-bot
```

### 2. 配置环境变量

```bash
cp .env.example .env
```

编辑 `.env`，至少填写：

```dotenv
DINGTALK_CLIENT_ID=你的应用ClientID
DINGTALK_CLIENT_SECRET=你的应用ClientSecret
OPENAI_API_KEY=你的APIKey
```

### 3. 启动机器人

```bash
docker compose up -d --build
docker compose logs -f bot
```

停止服务：

```bash
docker compose down
```

## 配置参考

| 变量 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `DINGTALK_CLIENT_ID` | 是 | 无 | 钉钉应用 Client ID，也称 AppKey |
| `DINGTALK_CLIENT_SECRET` | 是 | 无 | 钉钉应用 Client Secret，也称 AppSecret |
| `OPENAI_API_KEY` | 是 | 无 | OpenAI 或兼容服务的 API Key |
| `OPENAI_BASE_URL` | 否 | `https://api.openai.com/v1` | OpenAI 兼容 API 根地址 |
| `OPENAI_MODEL` | 否 | `gpt-5-mini` | 调用的模型名称 |
| `SYSTEM_PROMPT` | 否 | 内置工作助手提示词 | 机器人的系统提示词 |
| `COMMAND_PREFIX` | 否 | 空 | 仅响应指定前缀，例如 `/工作` |
| `ALLOWED_USERS` | 否 | 空 | 允许使用的 Staff ID，多个值用逗号分隔 |
| `MAX_INPUT_CHARS` | 否 | `6000` | 单条用户消息最大字符数 |
| `MAX_HISTORY_MESSAGES` | 否 | `12` | 每次请求携带的历史消息数量 |
| `REQUEST_TIMEOUT_SECONDS` | 否 | `120` | AI 请求总超时时间 |
| `DB_PATH` | 否 | `/app/data/bot.db` | 容器内 SQLite 数据库路径 |
| `LOG_LEVEL` | 否 | `INFO` | 日志级别 |

## 使用方式

如果 `COMMAND_PREFIX` 留空，机器人会处理收到的所有文本消息：

```text
请把下面的会议记录整理成待办事项……
```

如果配置 `COMMAND_PREFIX=/工作`：

```text
/工作 请帮我草拟一封客户跟进邮件
```

重置当前会话上下文：

```text
/reset
```

或：

```text
重置会话
```

## 数据与隐私

- 聊天记录保存在宿主机的 `data/bot.db`。
- 用户消息和必要的会话历史会发送到所配置的 AI 服务商。
- `.env`、数据库和 Python 缓存已加入 `.gitignore`。
- 正式部署建议配置 `ALLOWED_USERS`，并使用专用、低权限 API Key。
- 请勿在 Issue、日志或提交记录中公开任何密钥。

## 项目结构

```text
.
├── app.py                 # Stream 消息处理、AI 请求与 SQLite 会话
├── Dockerfile             # 运行镜像
├── docker-compose.yml     # 容器编排与数据卷
├── requirements.txt       # Python 依赖
├── .env.example           # 环境变量模板
├── .github/workflows      # GitHub Actions
├── CONTRIBUTING.md        # 贡献说明
├── SECURITY.md            # 安全漏洞报告方式
└── LICENSE                # MIT License
```

## 开发与检查

```bash
python -m py_compile app.py
docker compose build
```

提交 Pull Request 前，请确保没有提交 `.env`、数据库、访问令牌或应用密钥。

## 路线图

- [ ] 钉钉互动卡片和流式状态反馈
- [ ] 群聊与单聊独立触发策略
- [ ] 管理员命令与会话统计
- [ ] 可选的消息脱敏和保留周期
- [ ] 更多 OpenAI 兼容响应格式

## 贡献与安全

开发流程见 [CONTRIBUTING.md](CONTRIBUTING.md)。安全问题请不要创建公开 Issue，报告方式见 [SECURITY.md](SECURITY.md)。

## License

[MIT](LICENSE)
