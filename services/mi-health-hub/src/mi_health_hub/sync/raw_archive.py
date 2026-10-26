"""原始响应归档。

**这是全项目最重要的保险**：小米改版导致解析失败时，只要原始响应在，
改完解析器就能对历史数据重放，不必重新拉取（也可能已经拉不到了）。
"""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
import os
import secrets
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class RawArchive:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def save(
        self,
        key_name: str,
        range_start: int,
        range_end: int,
        records: list[dict[str, Any]],
        *,
        endpoint: str,
    ) -> tuple[str, str, int]:
        """落盘一份原始记录集合，返回 (相对路径, sha256, 记录数)。

        **哈希只由稳定内容决定，不含归档时间**。否则每次同步算出的哈希都不同，
        去重完全失效、raw/ 与 raw_response 会随每次同步无限增长（这是实际踩过的 bug）。
        用稳定哈希做文件名后缀，因此同内容重复归档不会产生新文件。
        """
        stable = {
            "key": key_name,
            "endpoint": endpoint,
            "range_start": range_start,
            "range_end": range_end,
            "record_count": len(records),
            "records": records,
        }
        canonical = json.dumps(
            stable, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        digest = _sha256(canonical)

        stamp = datetime.fromtimestamp(range_start, tz=timezone.utc)
        rel_dir = Path(f"{stamp.year:04d}") / f"{stamp.month:02d}"
        filename = f"{key_name}_{range_start}_{range_end}_{digest[:12]}.json.gz"
        rel_path = rel_dir / filename
        target = self.root / rel_path

        if target.exists():
            try:
                if self.stable_digest(self.load(str(rel_path))) == digest:
                    return str(rel_path), digest, len(records)
            except (OSError, EOFError, ValueError, TypeError):
                pass
        payload = json.dumps(
            {**stable, "archived_at": datetime.now(tz=timezone.utc).isoformat()},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary_name = tempfile.mkstemp(prefix=".archive-", suffix=".partial", dir=target.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(fd, "wb") as stream:
                with gzip.GzipFile(fileobj=stream, mode="wb", compresslevel=6, mtime=0) as handle:
                    handle.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            with gzip.open(temporary, "rb") as handle:
                verified = json.loads(handle.read().decode("utf-8"))
            if self.stable_digest(verified) != digest:
                raise OSError("原始归档校验失败，未发布")
            if target.exists():
                # Preserve a corrupt predecessor without exposing a missing
                # final filename to concurrent readers. Successful writers
                # always replace the final file with a complete valid copy.
                try:
                    valid = self.stable_digest(self.load(str(rel_path))) == digest
                except (OSError, EOFError, ValueError, TypeError):
                    valid = False
                if not valid:
                    os.link(target, target.with_name(target.name + ".corrupt-" + secrets.token_hex(6)))
            os.replace(temporary, target)
            directory_fd = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            temporary.unlink(missing_ok=True)
        return str(rel_path), digest, len(records)

    def load(self, rel_path: str) -> dict[str, Any]:
        """读回归档，用于解析器改版后的重放。"""
        with gzip.open(self.root / rel_path, "rb") as handle:
            return json.loads(handle.read().decode("utf-8"))

    @staticmethod
    def stable_digest(payload: dict[str, Any]) -> str:
        """按稳定字段重算哈希（与 save() 的口径一致，不含 archived_at）。"""
        stable = {
            key: payload[key]
            for key in ("key", "endpoint", "range_start", "range_end", "record_count", "records")
            if key in payload
        }
        canonical = json.dumps(
            stable, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return _sha256(canonical)


def prune_stale(repo: Any, archive: RawArchive, *, dry_run: bool = True) -> dict[str, int]:
    """清理失配的归档：索引里的哈希与文件实际内容对不上、或文件已丢失。

    什么情况下会失配：早期版本的哈希把 `archived_at` 算进去了，于是同一份内容
    被反复归档成多份文件。这个函数把它们识别出来并清掉（默认只报告不删）。

    返回 {"checked", "stale", "missing", "removed_files"}。
    """
    stats = {"checked": 0, "stale": 0, "missing": 0, "removed_files": 0}
    rows = repo.conn.execute("SELECT id, file_path, sha256 FROM raw_response").fetchall()

    for row in rows:
        stats["checked"] += 1
        path = archive.root / row["file_path"]
        if not path.is_file():
            stats["missing"] += 1
            if not dry_run:
                repo.conn.execute("DELETE FROM raw_response WHERE id = ?", (row["id"],))
            continue

        try:
            with gzip.open(path, "rb") as handle:
                payload = json.loads(handle.read().decode("utf-8"))
            digest = RawArchive.stable_digest(payload)
        except Exception:  # noqa: BLE001 - 读不出来的归档一律视为可疑
            digest = ""

        if digest != row["sha256"]:
            stats["stale"] += 1
            if not dry_run:
                repo.conn.execute("DELETE FROM raw_response WHERE id = ?", (row["id"],))
                try:
                    path.unlink()
                    stats["removed_files"] += 1
                except OSError:
                    pass

    return stats
