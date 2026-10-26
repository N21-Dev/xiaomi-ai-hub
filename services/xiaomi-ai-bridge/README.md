# AI 中枢服务

自托管模型网关、米家设备工具、自动化任务与网页管理面板。默认需要管理员登录；无需小米账号也可先启动面板并配置模型。

从仓库根使用 README 的 Docker 初始化流程。源码入口为 `src/xiaomi_ai_bridge/main.py`，环境配置在 `config.py`；`panel_auth.py` 管理独立账号/会话，`health.py` 提供管理与协议接口。

`POST /v1/chat/completions`、`POST /v1/messages`、`POST /v1/responses` 提供兼容入口；`GET /v1/models` 返回已启用模型。协议转换的能力依赖上游，图片、工具、流式和思考参数仍需对应模型支持。登录后的模型面板管理上游、活动路由和模型设置。

单一助手在文字和音箱入口共用工具，设备操作要求对应权限。健康、天气和网页搜索按需开启，不根据公网 IP 推断家庭位置。红外发送回执不能当作家电状态回读。

本地安装：Python 3.11+，`pip install -e .`；`python -m unittest discover -s tests -q` 运行模拟后端测试。容器测试与浏览器检查见根目录[开发说明](../../docs/DEVELOPMENT.md)。

配置、账号初始化、外网与数据流见[部署](../../docs/DEPLOYMENT.md)、[配置](../../docs/CONFIGURATION.md)、[隐私](../../docs/PRIVACY.md)。
