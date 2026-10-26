"""同步引擎：分块拉取 + 游标续传 + 单 key 失败隔离 + 原始归档 + 幂等入库。

三档同步（设计文档 §2.2）：
  T0 哨兵   —— 1 次轻量请求判断云端有没有新数据
  T1 增量   —— 拉最近 N 天所有 key
  T2 修正   —— 重拉最近 N 天（小米会回溯修正，尤其睡眠分期）
  T3 全量   —— 从起点日期按 chunk 分块回填
"""

from __future__ import annotations

import logging
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any

from ..cloud import parsers
from ..cloud.client import (
    MiAuthError,
    MiHealthClient,
    MiRateLimitError,
    RequestBudget,
)
from ..cloud.endpoints import SYNC_KEYS
from ..config import Config
from ..store.repository import Repository, now_ts
from .raw_archive import RawArchive
from ..auth.store import CredentialStore, Credentials

logger = logging.getLogger(__name__)

CN_TZ = timezone(timedelta(hours=8))


@dataclass
class SyncResult:
    tier: str
    ok_keys: list[str] = field(default_factory=list)
    failed_keys: dict[str, str] = field(default_factory=dict)
    rows_written: int = 0
    request_count: int = 0
    skipped: bool = False
    note: str | None = None

    @property
    def status(self) -> str:
        if self.skipped:
            return "skipped"
        if not self.failed_keys:
            return "success"
        return "partial" if self.ok_keys else "failed"


