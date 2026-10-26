# 配置说明

`.env` 控制 Compose，`config/bridge.env` 控制中枢，`config/health.env` 控制可选健康服务。模板没有默认账号、模型 Key、固定设备 ID 或私人服务器地址。

| 设置 | 默认/用途 |
|---|---|
| BIND_ADDRESS | `127.0.0.1`，显式修改后才开放局域网 |
| PANEL_PORT / HEALTH_PORT | `18765` / `8321` |
| PUID / PGID | 初始化时按当前用户生成 |
| PYTHON_IMAGE | 官方 `python:3.11-slim-bookworm`，可显式换为可信镜像站 |
| PIP_INDEX_URL | `https://pypi.org/simple`，可改为自己信任的索引 |
| PANEL_AUTH_REQUIRED | `true`，管理员登录 |
| AI_GATEWAY_AUTH_REQUIRED | `true`，网关令牌校验 |
| AI_BASE_URL / AI_API_KEY / AI_MODEL | 可选，模型也可在登录后的面板配置 |
| AI_MAX_TOKENS / AI_PANEL_MAX_TOKENS | 语音/文字独立预算，实际受上游限制 |
| MI_USER_ID / MI_PASS_TOKEN | 可选，首次启动不要求；可在连接设置授权 |
| MI_HARDWARE | 留空，按实际设备选择，不预设音箱型号 |
| HEALTH_AI_TOOLS_ENABLED | `false`，只读健康工具需明确开启 |
| WEATHER_TOOLS_ENABLED | `false`，公开天气模型与地点解析 |
| LOCATION_DEFAULT_QUERY | 留空，不推断家庭位置 |
| WEB_SEARCH_TOOLS_ENABLED | `false`，按需连接自己的 SearXNG |
| WEB_SEARCH_BASE_URL | 留空，填写受控 SearXNG 服务地址 |
| LOG_CONTENT | `false`，不记录完整对话 |

健康联接（启用 health profile 后）：

```ini
HEALTH_SERVICE_URL=http://mi-health-hub:8321
HEALTH_WEB_URL=http://localhost:8321
HEALTH_API_KEY_FILE=/run/secrets/health-api-key
HEALTH_AI_TOOLS_ENABLED=false
```

远程部署时把 HEALTH_WEB_URL 改为浏览器可以访问的实际入口；Compose 服务名只供容器间通信。健康服务 API Key 和账号不能与中枢凭据合并。

天气使用 Open-Meteo 固定公开端点；它把城市名称解析为坐标，不读取手机 GPS。SearXNG 应由你控制并开启 JSON 格式。外部搜索与天气服务会收到对应的查询参数。

设备控制和自动化只使用已授权的目录能力。红外回执表示已发送指令，不保证家电实际状态；不要把上次红外指令当成设备回读。

音箱当前只支持 `MI_REGION=cn`。健康统计中的日界线固定为北京时间；`TZ` 可以调整日志和调度时区，但不会把现有统计口径切换到其他地区。
