# Contributing

感谢你参与改进 DingTalk Codex Bot。

## 开发流程

1. Fork 仓库并创建独立分支。
2. 保持改动聚焦，避免同时提交无关重构。
3. 执行基础检查：

```bash
python -m py_compile app.py
docker compose build
```

4. 确认提交中不包含 `.env`、API Key、钉钉密钥或本地数据库。
5. 创建 Pull Request，说明问题、实现方式和验证结果。

## 代码约定

- 支持 Python 3.12。
- 优先使用标准库和已有依赖。
- 新增配置必须同步更新 `.env.example` 和 README 配置表。
- 错误日志不得输出访问令牌或完整敏感消息。

## Issue

普通缺陷和功能建议可以使用 GitHub Issue。安全漏洞请遵循 [SECURITY.md](SECURITY.md)，不要公开披露。