class SyncEngine:
    def __init__(
        self,
        config: Config,
        repo: Repository,
        client: MiHealthClient,
        archive: RawArchive,
        credentials: CredentialStore | None = None,
    ) -> None:
        self.config = config
        self.repo = repo
        self.client = client
        self.archive = archive
        self.credentials = credentials

    def persist_rotated_token(self) -> bool:
        """小米登录会轮换 passToken，变了就写回，否则下次可能用失效的旧值。"""
        if self.credentials is None:
            return False
        if self.client.pass_token == self.config.pass_token and self.client.user_id == self.config.user_id:
            return False
        try:
            current = self.credentials.load()
            self.credentials.save(
                Credentials(
                    user_id=self.client.user_id,
                    pass_token=self.client.pass_token,
                    region=self.config.region,
                    device_id=current.device_id if current else "",
                    c_user_id=current.c_user_id if current else "",
                )
            )
            self.config.pass_token = self.client.pass_token
            self.config.user_id = self.client.user_id
            return True
        except Exception as exc:  # noqa: BLE001 - 写回失败不该中断同步
            logger.warning("轮换后的凭据写回失败: %s", type(exc).__name__)
            return False

    # ------------------------------------------------------------ 工具
    def _today(self) -> date:
        return datetime.now(tz=CN_TZ).date()

    def _chunks(self, start: date, end: date, size: int) -> list[tuple[date, date]]:
        if size <= 0:
            size = 7
        result: list[tuple[date, date]] = []
        cursor = start
        while cursor <= end:
            chunk_end = min(cursor + timedelta(days=size - 1), end)
            result.append((cursor, chunk_end))
            cursor = chunk_end + timedelta(days=1)
        return result

    # ------------------------------------------------------------ T0 哨兵
    def sentinel(self) -> int | None:
        """只发 1 次请求，返回云端 steps 的最新时间戳（无数据则 None）。"""
        return self.client.probe_latest_steps(lookback_days=1)

    # ------------------------------------------------------------ 主流程
    def run(
        self,
        tier: str,
        *,
        start_date: str | None = None,
        end_date: str | None = None,
        keys: list[str] | None = None,
    ) -> SyncResult:
        """执行一次同步。

        `keys` 用于只重拉某几个数据源（例如解析器修好后只补 sleep），
        传 None 表示全部。分层修复时很有用——不然改一个解析器就得全量重跑一遍。
        """
        result = SyncResult(tier=tier)
        today = self._today()

        if tier == "T1":
            days = self.config.sync.recent_days
            window_start = today - timedelta(days=days)
        elif tier == "T2":
            days = self.config.sync.correct_days
            window_start = today - timedelta(days=days)
        elif tier == "T3":
            window_start = (
                date.fromisoformat(start_date)
                if start_date
                else date.fromisoformat(self.config.sync.fetch_start_date)
            )
        else:
            raise ValueError(f"未知同步档位: {tier}")

        window_end = date.fromisoformat(end_date) if end_date else today
        if window_start > window_end:
            result.skipped = True
            result.note = "起始日期晚于结束日期，已跳过"
            return result

        only = {k.strip() for k in keys if k.strip()} if keys else None
        # steps 与 calories 一起聚合成每日活动；workouts 走独立接口
        want_activity = only is None or bool({"steps", "calories"} & only)
        want_workouts = only is None or "workouts" in only
        pending_keys = [
            key
            for key in SYNC_KEYS
            if key not in ("steps", "calories") and (only is None or key in only)
        ]
        if only is not None and not (want_activity or want_workouts or pending_keys):
            result.skipped = True
            result.note = f"没有匹配的 key：{sorted(only)}"
            return result

        run_id = self.repo.start_sync_run(tier)
        try:
            self.client.login()
            self.persist_rotated_token()
        except (MiAuthError, MiRateLimitError) as exc:
            self.repo.finish_sync_run(run_id, [], {"__auth__": str(exc)}, 0)
            raise

        for chunk_start, chunk_end in self._chunks(
            window_start, window_end, self.config.sync.chunk_days
        ):
            if want_activity:
                self._sync_activity(chunk_start, chunk_end, result)
            for key in pending_keys:
                if key in result.failed_keys:
                    continue  # 本档内已失败过，不再对每个 chunk 重试
                self._sync_key(key, chunk_start, chunk_end, result)
            if want_workouts:
                self._sync_workouts(chunk_start, chunk_end, result)
            if result.failed_keys.get("__rate_limit__"):
                break

        self.repo.finish_sync_run(
            run_id, result.ok_keys, result.failed_keys, self.client.budget.count, result.note
        )

        # 同步完成后自动生成当日 AI 解读（缓存到库，前端/接口直接读）。
        # 只在启用且正常完成时触发；失败吞掉，绝不影响同步本身。
        if self.config.ai.enabled and self.config.ai.auto_after_sync:
            self._auto_generate_summary()

        return result

    def _auto_generate_summary(self) -> None:
        """同步后自动生成「今天」的 AI 解读，仅一次、失败仅记日志。"""
        try:
            from ..ai import AIClient, AIError, PROMPT_VERSION
            from ..analysis.daily import build_daily_payload
            from ..ai.cache import cache_matches, input_fingerprint

            day = self._today().isoformat()
            payload = build_daily_payload(self.repo, day)
            fingerprint = input_fingerprint(payload)
            cached = self.repo.get_ai_summary(day)
            if cached and cache_matches(cached, self.config.ai, fingerprint):
                return
            analysis = AIClient(self.config.ai).generate_daily_analysis(payload)
            self.repo.upsert_ai_summary(
                day,
                analysis,
                self.config.ai.model,
                PROMPT_VERSION,
                self.config.ai.fingerprint(),
                fingerprint,
            )
            logger.info("已自动生成 %s 的 AI 解读", day)
        except AIError as exc:
            logger.warning("自动生成 AI 解读失败（已忽略，不影响同步）：%s", exc)
        except Exception as exc:  # noqa: BLE001 - 兜底，AI 故障不应拖垮同步
            logger.warning("自动生成 AI 解读异常（已忽略）：%s", type(exc).__name__, exc)

    # ------------------------------------------------------------ 单 key 同步
    def _sync_activity(self, start: date, end: date, result: SyncResult) -> None:
        """steps + calories 聚合成每日活动。"""
        key = "steps"
        try:
            steps_records = self.client.fetch_key(key, start.isoformat(), end.isoformat())
            self._archive(key, start, end, steps_records)
            try:
                calorie_records = self.client.fetch_key(
                    "calories", start.isoformat(), end.isoformat()
                )
                self._archive("calories", start, end, calorie_records)
            except MiRateLimitError:
                raise
            except Exception as exc:  # noqa: BLE001 - 日活动必须是完整快照
                # daily_activity 把 steps 与 calories 聚合在同一行；如果 calories 子源失败仍发布，
                # 会用 steps payload 里的近似活动消耗覆盖既有准确值。整块判失败，保留旧数据。
                raise RuntimeError(f"calories 子源失败，未发布残缺日活动：{exc}") from exc

            rows = parsers.parse_daily_activity(steps_records, calorie_records)
            result.rows_written += self.repo.upsert_daily_activity(rows)
            for row in rows:
                self.repo.refresh_coverage(row["date"], "steps")
            self._mark_ok(key, steps_records, result)
        except MiRateLimitError:
            result.failed_keys["__rate_limit__"] = "触发限流"
            result.failed_keys[key] = "触发限流"
            raise
        except MiAuthError:
            raise
        except Exception as exc:  # noqa: BLE001 - 单 key 失败隔离
            self._mark_fail(key, exc, result)

    def _sync_workouts(self, start: date, end: date, result: SyncResult) -> None:
        """运动记录走独立接口（get_sport_records_by_time），不在 SYNC_KEYS 里。

        这个接口容易被漏掉——漏了的话 workout 表永远是 0，而其它指标看起来都正常。
        """
        key = "workouts"
        if key in result.failed_keys:
            return
        try:
            records = self.client.fetch_sport_records(start.isoformat(), end.isoformat())
            self._archive(
                key,
                start,
                end,
                records,
                endpoint="/app/v1/data/get_sport_records_by_time",
            )
            rows = parsers.parse_workouts(records)
            result.rows_written += self.repo.upsert_workouts(rows)
            self._mark_ok(key, records, result)
        except MiRateLimitError:
            result.failed_keys["__rate_limit__"] = "触发限流"
            result.failed_keys[key] = "触发限流"
            raise
        except MiAuthError:
            raise
        except Exception as exc:  # noqa: BLE001 - 单 key 失败隔离
            self._mark_fail(key, exc, result)

    def _sync_key(self, key: str, start: date, end: date, result: SyncResult) -> None:
        try:
            records = self.client.fetch_key(key, start.isoformat(), end.isoformat())
            self._archive(key, start, end, records)
            written = self._persist(key, records)
            result.rows_written += written
            self._mark_ok(key, records, result)
        except MiRateLimitError:
            result.failed_keys["__rate_limit__"] = "触发限流"
            result.failed_keys[key] = "触发限流"
            raise
        except MiAuthError:
            raise
        except Exception as exc:  # noqa: BLE001 - 单 key 失败隔离
            self._mark_fail(key, exc, result)

    def _persist(self, key: str, records: list[dict[str, Any]]) -> int:
        if key == "heart_rate":
            rows = parsers.parse_heart_rate(records)
            written = self.repo.upsert_heart_rate(rows)
            self._refresh_sample_coverage("heart_rate", rows)
            return written
        if key == "resting_heart_rate":
            return self.repo.upsert_resting_hr(parsers.parse_resting_hr(records))
        if key == "sleep":
            rows = parsers.parse_sleep(records)
            written = self.repo.upsert_sleep(rows)
            for row in rows:
                self.repo.refresh_coverage(row["date"], "sleep")
            return written
        if key == "spo2":
            rows = parsers.parse_spo2(records)
            written = self.repo.upsert_spo2(rows)
            self._refresh_sample_coverage("spo2", rows)
            return written
        if key == "stress":
            rows = parsers.parse_stress(records)
            written = self.repo.upsert_stress(rows)
            self._refresh_sample_coverage("stress", rows)
            return written
        if key == "weight":
            return self.repo.upsert_weight(parsers.parse_weight(records))
        if key == "abnormal_heart_beat":
            return self.repo.upsert_anomaly_events(parsers.parse_abnormal_hr(records))
        logger.debug("无解析器的 key，已跳过入库: %s", key)
        return 0

    def _refresh_sample_coverage(self, key: str, rows: list[dict[str, Any]]) -> None:
        """采样类数据按「出现的日期」刷新覆盖度（看板的缺数据日历依赖它）。"""
        days = {
            datetime.fromtimestamp(int(row["ts"]), tz=CN_TZ).strftime("%Y-%m-%d")
            for row in rows
            if row.get("ts")
        }
        for day in sorted(days):
            self.repo.refresh_coverage(day, key)

    # ------------------------------------------------------------ 归档与状态
    def _archive(
        self,
        key: str,
        start: date,
        end: date,
        records: list[dict[str, Any]],
        endpoint: str = "/app/v1/data/get_fitness_data_by_time",
    ) -> None:
        try:
            start_ts, end_ts = self.client.date_range_to_timestamps(
                start.isoformat(), end.isoformat()
            )
            rel_path, digest, count = self.archive.save(key, start_ts, end_ts, records, endpoint=endpoint)
            self.repo.record_raw(key, start_ts, end_ts, rel_path, digest, count)
        except Exception as exc:  # noqa: BLE001 - 归档失败不能阻塞入库
            logger.warning("原始归档失败 key=%s: %s", key, type(exc).__name__)

    def _mark_ok(self, key: str, records: list[dict[str, Any]], result: SyncResult) -> None:
        stamps = [int(item.get("time", 0) or 0) for item in records]
        cursor = max(stamps) if stamps else None
        self.repo.mark_sync_success(key, cursor)
        if key not in result.ok_keys:
            result.ok_keys.append(key)

    def _mark_fail(self, key: str, exc: Exception, result: SyncResult) -> None:
        message = f"{type(exc).__name__}: {exc}"
        self.repo.mark_sync_failure(key, message)
        result.failed_keys[key] = message
        logger.warning("key 同步失败 key=%s err=%s", key, message[:200])

    # ------------------------------------------------------------ 工作流
    def run_with_sentinel(self) -> SyncResult:
        """T0 探测有新数据才跑 T1，避免无谓请求。"""
        if not self.config.sync.sentinel_enabled:
            return self.run("T1")

        try:
            self.client.login()
            self.persist_rotated_token()
            latest = self.sentinel()
        except (MiAuthError, MiRateLimitError):
            raise

        if latest is None:
            logger.info("哨兵：云端最近 24h 无 steps 数据，跳过本次增量")
            return SyncResult(tier="T0", skipped=True, note="云端无新数据")

        state = self.repo.get_sync_state("steps")
        known = int((state or {}).get("cursor_ts") or 0)
        if latest <= known:
            logger.info("哨兵：云端数据未推进（latest=%s known=%s），跳过", latest, known)
            return SyncResult(tier="T0", skipped=True, note="云端数据未推进")

        logger.info("哨兵：检测到新数据，触发增量同步")
        return self.run("T1")

    def backup_database(self) -> str | None:
        """每日备份：VACUUM INTO，不需要停服。"""
        try:
            stamp = datetime.now(tz=CN_TZ).strftime("%Y%m%d-%H%M%S-%f")
            target = self.config.paths.backup / f"health-{stamp}.db"
            self.repo.vacuum_into(target)
            target.chmod(0o600)
            with closing(sqlite3.connect(target.resolve().as_uri() + "?mode=ro", uri=True)) as backup:
                check = [row[0] for row in backup.execute("PRAGMA integrity_check")]
            if check != ["ok"]:
                logger.error("备份文件完整性校验失败；保留现有备份")
                return None
            self._prune_backups()
            return str(target)
        except Exception as exc:  # noqa: BLE001
            logger.error("数据库备份失败: %s", type(exc).__name__)
            return None

    def _prune_backups(self, keep_daily: int = 14, keep_weekly: int = 8) -> None:
        # 每个有备份的日期保留最新一份，另保留更早的自然周各一份。
        # 无法识别的文件不删除；不要求恰好在周日成功备份。
        files = []
        for path in self.config.paths.backup.glob("health-*.db"):
            if path.is_symlink() or not path.is_file():
                continue
            try:
                stamp = path.stem.removeprefix("health-")
                fmt = "%Y%m%d-%H%M%S-%f" if stamp.count("-") == 2 else "%Y%m%d-%H%M%S"
                files.append((datetime.strptime(stamp, fmt), path))
            except ValueError:
                continue
        daily = set()
        weekly = set()
        keep = set()
        for stamp, path in sorted(files, reverse=True):
            day = stamp.date()
            if day not in daily and len(daily) < keep_daily:
                daily.add(day)
                keep.add(path)
            elif day not in daily:
                week = day.isocalendar()[:2]
                if week not in weekly and len(weekly) < keep_weekly:
                    weekly.add(week)
                    keep.add(path)
        for _, path in files:
            if path in keep:
                continue
            try:
                path.unlink()
            except OSError:
                pass


def build_engine(config: Config, repo: Repository) -> SyncEngine:
    budget = RequestBudget(
        limit=config.sync.max_requests_per_day,
        storage_path=config.paths.db.with_name("request-budget.sqlite3"),
        account=f"{config.region}:{config.user_id}",
    )
    client = MiHealthClient(
        user_id=config.user_id,
        pass_token=config.pass_token,
        region=config.region,
        interval_ms=config.sync.request_interval_ms,
        timeout_s=config.sync.http_timeout_s,
        retries=config.sync.request_retries,
        max_pages=config.sync.max_pages,
        budget=budget,
    )
    archive = RawArchive(config.paths.raw)
    return SyncEngine(config, repo, client, archive, credentials=CredentialStore())
