"""SQLite 仓储层：连接管理 + 全部 UPSERT 写入 + 查询封装。

所有写操作都是幂等的（`ON CONFLICT DO UPDATE`）。小米会回溯修正历史数据，
重复同步必须安全，且后到的数据要覆盖旧值。
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

SCHEMA_FILE = Path(__file__).with_name("schema.sql")


def now_ts() -> int:
    return int(time.time())


def connect(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


# `CREATE TABLE IF NOT EXISTS` 不会给**已存在**的表补列，升级时得手工 ALTER。
# 这里是「表名 → {列名: 列定义}」，init_db 会按需补齐。
_EXTRA_COLUMNS: dict[str, dict[str, str]] = {
    "sleep_session": {
        "avg_hr": "INTEGER",
        "min_hr": "INTEGER",
        "max_hr": "INTEGER",
        "avg_spo2": "REAL",
        "min_spo2": "INTEGER",
        "max_spo2": "INTEGER",
        "avg_breath": "REAL",
        "awake_cnt": "INTEGER NOT NULL DEFAULT 0",
        "has_stage": "INTEGER NOT NULL DEFAULT 0",
        "has_rem": "INTEGER NOT NULL DEFAULT 0",
        "alg_version": "INTEGER",
    },
    "ai_daily_summary": {
        "config_fingerprint": "TEXT",
        "input_fingerprint": "TEXT",
    },
    "workout": {
        "total_cal": "REAL",
        "min_hr": "INTEGER",
        "avg_pace_s": "REAL",
        "max_pace_s": "REAL",
        "pace_basis": "TEXT",
        "train_effect": "REAL",
        "anaerobic_train_effect": "REAL",
        "train_load": "REAL",
        "recover_time_h": "INTEGER",
        "vitality": "REAL",
        "zone_warmup_s": "INTEGER",
        "zone_fat_burn_s": "INTEGER",
        "zone_aerobic_s": "INTEGER",
        "zone_anaerobic_s": "INTEGER",
        "zone_extreme_s": "INTEGER",
        "stroke_count": "INTEGER",
        "avg_stroke_freq": "REAL",
        "max_stroke_freq": "REAL",
        "turn_count": "INTEGER",
        "avg_swolf": "REAL",
        "best_swolf": "REAL",
    },
}


# upsert 用命名参数绑定，缺键会直接报错；给这些可选字段兜底为 None
_WORKOUT_DEFAULTS: dict[str, Any] = {
    name: None
    for name in (
        "total_cal",
        "min_hr",
        "avg_pace_s",
        "max_pace_s",
        "pace_basis",
        "train_effect",
        "anaerobic_train_effect",
        "train_load",
        "recover_time_h",
        "vitality",
        "zone_warmup_s",
        "zone_fat_burn_s",
        "zone_aerobic_s",
        "zone_anaerobic_s",
        "zone_extreme_s",
        "stroke_count",
        "avg_stroke_freq",
        "max_stroke_freq",
        "turn_count",
        "avg_swolf",
        "best_swolf",
        "steps",
    )
}


# 早期把配速硬编码成「秒/公里」，对游泳是错的（差 10 倍）。
# 已由 avg_pace_s + pace_basis 取代，这里把废弃列删掉，避免有人误读旧值。
_DROP_COLUMNS: dict[str, tuple[str, ...]] = {
    "workout": ("avg_pace_s_km",),
}


def _ensure_columns(conn: sqlite3.Connection) -> None:
    for table, columns in _EXTRA_COLUMNS.items():
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()  # noqa: S608
        existing = {row["name"] for row in rows}
        for name, ddl in columns.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")  # noqa: S608

    for table, columns in _DROP_COLUMNS.items():
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()  # noqa: S608
        existing = {row["name"] for row in rows}
        for name in columns:
            if name in existing:
                conn.execute(f"ALTER TABLE {table} DROP COLUMN {name}")  # noqa: S608


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_FILE.read_text(encoding="utf-8"))
    _ensure_columns(conn)


def _executemany(conn: sqlite3.Connection, sql: str, rows: Sequence[Sequence[Any]]) -> int:
    if not rows:
        return 0
    before = conn.total_changes
    conn.executemany(sql, rows)
    return conn.total_changes - before


class Repository:
    """事实表与分析层产出的读写入口。"""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    # ------------------------------------------------------------ 元数据
    def record_raw(
        self,
        key_name: str,
        range_start: int,
        range_end: int,
        file_path: str,
        sha256: str,
        record_count: int,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO raw_response
                (key_name, range_start, range_end, fetched_at, file_path, sha256, record_count)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (key_name, range_start, range_end, sha256) DO UPDATE SET
                fetched_at   = excluded.fetched_at,
                file_path    = excluded.file_path,
                record_count = excluded.record_count
            """,
            (key_name, range_start, range_end, now_ts(), file_path, sha256, record_count),
        )

    def get_sync_state(self, key_name: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM sync_state WHERE key_name = ?", (key_name,)
        ).fetchone()
        return dict(row) if row else None

    def all_sync_states(self) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM sync_state ORDER BY key_name").fetchall()
        return [dict(row) for row in rows]

    def mark_sync_success(self, key_name: str, cursor_ts: int | None) -> None:
        self.conn.execute(
            """
            INSERT INTO sync_state (key_name, cursor_ts, last_success_at, last_attempt_at,
                                    last_error, consecutive_fail)
            VALUES (?, ?, ?, ?, NULL, 0)
            ON CONFLICT (key_name) DO UPDATE SET
                cursor_ts        = COALESCE(excluded.cursor_ts, sync_state.cursor_ts),
                last_success_at  = excluded.last_success_at,
                last_attempt_at  = excluded.last_attempt_at,
                last_error       = NULL,
                consecutive_fail = 0
            """,
            (key_name, cursor_ts, now_ts(), now_ts()),
        )

    def mark_sync_failure(self, key_name: str, error: str) -> None:
        self.conn.execute(
            """
            INSERT INTO sync_state (key_name, last_attempt_at, last_error, consecutive_fail)
            VALUES (?, ?, ?, 1)
            ON CONFLICT (key_name) DO UPDATE SET
                last_attempt_at  = excluded.last_attempt_at,
                last_error       = excluded.last_error,
                consecutive_fail = sync_state.consecutive_fail + 1
            """,
            (key_name, now_ts(), error[:500]),
        )

    def start_sync_run(self, tier: str) -> int:
        cursor = self.conn.execute(
            "INSERT INTO sync_run (tier, started_at, status) VALUES (?, ?, 'running')",
            (tier, now_ts()),
        )
        return int(cursor.lastrowid or 0)

    def finish_sync_run(
        self,
        run_id: int,
        ok_keys: list[str],
        failed_keys: dict[str, str],
        request_count: int,
        note: str | None = None,
    ) -> None:
        status = "success" if not failed_keys else ("partial" if ok_keys else "failed")
        self.conn.execute(
            """
            UPDATE sync_run
               SET finished_at = ?, ok_keys = ?, failed_keys = ?, request_count = ?, status = ?, note = ?
             WHERE id = ?
            """,
            (
                now_ts(),
                json.dumps(ok_keys, ensure_ascii=False),
                json.dumps(failed_keys, ensure_ascii=False),
                request_count,
                status,
                note,
                run_id,
            ),
        )

    def recent_sync_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM sync_run ORDER BY started_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------ 事实表写入
    def upsert_daily_activity(self, rows: Iterable[dict[str, Any]]) -> int:
        return _executemany(
            self.conn,
            """
            INSERT INTO daily_activity
                (date, steps, distance_m, calories, quality_flag, source, updated_at)
            VALUES (:date, :steps, :distance_m, :calories, :quality_flag, :source, :updated_at)
            ON CONFLICT (date) DO UPDATE SET
                steps        = excluded.steps,
                distance_m   = excluded.distance_m,
                calories     = excluded.calories,
                quality_flag = excluded.quality_flag,
                updated_at   = excluded.updated_at
            """,
            list(rows),
        )

    def upsert_heart_rate(self, rows: Iterable[dict[str, Any]]) -> int:
        return _executemany(
            self.conn,
            """
            INSERT INTO heart_rate_sample (ts, bpm, type, quality_flag)
            VALUES (:ts, :bpm, :type, :quality_flag)
            ON CONFLICT (ts, type) DO UPDATE SET
                bpm          = excluded.bpm,
                quality_flag = excluded.quality_flag
            """,
            list(rows),
        )

    def upsert_resting_hr(self, rows: Iterable[dict[str, Any]]) -> int:
        return _executemany(
            self.conn,
            """
            INSERT INTO resting_hr (date, bpm, quality_flag, source, updated_at)
            VALUES (:date, :bpm, :quality_flag, :source, :updated_at)
            ON CONFLICT (date) DO UPDATE SET
                bpm          = excluded.bpm,
                quality_flag = excluded.quality_flag,
                updated_at   = excluded.updated_at
            """,
            list(rows),
        )

    def upsert_sleep(self, rows: Iterable[dict[str, Any]]) -> int:
        materialized = list(rows)
        count = _executemany(
            self.conn,
            """
            INSERT INTO sleep_session
                (date, start_ts, end_ts, total_min, in_bed_min, deep_min, light_min, rem_min,
                 awake_min, nap_min, efficiency, score, is_nap, avg_hr, min_hr, max_hr,
                 avg_spo2, min_spo2, max_spo2, avg_breath, awake_cnt, has_stage, has_rem,
                 alg_version, quality_flag, source, updated_at)
            VALUES (:date, :start_ts, :end_ts, :total_min, :in_bed_min, :deep_min, :light_min,
                    :rem_min, :awake_min, :nap_min, :efficiency, :score, :is_nap, :avg_hr,
                    :min_hr, :max_hr, :avg_spo2, :min_spo2, :max_spo2, :avg_breath, :awake_cnt,
                    :has_stage, :has_rem, :alg_version, :quality_flag, :source, :updated_at)
            ON CONFLICT (date) DO UPDATE SET
                start_ts     = excluded.start_ts,
                end_ts       = excluded.end_ts,
                total_min    = excluded.total_min,
                in_bed_min   = excluded.in_bed_min,
                deep_min     = excluded.deep_min,
                light_min    = excluded.light_min,
                rem_min      = excluded.rem_min,
                awake_min    = excluded.awake_min,
                nap_min      = excluded.nap_min,
                efficiency   = excluded.efficiency,
                score        = excluded.score,
                is_nap       = excluded.is_nap,
                avg_hr       = excluded.avg_hr,
                min_hr       = excluded.min_hr,
                max_hr       = excluded.max_hr,
                avg_spo2     = excluded.avg_spo2,
                min_spo2     = excluded.min_spo2,
                max_spo2     = excluded.max_spo2,
                avg_breath   = excluded.avg_breath,
                awake_cnt    = excluded.awake_cnt,
                has_stage    = excluded.has_stage,
                has_rem      = excluded.has_rem,
                alg_version  = excluded.alg_version,
                quality_flag = excluded.quality_flag,
                updated_at   = excluded.updated_at
            """,
            [
                {
                    **row,
                    "nap_min": row.get("nap_min") or 0,
                    "awake_cnt": row.get("awake_cnt") or 0,
                    "has_stage": row.get("has_stage") or 0,
                    "has_rem": row.get("has_rem") or 0,
                    "avg_hr": row.get("avg_hr"),
                    "min_hr": row.get("min_hr"),
                    "max_hr": row.get("max_hr"),
                    "avg_spo2": row.get("avg_spo2"),
                    "min_spo2": row.get("min_spo2"),
                    "max_spo2": row.get("max_spo2"),
                    "avg_breath": row.get("avg_breath"),
                    "alg_version": row.get("alg_version"),
                }
                for row in materialized
            ],
        )
        # 分期重写：先删该日旧分期，再写新分期（分期是整体重算的结果）
        for row in materialized:
            self.conn.execute("DELETE FROM sleep_stage WHERE date = ?", (row["date"],))
            stages = row.get("stages") or []
            if stages:
                _executemany(
                    self.conn,
                    """
                    INSERT OR REPLACE INTO sleep_stage (date, stage, start_ts, end_ts, minutes)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    [
                        (row["date"], seg["stage"], seg["start_ts"], seg["end_ts"], seg["minutes"])
                        for seg in stages
                    ],
                )
        return count

    def upsert_spo2(self, rows: Iterable[dict[str, Any]]) -> int:
        return _executemany(
            self.conn,
            """
            INSERT INTO spo2_sample (ts, spo2, quality_flag)
            VALUES (:ts, :spo2, :quality_flag)
            ON CONFLICT (ts) DO UPDATE SET
                spo2 = excluded.spo2, quality_flag = excluded.quality_flag
            """,
            list(rows),
        )

    def upsert_stress(self, rows: Iterable[dict[str, Any]]) -> int:
        return _executemany(
            self.conn,
            """
            INSERT INTO stress_sample (ts, stress, quality_flag)
            VALUES (:ts, :stress, :quality_flag)
            ON CONFLICT (ts) DO UPDATE SET
                stress = excluded.stress, quality_flag = excluded.quality_flag
            """,
            list(rows),
        )

    def upsert_weight(self, rows: Iterable[dict[str, Any]]) -> int:
        return _executemany(
            self.conn,
            """
            INSERT INTO weight_record
                (date, weight_kg, bmi, body_fat_pct, muscle_kg, water_pct, quality_flag, updated_at)
            VALUES (:date, :weight_kg, :bmi, :body_fat_pct, :muscle_kg, :water_pct,
                    :quality_flag, :updated_at)
            ON CONFLICT (date) DO UPDATE SET
                weight_kg    = excluded.weight_kg,
                bmi          = excluded.bmi,
                body_fat_pct = excluded.body_fat_pct,
                muscle_kg    = excluded.muscle_kg,
                water_pct    = excluded.water_pct,
                updated_at   = excluded.updated_at
            """,
            list(rows),
        )

    def upsert_workouts(self, rows: Iterable[dict[str, Any]]) -> int:
        return _executemany(
            self.conn,
            """
            INSERT INTO workout
                (id, type, start_ts, end_ts, duration_s, distance_m, calories, total_cal,
                 avg_hr, max_hr, min_hr, avg_pace_s, max_pace_s, pace_basis, steps,
                 train_effect, anaerobic_train_effect, train_load, recover_time_h, vitality,
                 zone_warmup_s, zone_fat_burn_s, zone_aerobic_s, zone_anaerobic_s, zone_extreme_s,
                 stroke_count, avg_stroke_freq, max_stroke_freq, turn_count, avg_swolf, best_swolf,
                 quality_flag, raw_json, source, updated_at)
            VALUES (:id, :type, :start_ts, :end_ts, :duration_s, :distance_m, :calories, :total_cal,
                    :avg_hr, :max_hr, :min_hr, :avg_pace_s, :max_pace_s, :pace_basis, :steps,
                    :train_effect, :anaerobic_train_effect, :train_load, :recover_time_h, :vitality,
                    :zone_warmup_s, :zone_fat_burn_s, :zone_aerobic_s, :zone_anaerobic_s,
                    :zone_extreme_s, :stroke_count, :avg_stroke_freq, :max_stroke_freq,
                    :turn_count, :avg_swolf, :best_swolf,
                    :quality_flag, :raw_json, :source, :updated_at)
            ON CONFLICT (id) DO UPDATE SET
                end_ts                 = excluded.end_ts,
                duration_s             = excluded.duration_s,
                distance_m             = excluded.distance_m,
                calories               = excluded.calories,
                total_cal              = excluded.total_cal,
                avg_hr                 = excluded.avg_hr,
                max_hr                 = excluded.max_hr,
                min_hr                 = excluded.min_hr,
                avg_pace_s             = excluded.avg_pace_s,
                max_pace_s             = excluded.max_pace_s,
                pace_basis             = excluded.pace_basis,
                steps                  = excluded.steps,
                train_effect           = excluded.train_effect,
                anaerobic_train_effect = excluded.anaerobic_train_effect,
                train_load             = excluded.train_load,
                recover_time_h         = excluded.recover_time_h,
                vitality               = excluded.vitality,
                zone_warmup_s          = excluded.zone_warmup_s,
                zone_fat_burn_s        = excluded.zone_fat_burn_s,
                zone_aerobic_s         = excluded.zone_aerobic_s,
                zone_anaerobic_s       = excluded.zone_anaerobic_s,
                zone_extreme_s         = excluded.zone_extreme_s,
                stroke_count           = excluded.stroke_count,
                avg_stroke_freq        = excluded.avg_stroke_freq,
                max_stroke_freq        = excluded.max_stroke_freq,
                turn_count             = excluded.turn_count,
                avg_swolf              = excluded.avg_swolf,
                best_swolf             = excluded.best_swolf,
                quality_flag           = excluded.quality_flag,
                raw_json               = excluded.raw_json,
                updated_at             = excluded.updated_at
            """,
            [{**_WORKOUT_DEFAULTS, **row} for row in rows],
        )

    def upsert_anomaly_events(self, rows: Iterable[dict[str, Any]]) -> int:
        return _executemany(
            self.conn,
            """
            INSERT INTO anomaly_event (date, ts, kind, severity, message, payload_json, detected_at)
            VALUES (:date, :ts, :kind, :severity, :message, :payload_json, :detected_at)
            ON CONFLICT (date, kind, ts) DO UPDATE SET
                severity     = excluded.severity,
                message      = excluded.message,
                payload_json = excluded.payload_json
            """,
            [{**row, "detected_at": row.get("detected_at") or now_ts()} for row in rows],
        )

    # ------------------------------------------------------------ 覆盖度
    # 按数据源映射到 (事实表, 日期表达式, 采样时间列, 时间范围列对)。
    # - 采样类数据用 ts 列统计
    # - 每日一行的事实表没有 ts 列，用起止列表示范围（daily_activity 连起止都没有）
    # 之前这个函数只处理 heart_rate/spo2/stress，而调用方只传 steps/sleep，
    # 两边对不上 → data_coverage 表**永远是 0 行**，看板的覆盖度日历一直空着。
    _COVERAGE_SOURCES: dict[str, tuple[str, str, str | None, tuple[str, str] | None]] = {
        "steps": ("daily_activity", "date", None, None),
        "sleep": ("sleep_session", "date", None, ("start_ts", "end_ts")),
        "heart_rate": ("heart_rate_sample", "date(ts, 'unixepoch', '+8 hours')", "ts", None),
        "spo2": ("spo2_sample", "date(ts, 'unixepoch', '+8 hours')", "ts", None),
        "stress": ("stress_sample", "date(ts, 'unixepoch', '+8 hours')", "ts", None),
    }

    def refresh_coverage(self, date: str, key_name: str) -> None:
        """按事实表重算某日某数据源的采样数与时间范围。"""
        source = self._COVERAGE_SOURCES.get(key_name)
        if not source:
            return
        table, date_expr, ts_column, span_columns = source

        count = 0
        first_ts: int | None = None
        last_ts: int | None = None

        if ts_column:
            row = self.conn.execute(
                f"SELECT COUNT(*) AS n, MIN({ts_column}) AS a, MAX({ts_column}) AS b "  # noqa: S608
                f"FROM {table} WHERE {date_expr} = ?",
                (date,),
            ).fetchone()
            count = int(row["n"] or 0)
            first_ts, last_ts = row["a"], row["b"]
        else:
            row = self.conn.execute(
                f"SELECT COUNT(*) AS n FROM {table} WHERE {date_expr} = ?",  # noqa: S608
                (date,),
            ).fetchone()
            count = int(row["n"] or 0)
            if count and span_columns:
                a_col, b_col = span_columns
                span = self.conn.execute(
                    f"SELECT MIN({a_col}) AS a, MAX({b_col}) AS b FROM {table} "  # noqa: S608
                    f"WHERE {date_expr} = ?",
                    (date,),
                ).fetchone()
                first_ts, last_ts = span["a"], span["b"]

        self.conn.execute(
            """
            INSERT INTO data_coverage (date, key_name, sample_count, first_ts, last_ts, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (date, key_name) DO UPDATE SET
                sample_count = excluded.sample_count,
                first_ts     = excluded.first_ts,
                last_ts      = excluded.last_ts,
                updated_at   = excluded.updated_at
            """,
            (date, key_name, count, first_ts, last_ts, now_ts()),
        )

    def coverage_range(self, start: str, end: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT * FROM data_coverage
             WHERE date BETWEEN ? AND ?
             ORDER BY date, key_name
            """,
            (start, end),
        ).fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------ 统计（幂等验证、状态展示用）
    def table_counts(self) -> dict[str, int]:
        tables = (
            "daily_activity",
            "heart_rate_sample",
            "resting_hr",
            "sleep_session",
            "sleep_stage",
            "spo2_sample",
            "stress_sample",
            "weight_record",
            "workout",
            "raw_response",
            "anomaly_event",
        )
        result: dict[str, int] = {}
        for table in tables:
            row = self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()  # noqa: S608
            result[table] = int(row[0]) if row else 0
        return result

    def data_span(self, table: str, ts_column: str = "ts") -> tuple[int | None, int | None]:
        row = self.conn.execute(
            f"SELECT MIN({ts_column}), MAX({ts_column}) FROM {table}"  # noqa: S608
        ).fetchone()
        if not row:
            return (None, None)
        return (row[0], row[1])

    def integrity_check(self) -> str:
        row = self.conn.execute("PRAGMA integrity_check").fetchone()
        return str(row[0]) if row else "unknown"

    def vacuum_into(self, target: str | Path) -> None:
        Path(target).parent.mkdir(parents=True, exist_ok=True)
        self.conn.execute("VACUUM INTO ?", (str(target),))

    # ------------------------------------------------------------ AI 摘要缓存
    def upsert_ai_summary(
        self,
        day: str,
        summary: dict[str, Any],
        model: str,
        prompt_version: str,
        config_fingerprint: str | None = None,
        input_fingerprint: str | None = None,
    ) -> None:
        """缓存某日的 AI 解读。同日重算会覆盖（保持最新）。"""
        self.conn.execute(
            """
            INSERT INTO ai_daily_summary
                (date, summary_json, model, prompt_version, config_fingerprint, input_fingerprint, generated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (date) DO UPDATE SET
                summary_json       = excluded.summary_json,
                model              = excluded.model,
                prompt_version     = excluded.prompt_version,
                config_fingerprint = excluded.config_fingerprint,
                input_fingerprint  = excluded.input_fingerprint,
                generated_at       = excluded.generated_at
            """,
            (
                day,
                json.dumps(summary, ensure_ascii=False),
                model,
                prompt_version,
                config_fingerprint,
                input_fingerprint,
                now_ts(),
            ),
        )

    def get_ai_summary(self, day: str) -> dict[str, Any] | None:
        """读取缓存的 AI 解读。返回 None 表示当天没生成过。"""
        row = self.conn.execute(
            "SELECT summary_json, model, prompt_version, config_fingerprint, input_fingerprint, generated_at "
            "FROM ai_daily_summary WHERE date = ?",
            (day,),
        ).fetchone()
        if not row:
            return None
        return {
            "summary": json.loads(row["summary_json"]),
            "model": row["model"],
            "prompt_version": row["prompt_version"],
            "config_fingerprint": row["config_fingerprint"],
            "input_fingerprint": row["input_fingerprint"],
            "generated_at": row["generated_at"],
        }

    def list_ai_summaries(self, limit: int = 7) -> list[dict[str, Any]]:
        """最近 N 份 AI 解读，新的在前。供对话上下文与前端引用。"""
        rows = self.conn.execute(
            "SELECT date, summary_json, model, prompt_version, config_fingerprint, generated_at "
            "FROM ai_daily_summary ORDER BY date DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [
            {
                "date": row["date"],
                "summary": json.loads(row["summary_json"]),
                "model": row["model"],
                "prompt_version": row["prompt_version"],
                "config_fingerprint": row["config_fingerprint"],
                "generated_at": row["generated_at"],
            }
            for row in rows
        ]

    # ------------------------------------------------------------ AI 对话
    def add_chat_message(self, role: str, content: str) -> None:
        """追加一条对话消息（role: user / assistant）。"""
        self.conn.execute(
            "INSERT INTO ai_chat (role, content, created_at) VALUES (?, ?, ?)",
            (role, content, now_ts()),
        )

    def get_chat_history(self, limit: int = 50) -> list[dict[str, Any]]:
        """最近 N 条对话，按时间升序返回（便于直接作为模型上下文）。"""
        rows = self.conn.execute(
            "SELECT id, role, content, created_at FROM "
            "(SELECT * FROM ai_chat ORDER BY id DESC LIMIT ?) ORDER BY id ASC",
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------ 健康备注
    def add_health_note(self, note_date: str, content: str, source: str = "chat") -> int:
        """记录一条健康备注（用户主观陈述：症状/状态/生活事件）。"""
        cursor = self.conn.execute(
            "INSERT INTO health_note (note_date, content, source, created_at) "
            "VALUES (?, ?, ?, ?)",
            (note_date, content, source, now_ts()),
        )
        return int(cursor.lastrowid or 0)

    def get_health_notes(self, limit: int = 20) -> list[dict[str, Any]]:
        """最近 N 条健康备注，新的在前。"""
        rows = self.conn.execute(
            "SELECT id, note_date, content, source, created_at "
            "FROM health_note ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]

    def delete_health_note(self, note_id: int) -> bool:
        """删除一条健康备注。返回是否存在。"""
        cursor = self.conn.execute(
            "DELETE FROM health_note WHERE id = ?", (note_id,)
        )
        return cursor.rowcount > 0

    def get_health_notes_between(self, start: str, end: str) -> list[dict[str, Any]]:
        """某日期区间（含两端）内的健康备注，按日期升序。"""
        rows = self.conn.execute(
            "SELECT id, note_date, content, source FROM health_note "
            "WHERE note_date >= ? AND note_date <= ? ORDER BY note_date, id",
            (start, end),
        ).fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------ 周报 / 月报
    def upsert_ai_report(
        self,
        period_type: str,
        period_start: str,
        period_end: str,
        report: dict[str, Any],
        model: str,
        prompt_version: str,
        config_fingerprint: str | None = None,
        input_fingerprint: str | None = None,
    ) -> None:
        """写入/覆盖一份周期报告（period_type: weekly / monthly）。复用 report 表。"""
        self.conn.execute(
            """
            INSERT INTO report
                (period_type, period_start, period_end, generated_at, summary_json)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (period_type, period_start, period_end) DO UPDATE SET
                generated_at = excluded.generated_at,
                summary_json = excluded.summary_json
            """,
            (
                period_type,
                period_start,
                period_end,
                now_ts(),
                json.dumps(
                    {
                        **report,
                        "model": model,
                        "prompt_version": prompt_version,
                        "config_fingerprint": config_fingerprint,
                        "input_fingerprint": input_fingerprint,
                    },
                    ensure_ascii=False,
                ),
            ),
        )

    def get_ai_report(
        self, period_type: str, period_start: str, period_end: str
    ) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT summary_json, generated_at FROM report "
            "WHERE period_type = ? AND period_start = ? AND period_end = ?",
            (period_type, period_start, period_end),
        ).fetchone()
        if not row:
            return None
        return {
            "report": json.loads(row["summary_json"]),
            "generated_at": row["generated_at"],
        }

    def list_ai_reports(
        self, limit: int = 10, period_type: str | None = None
    ) -> list[dict[str, Any]]:
        """最近的周期报告列表，新的在前（含完整 report JSON，便于前端直接渲染）。"""
        sql = (
            "SELECT id, period_type, period_start, period_end, generated_at, summary_json "
            "FROM report WHERE period_type IN ('weekly', 'monthly')"
        )
        params: list[Any] = []
        if period_type:
            sql += " AND period_type = ?"
            params.append(period_type)
        sql += " ORDER BY period_end DESC, period_type LIMIT ?"
        params.append(limit)
        rows = self.conn.execute(sql, params).fetchall()
        items = []
        for row in rows:
            items.append(
                {
                    "id": row["id"],
                    "period_type": row["period_type"],
                    "period_start": row["period_start"],
                    "period_end": row["period_end"],
                    "generated_at": row["generated_at"],
                    "report": json.loads(row["summary_json"]) if row["summary_json"] else None,
                }
            )
        return items
