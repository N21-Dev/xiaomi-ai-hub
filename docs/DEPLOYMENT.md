# 部署与账号

## 新安装

从仓库根运行 README 的 init、build、admin、start。初始化会创建权限为 0600 的本地配置和随机健康 API Key，不创建默认管理员，也不连接第三方账号。

没有小米账号或模型时也能打开登录页。管理员创建后登录面板，在模型中枢录入自己的上游地址、模型与 Key；模型管理和文字对话可独立使用。需要音箱或米家设备时再从连接设置登录自己的小米账号。

首次创建和忘记密码都使用：

```sh
python3 ops/setup.py admin
python3 ops/setup.py start
```

密码交互输入两次，至少 12 个字符。工具在重设账号后自动重启正在运行的中枢，使新密码和会话撤销立即生效；首次安装通过 start 启动。设备设置、模型配置和健康数据保留。不要删除数据库来重设密码。

`python3 ops/setup.py check` 仅校验 Compose。服务已运行时：

```sh
docker compose ps
curl --fail http://localhost:18765/healthz
```

未登录访问管理 API 返回 401 是预期行为。`/healthz` 仅为无凭据存活检查，服务就绪和小米连接状态请登录面板查看。

## NAS 与局域网

初始化会使用当前用户的 UID/GID；以 root 初始化时使用 10001。可在 `.env` 修改 PUID、PGID，但数据目录的文件所有者也需相符。不要用 `chmod -R 777` 解决权限问题。

只在需要时把 `.env` 的 `BIND_ADDRESS=127.0.0.1` 改成服务器的局域网地址或 `0.0.0.0`，并保留登录和网关 Key 校验。端口可通过 PANEL_PORT / HEALTH_PORT 修改。远程 NAS 也可以保留回环绑定并使用 SSH 隧道。

## 公网与 Cloudflare

用 HTTPS 反向代理连接中枢端口，保留 Host、转发原始协议。只把实际代理地址加入 `config/bridge.env` 的 `PANEL_TRUSTED_PROXY_CIDRS`，不要信任整个互联网。可信代理传入 HTTPS 时，会话 Cookie 使用 Secure。

面板会话与模型网关令牌是不同权限：内网 API 客户端使用网关自己的 Bearer Key；公网转发入口同时遵循面板的认证策略。不要关闭 `PANEL_AUTH_REQUIRED` 或 `AI_GATEWAY_AUTH_REQUIRED` 作为跨域问题的解决办法。

健康服务的账号、API Key 与数据库独立。中枢联接只读 API 不代表健康站点获得了面板登录保护；公开健康页面前需额外配置身份访问控制。

## 健康服务

新安装使用 `init --with-health`；已有安装可在 `.env` 增加 `COMPOSE_PROFILES=health`，并按 CONFIGURATION.md 设置中枢联接。健康 API Key 在 `config/health.env` 和 `config/health-api-key` 中生成，不输出到日志，也不写入仓库。

健康 AI 工具默认关闭，启用 `HEALTH_AI_TOOLS_ENABLED=true` 前应明确知道摘要会发给所选模型上游。小米健康凭据在健康服务自己的界面/文件中配置，不能与音箱票据共享。云同步的实际新鲜度取决于手机将手表数据上传到小米云的时机。

首次打开健康页面时，输入管理员从本地 `config/health-api-key` 取得的访问密钥。密钥仅在当前标签页会话保存，使用 `X-API-Key` 请求头发送；页面提供更改和清除按钮。不要把密钥放入网址、截图、工单或公开日志。

## 备份与更新

备份 `.env`、私有 config、`data/bridge` 和 `data/health`，加密保存到受控位置。运行中的 SQLite 使用应用一致性备份或 SQLite backup，不直接复制正在写入的数据库文件。

更新前记录旧镜像、保存配置和一致性数据备份，核对版本说明，构建后逐个更新相关服务。不要从新旧目录同时启动两套实例，不执行未经确认的 `docker compose down -v`。回退前必须确认旧代码兼容数据库结构。
