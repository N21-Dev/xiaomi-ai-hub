"""调度器：T0 每小时哨兵 / T1 每日增量 / T2 每周修正 / 每日备份。

用进程内 APScheduler（BlockingScheduler），不依赖额外 cron 容器。

两个关键约束：
1. **每个任务单独开 SQLite 连接**。APScheduler 在线程池里执行任务，而 sqlite3 的连接
   默认不允许跨线程使用，在主线程建好连接再拿到任务线程里用会抛
   `ProgrammingError: SQLite objects created in a thread can only be used in that same thread`。
   （这个坑实际踩过：哨兵任务每次都在失败，而日志里只看到一句 ProgrammingError。）
2. 所有任务共用一把互斥锁：同步任务重叠时后到的直接跳过，**不排队堆积**
   （堆积会在接口故障时打出请求风暴）。
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from .cloud.client import MiAuthError, MiRateLimitError
from .config import Config
from .ai.cache import cache_matches, input_fingerprint
from .store.repository import Repository, connect, init_db
from .sync.engine import SyncEngine, build_engine

logger = logging.getLogger(__name__)

CN_TZ = timezone(timedelta(hours=8))

_sync_lock = threading.Lock()


class Scheduler:
    def __init__(self, config: Config) -> None:
        self.config = config
        config.paths.ensure()
        self.scheduler = BlockingScheduler(timezone=config.schedule.timezone)

    # ------------------------------------------------------------ 连接
    def open_repo(self) -> tuple[sqlite3.Connection, Repository]:
        """为**当前线程**开一个 SQLite 连接（见模块文档第 1 条）。"""
        self.config.paths.ensure()
        conn = connect(self.config.paths.db)
        init_db(conn)
        return conn, Repository(conn)

    # ------------------------------------------------------------ 任务包装
    def _run_locked(self, label: str, job: Callable[[Repository], None]) -> None:
        if not _sync_lock.acquire(blocking=False):
            logger.info("跳过 %s：已有同步任务在运行", label)
            return
        started = time.monotonic()
        conn: sqlite3.Connection | None = None
        try:
            conn, repo = self.open_repo()
            job(repo)
            logger.info("任务完成 %s 用时 %.1fs", label, time.monotonic() - started)
        except MiAuthError as exc:
            # 凭据失效必须人工介入，日志要说清楚
            logger.error(
                "凭据失效，同步已停止；请打开健康看板的「账号」窗口重新登录：%s",
                exc,
            )
        except MiRateLimitError as exc:
            logger.warning("触发限流，本次同步中止：%s", exc)
        except Exception as exc:  # noqa: BLE001 - 调度线程不能因单次失败退出
            logger.exception("任务失败 %s: %s", label, type(exc).__name__)
        finally:
            if conn is not None:
                conn.close()
            _sync_lock.release()

    def _with_engine(self, repo: Repository, action: Callable[[SyncEngine], None]) -> None:
        """建引擎 → 执行 → 保证关闭 HTTP 客户端。"""
        engine = build_engine(self.config, repo)
        try:
            action(engine)
        finally:
            engine.client.close()

    # ------------------------------------------------------------ 任务
    def job_sentinel(self) -> None:
        """T0：有新数据才触发 T1。"""

        def job(repo: Repository) -> None:
            def action(engine: SyncEngine) -> None:
                result = engine.run_with_sentinel()
                logger.info(
                    "哨兵结果 tier=%s status=%s note=%s rows=%d",
                    result.tier,
                    result.status,
                    result.note,
                    result.rows_written,
                )
                if result.rows_written:
                    self._backup(engine)

            self._with_engine(repo, action)

        self._run_locked("T0 哨兵", job)

    def job_incremental(self) -> None:
        """T1：每日强制增量，兜住哨兵漏掉的情况。"""

        def job(repo: Repository) -> None:
            def action(engine: SyncEngine) -> None:
                result = engine.run("T1")
                logger.info(
                    "增量同步 status=%s ok=%s failed=%s rows=%d",
                    result.status,
                    result.ok_keys,
                    list(result.failed_keys),
                    result.rows_written,
                )
                self._backup(engine)

            self._with_engine(repo, action)

        self._run_locked("T1 增量", job)

    def job_correction(self) -> None:
        """T2：每周重拉，覆盖小米对历史数据的回溯修正（睡眠分期尤其明显）。"""

        def job(repo: Repository) -> None:
            def action(engine: SyncEngine) -> None:
                result = engine.run("T2")
                logger.info("修正同步 status=%s rows=%d", result.status, result.rows_written)

            self._with_engine(repo, action)

        self._run_locked("T2 修正", job)

    # ------------------------------------------------------------ AI 报告
    def _generate_period_report(self, repo: Repository, period_type: str) -> None:
        """生成周报/月报（已有缓存则跳过；AI 失败自动重试，最多 5 次）。"""
        from .ai import AIClient, AIError, PROMPT_VERSION
        from .analysis.report import build_report_payload, resolve_period_range

        label = "周报" if period_type == "weekly" else "月报"
        if not self.config.ai.auto_period_reports:
            logger.info("自动周期报告已关闭，跳过%s生成", label)
            return
        if not self.config.ai.configured():
            logger.info("AI 未配置，跳过%s生成", label)
            return

        client = AIClient(self.config.ai)
        today = datetime.now(tz=CN_TZ).date()
        start, end = resolve_period_range(period_type, today)
        payload = build_report_payload(repo, period_type, start, end)
        if not self.config.ai.include_health_notes:
            payload["health_notes"] = []
        fingerprint = input_fingerprint(payload)
        cached = repo.get_ai_report(period_type, start.isoformat(), end.isoformat())
        if (
            cached
            and cache_matches(cached["report"], self.config.ai, fingerprint)
        ):
            logger.info("%s 已存在且配置未变（%s ~ %s），跳过", label, start, end)
            return

        if not (
            payload["sleep"]["days_with_data"] or payload["activity"]["days_with_data"]
        ):
            logger.warning("周期 %s ~ %s 没有任何数据，跳过%s", start, end, label)
            return

        last_err: Exception | None = None
        for attempt in range(1, 6):
            try:
                report = client.generate_period_report(
                    payload, period_type, start.isoformat(), end.isoformat()
                )
                repo.upsert_ai_report(
                    period_type, start.isoformat(), end.isoformat(),
                    report,
                    self.config.ai.model,
                    PROMPT_VERSION,
                    self.config.ai.fingerprint(),
                    fingerprint,
                )
                logger.info("%s 已生成（%s ~ %s）", label, start, end)
                return
            except AIError as exc:
                last_err = exc
                logger.warning("%s 生成第 %d 次失败：%s", label, attempt, exc)
                time.sleep(30)
        logger.error("%s 生成最终失败：%s", label, last_err)

    def job_weekly_report(self) -> None:
        """每周一早上生成上周健康周报。"""

        def job(repo: Repository) -> None:
            self._generate_period_report(repo, "weekly")

        self._run_locked("周报生成", job)

    def job_monthly_report(self) -> None:
        """每月 1 日生成上个自然月的健康月报。"""

        def job(repo: Repository) -> None:
            self._generate_period_report(repo, "monthly")

        self._run_locked("月报生成", job)

    @staticmethod
    def _backup(engine: SyncEngine) -> None:
        path = engine.backup_database()
        if path:
            logger.info("数据库已备份: %s", path)

    # ------------------------------------------------------------ 启动
    def start(self) -> None:
        sched = self.config.schedule
        hour, minute = _parse_hhmm(sched.incremental_time)
        corr_hour, corr_minute = _parse_hhmm(sched.correction_time)

        if self.config.sync.sentinel_enabled:
            self.scheduler.add_job(
                self.job_sentinel,
                IntervalTrigger(hours=sched.sentinel_interval_hours),
                id="t0_sentinel",
                max_instances=1,
                coalesce=True,
                next_run_time=datetime.now(tz=CN_TZ) + timedelta(seconds=30),
            )

        self.scheduler.add_job(
            self.job_incremental,
            CronTrigger(hour=hour, minute=minute),
            id="t1_incremental",
            max_instances=1,
            coalesce=True,
        )
        self.scheduler.add_job(
            self.job_correction,
            CronTrigger(day_of_week="sun", hour=corr_hour, minute=corr_minute),
            id="t2_correction",
            max_instances=1,
            coalesce=True,
        )
        self.scheduler.add_job(
            self.job_weekly_report,
            CronTrigger(day_of_week="mon", hour=5, minute=15),
            id="ai_weekly_report",
            max_instances=1,
            coalesce=True,
        )
        self.scheduler.add_job(
            self.job_monthly_report,
            CronTrigger(day=1, hour=5, minute=45),
            id="ai_monthly_report",
            max_instances=1,
            coalesce=True,
        )

        logger.info(
            "调度器启动：哨兵每 %dh / 增量每日 %s / 修正每周日 %s / "
            "周报周一 05:15 / 月报每月 1 日 05:45（时区 %s）",
            sched.sentinel_interval_hours,
            sched.incremental_time,
            sched.correction_time,
            sched.timezone,
        )
        try:
            self.scheduler.start()
        except (KeyboardInterrupt, SystemExit):
            logger.info("调度器退出")


def _parse_hhmm(value: str) -> tuple[int, int]:
    try:
        hour, minute = value.split(":", 1)
        return int(hour), int(minute)
    except (ValueError, AttributeError):
        logger.warning("时间格式无法解析 %r，回退到 04:30", value)
        return 4, 30
