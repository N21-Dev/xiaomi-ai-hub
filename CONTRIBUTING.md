# 贡献指南

欢迎通过 Issue 描述问题或提出功能需求，再提交范围清楚的 Pull Request。请说明触发条件、期望/实际行为、相关版本及验证命令；界面修改附仅含模拟数据的截图。

使用 Python 标准风格和描述性名称，保持现有文件缩进。把功能修改与大范围格式调整分开。新增权限、持久化、网关转换和设备操作应覆盖正常与失败路径；不能用真实设备完成自动化测试。

请勿提交账号、API Key、Cookie、数据库、设备 ID、真实健康记录或 APK 签名文件。模板使用占位值，测试使用明确的模拟值。运行 `python3 ops/audit_public.py` 与相关测试，流程见 [开发说明](docs/DEVELOPMENT.md)。

安全漏洞不要公开放入 Issue，请使用 [私密安全报告](https://github.com/N21-Dev/xiaomi-ai-hub/security/advisories/new)。提交贡献即表示你有权按本项目相应许可提供这些内容；第三方代码必须保留来源和许可说明。
