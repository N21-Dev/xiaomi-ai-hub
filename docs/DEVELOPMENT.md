# 开发与验证

Python 3.11+；前端检查使用 Node.js 22+。不要把生产数据、令牌或设备权限挂到测试环境。

## 容器测试

```sh
docker compose -f compose.dev.yaml build
docker compose -f compose.dev.yaml run --rm bridge-tests
docker compose -f compose.dev.yaml run --rm health-tests
```

两套测试禁网、只读挂载源码，使用临时可写目录。中枢协议测试中的官方 SDK 测试为可选项，未安装时会明确跳过。

初始化工具的独立测试：`python3 -m unittest discover -s tests -q`。它检查配置不被覆盖、密钥随机且不输出、私有文件权限及符号链接边界，使用临时目录。

本机开发时可分别建立虚拟环境，在对应服务目录安装 `pip install -e .`；健康服务安装 `pip install -e '.[dev]'`。运行中枢 `python -m unittest discover -s tests -q` 和健康 `python -m pytest -q`。

## 浏览器

在仓库外的工具目录安装 Playwright 和 `@axe-core/playwright`，再从仓库根运行：

```sh
npm install --prefix /path/to/browser-tools playwright@1.56.0 @axe-core/playwright@4.10.2
/path/to/browser-tools/node_modules/.bin/playwright install chromium
```

`/path/to/browser-tools` 是自己选择的仓库外目录。需要补齐系统依赖时，按 Playwright 的提示安装或使用其测试容器。

```sh
HUB_PLAYWRIGHT_MODULE=/path/to/node_modules/playwright \
HUB_AXE_MODULE=/path/to/node_modules/@axe-core/playwright \
node services/xiaomi-ai-bridge/tools/verify_panel_auth_browser.cjs

HUB_PLAYWRIGHT_MODULE=/path/to/node_modules/playwright \
node services/mi-health-hub/tools/verify_access_browser.cjs
```

该脚本使用模拟账号和请求拦截，不访问真实服务。其他 `verify_*_dom.cjs` 使用 jsdom；通过 HUB_JSDOM_MODULE 指定仓库外的依赖路径。修改顶部布局时同时检查中枢与健康页。

## 发布前

运行 `python3 ops/audit_public.py`，再用 Gitleaks 扫描工作树和提交历史。检查截图只有模拟数据、配置模板没有凭据、构建不依赖本机目录，确认登录、CSRF 和数据权限测试通过后再发布。测试输出不代表真实小米型号的兼容性保证。
