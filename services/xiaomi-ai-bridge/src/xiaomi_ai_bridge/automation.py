"""Persistent, permission-checked device automations.

The scheduler stores a structured device action and never asks the model to
reinterpret it at execution time.  SQLite is kept separate from hub history
and health data so a restart does not lose pending tasks.
"""

from __future__ import annotations

import json
import math
import os
import secrets
import sqlite3
import threading
from contextlib import closing, contextmanager
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


AUTOMATION_TOOL_NAME = "manage_automations"
AUTOMATION_TZ = ZoneInfo("Asia/Shanghai")
MAX_AUTOMATIONS = 100
MAX_DELAY_SECONDS = 365 * 24 * 60 * 60


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _iso(value: datetime) -> str:
    return _utc(value).isoformat()


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class AutomationStore:
    """Own the task database and the due-task claim protocol."""

    def __init__(self, state_dir: Path):
        self.state_dir = Path(state_dir)
        self.path = self.state_dir / "automations.sqlite3"
        self._lock = threading.RLock()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    @contextmanager
    def _database(self):
        with self._lock, closing(self._connect()) as connection, connection:
            yield connection

    def _initialize(self) -> None:
        with self._database() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS automations (
                    id TEXT PRIMARY KEY,
                    schedule_json TEXT NOT NULL,
                    next_run_at TEXT,
                    target_json TEXT NOT NULL,
                    action TEXT NOT NULL,
                    value_json TEXT,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_run_at TEXT,
                    last_outcome TEXT,
                    last_message TEXT,
                    run_count INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_automations_due "
                "ON automations(status, next_run_at)"
            )
            columns = {row[1] for row in connection.execute("PRAGMA table_info(automations)")}
            for name, kind in (
                ("attempt_id", "TEXT"), ("started_at", "TEXT"),
                ("paused_remaining_seconds", "INTEGER"),
            ):
                if name not in columns:
                    connection.execute(f"ALTER TABLE automations ADD COLUMN {name} {kind}")
        if os.name != "nt":
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass

    @staticmethod
    def _parse_clock(value: object) -> time:
        if not isinstance(value, str) or len(value) not in {5, 8}:
            raise ValueError("时间必须是 HH:MM")
        try:
            parsed = time.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("时间必须是 HH:MM") from exc
        if parsed.second or parsed.microsecond:
            raise ValueError("时间只能精确到分钟")
        return parsed

    @classmethod
    def _schedule_next(
        cls, schedule: object, *, now: datetime | None = None
    ) -> tuple[dict[str, Any], datetime | None]:
        if not isinstance(schedule, dict):
            raise ValueError("schedule 必须是对象")
        if set(schedule) - {"type", "at", "delay_seconds", "days"}:
            raise ValueError("schedule 包含未支持字段")
        kind = schedule.get("type")
        if kind not in {"once", "delay", "daily", "weekly"}:
            raise ValueError("任务类型仅支持 once、delay、daily、weekly")
        now_utc = _utc(now or datetime.now(UTC))
        local_now = now_utc.astimezone(AUTOMATION_TZ)
        if kind == "delay":
            seconds = schedule.get("delay_seconds")
            if type(seconds) is not int or not 1 <= seconds <= MAX_DELAY_SECONDS:
                raise ValueError("delay_seconds 必须是 1 到 31536000 的整数")
            normalized = {"type": kind, "delay_seconds": seconds}
            return normalized, now_utc + timedelta(seconds=seconds)
        if kind == "once":
            raw = schedule.get("at")
            if not isinstance(raw, str) or not 1 <= len(raw) <= 40:
                raise ValueError("一次性任务需要 at 日期时间")
            try:
                parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError("at 必须是 YYYY-MM-DD HH:MM") from exc
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=AUTOMATION_TZ)
            parsed = parsed.astimezone(UTC).replace(second=0, microsecond=0)
            if parsed <= now_utc:
                raise ValueError("一次性任务时间必须在未来")
            normalized = {"type": kind, "at": parsed.astimezone(AUTOMATION_TZ).strftime("%Y-%m-%d %H:%M")}
            return normalized, parsed
        clock = cls._parse_clock(schedule.get("at"))
        if kind == "daily":
            candidate = datetime.combine(local_now.date(), clock, AUTOMATION_TZ)
            if candidate <= local_now:
                candidate += timedelta(days=1)
            return {"type": kind, "at": clock.strftime("%H:%M")}, candidate.astimezone(UTC)
        days = schedule.get("days")
        if not isinstance(days, list) or not days or any(type(day) is not int or not 0 <= day <= 6 for day in days):
            raise ValueError("weekly 任务需要 0 到 6 的 days 列表，0 表示周一")
        normalized_days = sorted(set(days))
        for offset in range(8):
            date = local_now.date() + timedelta(days=offset)
            if date.weekday() not in normalized_days:
                continue
            candidate = datetime.combine(date, clock, AUTOMATION_TZ)
            if candidate > local_now:
                return {"type": kind, "at": clock.strftime("%H:%M"), "days": normalized_days}, candidate.astimezone(UTC)
        raise ValueError("weekly 任务无法计算下一次执行时间")

    @staticmethod
    def _row(row: sqlite3.Row) -> dict[str, Any]:
        schedule = json.loads(row["schedule_json"])
        target = json.loads(row["target_json"])
        value = json.loads(row["value_json"]) if row["value_json"] is not None else None
        return {
            "id": row["id"], "schedule": schedule, "next_run_at": row["next_run_at"],
            "target": target, "action": row["action"], "value": value,
            "status": row["status"], "created_at": row["created_at"],
            "updated_at": row["updated_at"], "last_run_at": row["last_run_at"],
            "last_outcome": row["last_outcome"], "last_message": row["last_message"],
            "run_count": row["run_count"],
            "attempt_id": row["attempt_id"], "started_at": row["started_at"],
        }

    def create(
        self, *, target: dict[str, Any], action: str, value: object, schedule: object,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        normalized, next_run = self._schedule_next(schedule, now=now)
        if not isinstance(target, dict) or set(target) - {"kind", "device_key", "device", "profile_key"}:
            raise ValueError("任务目标无效")
        if target.get("kind") not in {"xiaomi", "infrared_ac"} or not isinstance(target.get("device"), str) or not target["device"]:
            raise ValueError("任务目标设备无效")
        if not isinstance(action, str) or not 1 <= len(action) <= 80:
            raise ValueError("任务动作无效")
        if value is not None and not isinstance(value, (str, int, float, bool, dict)):
            raise ValueError("任务值无效")
        now_iso = _iso(now or datetime.now(UTC))
        task_id = "auto_" + secrets.token_urlsafe(12)
        with self._database() as connection:
            connection.execute("BEGIN IMMEDIATE")
            count = connection.execute(
                "SELECT COUNT(*) FROM automations WHERE status IN ('pending','claimed','running','paused')"
            ).fetchone()[0]
            if count >= MAX_AUTOMATIONS:
                raise ValueError("最多保存100个未结束的自动化任务")
            connection.execute(
                "INSERT INTO automations "
                "(id,schedule_json,next_run_at,target_json,action,value_json,status,created_at,updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (task_id, _json(normalized), _iso(next_run), _json(target), action,
                 None if value is None else _json(value), "pending", now_iso, now_iso),
            )
        return self.get(task_id) or {}

    def get(self, task_id: str) -> dict[str, Any] | None:
        with self._database() as connection:
            row = connection.execute("SELECT * FROM automations WHERE id=?", (task_id,)).fetchone()
        return self._row(row) if row else None

    def list(self, *, include_finished: bool = False) -> list[dict[str, Any]]:
        query = "SELECT * FROM automations"
        params: tuple[Any, ...] = ()
        if not include_finished:
            query += " WHERE status IN ('pending','claimed','running','paused','failed','unknown')"
        query += " ORDER BY CASE status WHEN 'running' THEN 0 WHEN 'claimed' THEN 1 WHEN 'pending' THEN 2 WHEN 'paused' THEN 3 ELSE 4 END, next_run_at"
        with self._database() as connection:
            rows = connection.execute(query, params).fetchall()
        return [self._row(row) for row in rows]

    def claim_due(self, *, now: datetime | None = None, limit: int = 1) -> list[dict[str, Any]]:
        now_iso = _iso(now or datetime.now(UTC))
        with self._database() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT * FROM automations WHERE status='pending' AND next_run_at IS NOT NULL AND next_run_at<=? ORDER BY next_run_at LIMIT ?",
                (now_iso, max(1, min(limit, 50))),
            ).fetchall()
            for row in rows:
                connection.execute(
                    "UPDATE automations SET status='claimed', attempt_id=?, started_at=NULL, updated_at=? "
                    "WHERE id=? AND status='pending'",
                    (secrets.token_urlsafe(16), now_iso, row["id"]),
                )
            claimed = [connection.execute("SELECT * FROM automations WHERE id=?", (row["id"],)).fetchone() for row in rows]
        return [self._row(row) for row in claimed]

    def begin_execution(self, task_id: str, attempt_id: str, *, now: datetime | None = None) -> dict[str, Any] | None:
        """Commit the send boundary; cancelled or superseded claims never send."""
        if not attempt_id:
            return None
        now_iso = _iso(now or datetime.now(UTC))
        with self._database() as connection:
            connection.execute("BEGIN IMMEDIATE")
            result = connection.execute(
                "UPDATE automations SET status='running', started_at=?, updated_at=? "
                "WHERE id=? AND status='claimed' AND attempt_id=?",
                (now_iso, now_iso, task_id, attempt_id),
            )
            if not result.rowcount:
                return None
            row = connection.execute("SELECT * FROM automations WHERE id=?", (task_id,)).fetchone()
        return self._row(row)

    def recover_interrupted(self, *, now: datetime | None = None) -> None:
        """Called once by the scheduler at startup, never by readers.

        Claims that never crossed the send boundary can be retried. Legacy
        running rows and started sends have unknown outcomes and are not replayed.
        """
        value = now or datetime.now(UTC)
        now_iso = _iso(value)
        with self._database() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE automations SET status='pending', attempt_id=NULL, started_at=NULL, updated_at=? "
                "WHERE status='claimed'", (now_iso,),
            )
            for row in connection.execute("SELECT * FROM automations WHERE status='running'").fetchall():
                schedule = json.loads(row["schedule_json"])
                repeat = schedule["type"] in {"daily", "weekly"}
                next_run = self._schedule_next(schedule, now=value)[1] if repeat else None
                connection.execute(
                    "UPDATE automations SET status=?,next_run_at=?,attempt_id=NULL,updated_at=?,"
                    "last_run_at=COALESCE(started_at,updated_at),last_outcome='unconfirmed',"
                    "last_message=?,run_count=run_count+1 WHERE id=?",
                    ("pending" if repeat else "unknown", _iso(next_run) if next_run else None,
                     now_iso, "上次执行因服务中断，设备结果未确认；未自动重发，请检查设备状态", row["id"]),
                )

    def complete(self, task_id: str, *, attempt_id: str, outcome: str, message: str, now: datetime | None = None) -> dict[str, Any] | None:
        if outcome not in {"confirmed", "accepted", "unconfirmed", "failed"}:
            raise ValueError("自动化执行结果无效")
        now_value = now or datetime.now(UTC)
        now_iso = _iso(now_value)
        with self._database() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM automations WHERE id=?", (task_id,)).fetchone()
            if row is None:
                return None
            if row["status"] != "running" or not attempt_id or row["attempt_id"] != attempt_id:
                return self._row(row)
            schedule = json.loads(row["schedule_json"])
            if schedule["type"] in {"daily", "weekly"}:
                _, next_run = self._schedule_next(schedule, now=now_value)
                status, next_iso = "pending", _iso(next_run)
            else:
                status = "completed" if outcome in {"confirmed", "accepted"} else "unknown" if outcome == "unconfirmed" else "failed"
                next_iso = None
            connection.execute(
                "UPDATE automations SET status=?, next_run_at=?, updated_at=?, last_run_at=?, last_outcome=?, last_message=?, run_count=run_count+1 WHERE id=?",
                (status, next_iso, now_iso, now_iso, outcome, str(message)[:500], task_id),
            )
            row = connection.execute("SELECT * FROM automations WHERE id=?", (task_id,)).fetchone()
        return self._row(row)

    def pause(self, task_id: str, *, now: datetime | None = None) -> dict[str, Any] | None:
        value = now or datetime.now(UTC)
        with self._database() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM automations WHERE id=?", (task_id,)).fetchone()
            if row is None:
                return None
            if row["status"] not in {"pending", "claimed", "paused"}:
                raise ValueError("只有尚未开始执行的任务可以暂停")
            if row["status"] != "paused":
                remaining = None
                if json.loads(row["schedule_json"])["type"] == "delay" and row["next_run_at"]:
                    remaining = max(1, math.ceil((datetime.fromisoformat(row["next_run_at"]) - _utc(value)).total_seconds()))
                connection.execute(
                    "UPDATE automations SET status='paused',attempt_id=NULL,paused_remaining_seconds=?,updated_at=? WHERE id=?",
                    (remaining, _iso(value), task_id),
                )
            row = connection.execute("SELECT * FROM automations WHERE id=?", (task_id,)).fetchone()
        return self._row(row)

    def resume(self, task_id: str, *, now: datetime | None = None) -> dict[str, Any] | None:
        value = now or datetime.now(UTC)
        with self._database() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM automations WHERE id=?", (task_id,)).fetchone()
            if row is None:
                return None
            if row["status"] != "paused":
                raise ValueError("只有已暂停的任务可以恢复")
            schedule = json.loads(row["schedule_json"])
            next_iso = row["next_run_at"]
            if schedule["type"] == "once" and next_iso:
                next_run = datetime.fromisoformat(row["next_run_at"])
                if next_run <= _utc(value):
                    raise ValueError("一次性任务时间已过，不能恢复")
            elif schedule["type"] in {"daily", "weekly"}:
                next_iso = _iso(self._schedule_next(schedule, now=value)[1])
            elif schedule["type"] == "delay":
                remaining = row["paused_remaining_seconds"] or schedule["delay_seconds"]
                next_iso = _iso(_utc(value) + timedelta(seconds=remaining))
            if not next_iso:
                raise ValueError("任务已无可恢复的执行时间，请重新创建")
            connection.execute(
                "UPDATE automations SET status='pending',next_run_at=?,attempt_id=NULL,paused_remaining_seconds=NULL,updated_at=? WHERE id=?",
                (next_iso, _iso(value), task_id),
            )
            row = connection.execute("SELECT * FROM automations WHERE id=?", (task_id,)).fetchone()
        return self._row(row)

    def cancel(self, task_id: str) -> dict[str, Any] | None:
        return self._set_status(task_id, "cancelled", allowed=("pending", "claimed", "paused", "unknown", "failed"))

    def _set_status(self, task_id: str, status: str, *, allowed: tuple[str, ...] = ("pending", "failed")) -> dict[str, Any] | None:
        with self._database() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM automations WHERE id=?", (task_id,)).fetchone()
            if row is not None and row["status"] == "running":
                raise ValueError("任务已开始执行，无法保证撤回；请检查设备状态")
            placeholders = ",".join("?" for _ in allowed)
            now_iso = _iso(datetime.now(UTC))
            connection.execute(
                f"UPDATE automations SET status=?, attempt_id=NULL, updated_at=? WHERE id=? AND status IN ({placeholders})",
                (status, now_iso, task_id, *allowed),
            )
            row = connection.execute("SELECT * FROM automations WHERE id=?", (task_id,)).fetchone()
        return self._row(row) if row else None
