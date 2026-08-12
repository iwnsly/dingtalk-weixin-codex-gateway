# Security Policy

## Supported Versions

安全修复以默认分支的最新版本为准。

## Reporting a Vulnerability

请通过 GitHub 仓库的 Private vulnerability reporting 功能报告安全问题，不要创建公开 Issue。

报告中请包含：

- 受影响的版本或提交；
- 可复现步骤；
- 潜在影响；
- 建议的修复方式（如有）。

## Secret Handling

如果 API Key、钉钉 Client Secret 或访问令牌曾出现在聊天、日志、提交或 Issue 中，应立即在对应平台撤销并重新生成。仅从 Git 历史删除字符串并不能使已泄露凭据恢复安全。
