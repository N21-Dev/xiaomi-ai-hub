"""Bounded SQLite maintenance and verified backups for the household hub.

Normal maintenance uses short transactions and never VACUUMs the live database.
The explicit offline migration preserves every historical value and timestamp.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import logging
import os
import sqlite3
import threading
import time
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _integer_env(name: str, default: int, minimum: int, maximum: int) -> int:
    value = int(os.getenv(name, str(default)))
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


class HubMaintenance:
    def __init__(self, store: Any) -> None:
        self.db_path = store.db_path
        self.state_dir = store.state_dir
        self.backup_dir = self.state_dir / "hub-backups"
        self.status_path = self.state_dir / "hub-maintenance.json"
        self.retention_days = _integer_env("HUB_HISTORY_RETENTION_DAYS", 30, 1, 3650)
        self.backup_hours = _integer_env("HUB_BACKUP_INTERVAL_HOURS", 24, 1, 168)
        self.keep_daily = _integer_env("HUB_BACKUP_KEEP_DAILY", 7, 1, 90)
        self.keep_weekly = _integer_env("HUB_BACKUP_KEEP_WEEKLY", 4, 0, 52)
        self._lock = threading.Lock()
        self._state_lock = threading.RLock()
        self._cached: tuple[float, dict[str, Any]] | None = None
        self._state: dict[str, Any] = {
            "backup": {"last_success_at": None, "last_error": "", "last_size_bytes": 0},
            "maintenance": {
                "last_attempt_at": None, "last_success_at": None,
                "last_error": "", "running": False,
            },
        }
        try:
            previous = json.loads(self.status_path.read_text(encoding="utf-8"))
            for section in self._state:
                if isinstance(previous.get(section), dict):
                    self._state[section].update(previous[section])
            self._state["maintenance"]["running"] = False
        except (OSError, ValueError, TypeError, AttributeError):
            pass

    def _connect(self, *, readonly: bool = False) -> sqlite3.Connection:
        uri = self.db_path.resolve().as_uri() + ("?mode=ro" if readonly else "?mode=rw")
        conn = sqlite3.connect(uri, uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        return conn

    def _update(self, section: str, **values: Any) -> None:
        with self._state_lock:
            self._state[section].update(values)
            self._cached = None
            temporary = self.status_path.with_suffix(".tmp")
            with open(temporary, "w", encoding="utf-8") as stream:
                os.chmod(temporary, 0o600)
                json.dump(self._state, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.status_path)

    def status(self) -> dict[str, Any]:
        with self._state_lock:
            if self._cached and time.monotonic() - self._cached[0] < 60:
                return copy.deepcopy(self._cached[1])
            state = copy.deepcopy(self._state)
        with closing(self._connect(readonly=True)) as conn:
            history = dict(conn.execute(
                "SELECT COUNT(*) AS event_count, MIN(observed_at) AS oldest_observed_at, "
                "MAX(observed_at) AS newest_observed_at, "
                "COALESCE(SUM(fields_blob IS NOT NULL),0) AS compressed_events, "
                "COALESCE(SUM(fields_blob IS NULL AND fields_json != '[]'),0) AS pending_compression "
                "FROM hub_events"
            ).fetchone())
            resources = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(enabled),0) FROM resources"
            ).fetchone()
            history.update(resource_count=resources[0], enabled_resource_count=resources[1])
            page_size = conn.execute("PRAGMA page_size").fetchone()[0]
            allocated = conn.execute("PRAGMA page_count").fetchone()[0] * page_size
            free = conn.execute("PRAGMA freelist_count").fetchone()[0] * page_size
        wal = self.db_path.with_name(self.db_path.name + "-wal")
        result = {
            "database": {
                "bytes": self.db_path.stat().st_size,
                "wal_bytes": wal.stat().st_size if wal.exists() else 0,
                "allocated_bytes": allocated, "free_bytes": free,
            },
            "history": history,
            "policy": {
                "max_events_per_resource": 5000, "max_age_days": self.retention_days,
                "backup_interval_hours": self.backup_hours,
                "backup_keep_daily": self.keep_daily, "backup_keep_weekly": self.keep_weekly,
            },
            **state,
        }
        with self._state_lock:
            self._cached = (time.monotonic(), result)
        return copy.deepcopy(result)

    @staticmethod
    def verify_database(path: Path, *, deadline: float | None = None) -> None:
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError("备份验证超时，下轮重试")
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
            if deadline is not None:
                conn.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
            if conn.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise RuntimeError("数据库副本完整性校验失败")

    def backup(self) -> Path:
        self.backup_dir.mkdir(mode=0o700, exist_ok=True)
        os.chmod(self.backup_dir, 0o700)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        target = self.backup_dir / f"hub-{stamp}.sqlite3"
        partial = target.with_suffix(".partial")
        fd = os.open(partial, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        try:
            deadline = time.monotonic() + 120

            def progress(_status: int, _remaining: int, _total: int) -> None:
                if time.monotonic() > deadline:
                    raise TimeoutError("备份超时，下轮重试")

            with closing(self._connect(readonly=True)) as source:
                with closing(sqlite3.connect(partial)) as destination:
                    source.backup(destination, pages=512, progress=progress, sleep=0.05)
            self.verify_database(partial, deadline=deadline)
            with closing(sqlite3.connect(partial)) as verified:
                last_event_id = verified.execute("SELECT COALESCE(MAX(id),0) FROM hub_events").fetchone()[0]
            os.replace(partial, target)
            self._update("backup", last_success_at=_now(), last_error="",
                         last_size_bytes=target.stat().st_size, filename=target.name,
                         last_event_id=last_event_id)
            self._prune_backups()
            return target
        except Exception as exc:
            partial.unlink(missing_ok=True)
            self._update("backup", last_error=f"备份未完成（{type(exc).__name__}），已有备份保留")
            raise

    def _prune_backups(self) -> None:
        candidates: list[tuple[datetime, Path]] = []
        for path in self.backup_dir.glob("hub-*.sqlite3"):
            if path.is_symlink() or not path.is_file():
                continue
            try:
                stamp = datetime.strptime(path.stem.removeprefix("hub-"), "%Y%m%dT%H%M%S%fZ")
                candidates.append((stamp, path))
            except ValueError:
                continue
        days, weeks, keep = set(), set(), set()
        for stamp, path in sorted(candidates, reverse=True):
            day = stamp.date()
            if day not in days and len(days) < self.keep_daily:
                days.add(day)
                keep.add(path)
            elif day not in days:
                week = day.isocalendar()[:2]
                if week not in weeks and len(weeks) < self.keep_weekly:
                    weeks.add(week)
                    keep.add(path)
        for _, path in candidates:
            if path not in keep:
                path.unlink(missing_ok=True)

    def _backup_due(self) -> bool:
        with self._state_lock:
            backup = self._state["backup"].copy()
        name = backup.get("filename") or ""
        if not name or Path(name).name != name or not (self.backup_dir / name).is_file():
            return True
        try:
            age = datetime.now(UTC) - datetime.fromisoformat(backup["last_success_at"])
            return age >= timedelta(hours=self.backup_hours) or age.total_seconds() < 0
        except (ValueError, TypeError, KeyError):
            return True

    def compress_batch(self, limit: int = 250) -> int:
        from .hub_data import decode_event_fields, encode_event_fields

        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT id, fields_json FROM hub_events WHERE fields_blob IS NULL "
                "AND fields_json != '[]' ORDER BY id LIMIT ?", (limit,),
            ).fetchall()
            values = []
            for row in rows:
                encoded = encode_event_fields(row["fields_json"])
                if decode_event_fields({"fields_blob": encoded, "fields_json": "[]"}) != row["fields_json"]:
                    raise ValueError("历史字段压缩验证失败")
                values.append((encoded, row["id"], row["fields_json"]))
            with conn:
                conn.executemany(
                    "UPDATE hub_events SET fields_blob=?, fields_json='[]' "
                    "WHERE id=? AND fields_blob IS NULL AND fields_json=?", values,
                )
            return len(values)

    def restore_fields(self) -> int:
        """Offline compatibility restore, retaining samples added since migration."""
        from .hub_data import decode_event_fields

        restored = 0
        with closing(self._connect()) as conn:
            while True:
                rows = conn.execute(
                    "SELECT id, fields_json, fields_blob FROM hub_events "
                    "WHERE fields_blob IS NOT NULL ORDER BY id LIMIT 250"
                ).fetchall()
                if not rows:
                    break
                values = [(decode_event_fields(row), row["id"]) for row in rows]
                with conn:
                    conn.executemany(
                        "UPDATE hub_events SET fields_json=?,fields_blob=NULL WHERE id=?", values,
                    )
                restored += len(values)
        return restored

    def maintain(self) -> None:
        if not self._lock.acquire(blocking=False):
            return
        try:
            self._update("maintenance", running=True, last_attempt_at=_now(), last_error="")
            # Fail closed on backup failure: no pruning or migration follows it.
            if self._backup_due():
                self.backup()
            self.compress_batch()
            cutoff = (datetime.now(UTC) - timedelta(days=self.retention_days)).isoformat()
            with self._state_lock:
                backed_up_id = self._state["backup"].get("last_event_id", 0)
            if type(backed_up_id) is not int:
                backed_up_id = 0
            with closing(self._connect()) as conn:
                with conn:
                    # Bounded deletion keeps the writer lock short on large archives.
                    deleted = conn.execute(
                        "DELETE FROM hub_events WHERE id IN "
                        "(SELECT id FROM hub_events WHERE observed_at < ? AND id <= ? LIMIT 1000)",
                        (cutoff, backed_up_id),
                    ).rowcount
                conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
            self._update("maintenance", last_success_at=_now(), last_error="",
                         last_pruned_events=deleted)
        except Exception as exc:
            LOGGER.warning("Hub maintenance deferred: %s", type(exc).__name__)
            self._update("maintenance", last_error=f"维护未完成（{type(exc).__name__}），下轮重试")
        finally:
            try:
                self._update("maintenance", running=False)
            finally:
                self._lock.release()

    async def run(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            try:
                await asyncio.to_thread(self.maintain)
            except Exception as exc:
                LOGGER.warning("Hub maintenance worker unavailable: %s", type(exc).__name__)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=900)
            except TimeoutError:
                pass


def main() -> None:
    parser = argparse.ArgumentParser(description="Offline hub archive migration and verified backup")
    parser.add_argument("command", choices=("backup", "migrate", "restore-fields", "status"))
    parser.add_argument("--state-dir", type=Path, default=Path("/data"))
    parser.add_argument("--vacuum", action="store_true", help="Only with service stopped; reclaim unused pages")
    args = parser.parse_args()
    from .hub_data import HubDataStore

    manager = HubDataStore(args.state_dir).maintenance
    if args.command == "status":
        print(json.dumps(manager.status(), ensure_ascii=False))
        return
    backup = manager.backup()
    changed = 0
    if args.command in {"migrate", "restore-fields"}:
        protected = manager.backup_dir / "migrations"
        protected.mkdir(mode=0o700, exist_ok=True)
        os.link(backup, protected / backup.name)
    if args.command == "migrate":
        while count := manager.compress_batch():
            changed += count
    elif args.command == "restore-fields":
        changed = manager.restore_fields()
    if args.vacuum:
        with closing(manager._connect()) as conn:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.execute("VACUUM")
    manager.verify_database(manager.db_path)
    print(json.dumps({"backup": backup.name, "changed_events": changed, "integrity": "ok"}))


if __name__ == "__main__":
    main()
