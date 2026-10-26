"""Model gateway usage ledger.

Records what the forwarding layer actually did: one row per proxied request
and one row per connectivity probe.  The ledger is deliberately separate from
``ai-models.json`` so that high-frequency operational data never touches the
configuration store, and so an operator can clear it without risking routes.

The store keeps a bounded history; ``prune`` trims rows older than the
retention window and caps the total row count so the NAS volume cannot grow
without limit.
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator

USAGE_DB_FILE = "model-usage.db"
DEFAULT_RETENTION_DAYS = 30
MAX_LOG_ROWS = 50000
MAX_LOGS_PER_QUERY = 500

_SCHEMA = """
CREATE TABLE IF NOT EXISTS request_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    provider_id TEXT NOT NULL DEFAULT '',
    provider_name TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    protocol TEXT NOT NULL DEFAULT '',
    surface TEXT NOT NULL DEFAULT '',
    status INTEGER NOT NULL DEFAULT 0,
    ok INTEGER NOT NULL DEFAULT 0,
    latency_ms INTEGER NOT NULL DEFAULT 0,
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens INTEGER NOT NULL DEFAULT 0,
    error TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_request_log_ts ON request_log(ts DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_request_log_model ON request_log(model, ts DESC);

CREATE TABLE IF NOT EXISTS model_probe (
    provider_id TEXT NOT NULL,
    model TEXT NOT NULL,
    ts TEXT NOT NULL,
    ok INTEGER NOT NULL DEFAULT 0,
    status INTEGER NOT NULL DEFAULT 0,
    latency_ms INTEGER NOT NULL DEFAULT 0,
    message TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (provider_id, model)
);
"""


def _now() -> datetime:
    return datetime.now(UTC)


def _stamp(value: datetime | None = None) -> str:
    return (value or _now()).isoformat(timespec="seconds")


def _int(value: object) -> int:
    try:
        return max(0, int(value))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def probe_key(provider_id: str, model: str) -> str:
    return f"{provider_id}\u0000{model}"


class ModelUsageStore:
    """SQLite ledger shared by the config plane and the forwarding plane."""

    def __init__(self, state_dir: Path, retention_days: int = DEFAULT_RETENTION_DAYS):
        self.path = Path(state_dir) / USAGE_DB_FILE
        self.retention_days = max(1, _int(retention_days) or DEFAULT_RETENTION_DAYS)
        self._writes = 0
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    @contextmanager
    def _database(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._database() as connection:
            connection.executescript(_SCHEMA)
        if os.name != "nt":
            os.chmod(self.path, 0o600)
        self.prune()

    # ------------------------------------------------------------------ writes

    def record_request(
        self,
        *,
        provider_id: str,
        provider_name: str,
        model: str,
        protocol: str,
        surface: str,
        status: int,
        latency_ms: int,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        total_tokens: int = 0,
        error: str = "",
        when: datetime | None = None,
    ) -> None:
        with self._database() as connection:
            connection.execute(
                "INSERT INTO request_log (ts, provider_id, provider_name, model, protocol, surface,"
                " status, ok, latency_ms, prompt_tokens, completion_tokens, total_tokens, error)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    _stamp(when),
                    str(provider_id)[:64],
                    str(provider_name)[:80],
                    str(model)[:200],
                    str(protocol)[:16],
                    str(surface)[:32],
                    _int(status),
                    1 if 200 <= _int(status) < 400 else 0,
                    _int(latency_ms),
                    _int(prompt_tokens),
                    _int(completion_tokens),
                    _int(total_tokens),
                    str(error)[:400],
                ),
            )
            self._writes += 1
            if self._writes % 200 == 0:
                self.prune()

    def record_probe(
        self, provider_id: str, model: str, result: dict[str, Any], when: datetime | None = None
    ) -> None:
        with self._database() as connection:
            connection.execute(
                "INSERT INTO model_probe (provider_id, model, ts, ok, status, latency_ms, message)"
                " VALUES (?,?,?,?,?,?,?)"
                " ON CONFLICT(provider_id, model) DO UPDATE SET"
                " ts=excluded.ts, ok=excluded.ok, status=excluded.status,"
                " latency_ms=excluded.latency_ms, message=excluded.message",
                (
                    str(provider_id)[:64],
                    str(model)[:200],
                    _stamp(when),
                    1 if result.get("ok") else 0,
                    _int(result.get("status")),
                    _int(result.get("latency_ms")),
                    str(result.get("message") or result.get("error") or "")[:200],
                ),
            )

    def forget_provider(self, provider_id: str) -> None:
        """Drop probe rows for a deleted provider; request history is kept."""

        with self._database() as connection:
            connection.execute("DELETE FROM model_probe WHERE provider_id = ?", (str(provider_id)[:64],))

    def forget_models(self, provider_id: str, models: list[str]) -> None:
        if not models:
            return
        with self._database() as connection:
            connection.executemany(
                "DELETE FROM model_probe WHERE provider_id = ? AND model = ?",
                [(str(provider_id)[:64], str(model)[:200]) for model in models],
            )

    def clear_logs(self) -> int:
        with self._database() as connection:
            removed = connection.execute("DELETE FROM request_log").rowcount
        return int(removed or 0)

    def prune(self, retention_days: int | None = None) -> int:
        days = max(1, _int(retention_days) or self.retention_days)
        cutoff = _stamp(_now() - timedelta(days=days))
        removed = 0
        with self._database() as connection:
            removed += connection.execute("DELETE FROM request_log WHERE ts < ?", (cutoff,)).rowcount or 0
            overflow = connection.execute(
                "SELECT id FROM request_log ORDER BY id DESC LIMIT 1 OFFSET ?", (MAX_LOG_ROWS,)
            ).fetchone()
            if overflow is not None:
                removed += connection.execute(
                    "DELETE FROM request_log WHERE id <= ?", (overflow["id"],)
                ).rowcount or 0
        return int(removed)

    # ------------------------------------------------------------------- reads

    def probes(self) -> dict[str, dict[str, Any]]:
        with self._database() as connection:
            rows = connection.execute(
                "SELECT provider_id, model, ts, ok, status, latency_ms, message FROM model_probe"
            ).fetchall()
        return {
            probe_key(row["provider_id"], row["model"]): {
                "provider_id": row["provider_id"],
                "model": row["model"],
                "tested_at": row["ts"],
                "ok": bool(row["ok"]),
                "status": row["status"],
                "latency_ms": row["latency_ms"],
                "message": row["message"],
            }
            for row in rows
        }

    def _window_rows(self, hours: int) -> list[sqlite3.Row]:
        hours = max(1, min(24 * 90, _int(hours) or 24))
        cutoff = _stamp(_now() - timedelta(hours=hours))
        with self._database() as connection:
            return connection.execute(
                "SELECT ts, provider_id, provider_name, model, status, ok, latency_ms,"
                " prompt_tokens, completion_tokens, total_tokens, error"
                " FROM request_log WHERE ts >= ? ORDER BY id DESC",
                (cutoff,),
            ).fetchall()

    @staticmethod
    def _percentile(values: list[int], fraction: float) -> int:
        if not values:
            return 0
        ordered = sorted(values)
        index = min(len(ordered) - 1, max(0, int(round(fraction * (len(ordered) - 1)))))
        return ordered[index]

    def summary(self, hours: int = 24) -> dict[str, Any]:
        hours = max(1, min(24 * 90, _int(hours) or 24))
        rows = self._window_rows(hours)
        latencies = [row["latency_ms"] for row in rows]
        total = len(rows)
        failed = sum(1 for row in rows if not row["ok"])
        per_model: dict[str, dict[str, Any]] = {}
        per_provider: dict[str, dict[str, Any]] = {}
        error_counts: dict[str, int] = {}
        for row in rows:
            for bucket, key, label in (
                (per_model, row["model"] or "—", row["model"] or "—"),
                (per_provider, row["provider_id"] or "—", row["provider_name"] or row["provider_id"] or "—"),
            ):
                entry = bucket.setdefault(
                    key,
                    {"key": key, "label": label, "requests": 0, "failed": 0, "total_latency": 0, "tokens": 0},
                )
                entry["requests"] += 1
                entry["failed"] += 0 if row["ok"] else 1
                entry["total_latency"] += row["latency_ms"]
                entry["tokens"] += row["total_tokens"] or (row["prompt_tokens"] + row["completion_tokens"])
            if not row["ok"] and row["error"]:
                error_counts[row["error"]] = error_counts.get(row["error"], 0) + 1
        for bucket in (per_model, per_provider):
            for entry in bucket.values():
                entry["avg_latency_ms"] = round(entry.pop("total_latency") / entry["requests"]) if entry["requests"] else 0
        bucket_count = 24 if hours <= 24 else min(30, max(7, hours // 24))
        span = timedelta(hours=hours) / bucket_count
        origin = _now() - timedelta(hours=hours)
        series = [{"at": _stamp(origin + span * index), "total": 0, "failed": 0} for index in range(bucket_count)]
        for row in rows:
            try:
                moment = datetime.fromisoformat(row["ts"])
            except ValueError:
                continue
            offset = int((moment - origin) / span)
            if 0 <= offset < bucket_count:
                series[offset]["total"] += 1
                if not row["ok"]:
                    series[offset]["failed"] += 1
        return {
            "hours": hours,
            "total": total,
            "ok": total - failed,
            "failed": failed,
            "success_rate": round((total - failed) / total * 100, 1) if total else None,
            "avg_latency_ms": round(sum(latencies) / total) if total else 0,
            "p95_latency_ms": self._percentile(latencies, 0.95),
            "tokens": sum(row["total_tokens"] or (row["prompt_tokens"] + row["completion_tokens"]) for row in rows),
            "series": series,
            "models": sorted(per_model.values(), key=lambda item: (-item["requests"], item["key"]))[:20],
            "providers": sorted(per_provider.values(), key=lambda item: (-item["requests"], item["key"]))[:20],
            "errors": sorted(
                ({"message": message, "count": count} for message, count in error_counts.items()),
                key=lambda item: -item["count"],
            )[:10],
        }

    def logs(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        provider_id: str = "",
        model: str = "",
        only_errors: bool = False,
    ) -> dict[str, Any]:
        limit = max(1, min(MAX_LOGS_PER_QUERY, _int(limit) or 100))
        offset = max(0, _int(offset))
        clauses: list[str] = []
        params: list[Any] = []
        if provider_id:
            clauses.append("provider_id = ?")
            params.append(str(provider_id)[:64])
        if model:
            clauses.append("model = ?")
            params.append(str(model)[:200])
        if only_errors:
            clauses.append("ok = 0")
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._database() as connection:
            total = connection.execute(f"SELECT COUNT(*) AS n FROM request_log{where}", params).fetchone()["n"]
            rows = connection.execute(
                "SELECT id, ts, provider_id, provider_name, model, protocol, surface, status, ok,"
                f" latency_ms, prompt_tokens, completion_tokens, total_tokens, error FROM request_log{where}"
                " ORDER BY id DESC LIMIT ? OFFSET ?",
                [*params, limit, offset],
            ).fetchall()
        return {
            "total": total,
            "limit": limit,
            "offset": offset,
            "items": [
                {
                    "id": row["id"],
                    "at": row["ts"],
                    "provider_id": row["provider_id"],
                    "provider_name": row["provider_name"],
                    "model": row["model"],
                    "protocol": row["protocol"],
                    "surface": row["surface"],
                    "status": row["status"],
                    "ok": bool(row["ok"]),
                    "latency_ms": row["latency_ms"],
                    "prompt_tokens": row["prompt_tokens"],
                    "completion_tokens": row["completion_tokens"],
                    "total_tokens": row["total_tokens"],
                    "error": row["error"],
                }
                for row in rows
            ],
        }
