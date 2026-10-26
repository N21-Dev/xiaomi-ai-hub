"""容器入口：同一进程内同时跑调度器与 API。

- uvicorn 跑在后台线程（只读 API，负载极低）
- BlockingScheduler 跑在主线程（同步任务串行，天然限速）
- 收到 SIGTERM 时优雅退出（docker stop 会先发 SIGTERM）

这样保持**单容器**部署，与 NAS 上现有容器的管理方式一致；
代价是不能单独重启 API，但对个人自托管场景不是问题。
"""

from __future__ import annotations

import logging
import signal
import threading
import time

import uvicorn

from .api.app import create_app
from .auth.store import CredentialError, CredentialStore
from .config import Config, ConfigError, load_config
from .logging_setup import scrub, setup_logging
from .scheduler import Scheduler

logger = logging.getLogger(__name__)

_shutdown = threading.Event()


def _resolve_config() -> Config:
    """凭据优先取环境变量，其次取凭据文件（登录会轮换 token，需要能读回）。"""
    config = load_config()
    if not config.user_id or not config.pass_token:
        store = CredentialStore()
        creds = store.load()
        if creds:
            config.user_id = creds.user_id
            config.pass_token = creds.pass_token
            config.region = config.region or creds.region
    return config


def main() -> int:
    setup_logging()
    try:
        config = _resolve_config()
    except (ConfigError, CredentialError) as exc:
        logger.error("配置加载失败：%s", scrub(str(exc)))
        return 2

    # 凭据缺失**不退出**：否则配合 restart:unless-stopped 会变成重启循环刷日志，
    # 而且首次部署时用户还没来得及填。改为照常起服务，把问题暴露在日志与看板上。
    if not config.user_id or not config.pass_token:
        logger.error(
            "尚未配置小米账号凭据，同步任务会被跳过。"
            "请写入 %s（推荐，权限 600）或设置 MI_USER_ID / MI_PASS_TOKEN。"
            "获取方式：浏览器登录 account.xiaomi.com → F12 → Application → Cookies。",
            CredentialStore().path,
        )

    config.paths.ensure()

    def handle_signal(signum: int, _frame: object) -> None:
        logger.info("收到信号 %s，准备退出", signum)
        _shutdown.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, handle_signal)
        except (ValueError, OSError):
            pass

    # ---- API 线程 ----
    app = create_app(config)
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host=config.server.api_host,
            port=config.server.api_port,
            log_config=None,
            access_log=False,
        )
    )
    api_thread = threading.Thread(target=server.run, name="api", daemon=True)
    api_thread.start()
    logger.info("API 已启动 http://%s:%s", config.server.api_host, config.server.api_port)

    # ---- 调度器（主线程）----
    scheduler = Scheduler(config)
    sched_thread = threading.Thread(target=scheduler.start, name="scheduler", daemon=True)
    sched_thread.start()

    try:
        while not _shutdown.is_set():
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass

    logger.info("正在停止 API …")
    server.should_exit = True
    api_thread.join(timeout=10)
    logger.info("已退出")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
