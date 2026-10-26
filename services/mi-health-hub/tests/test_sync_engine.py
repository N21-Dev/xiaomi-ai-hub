"""同步引擎测试（用假客户端，不打真实接口）。

覆盖两个实际踩过的问题：
1. **运动记录被漏掉**：sport_records 走独立接口，不在 SYNC_KEYS 里，
   漏接的话 workout 表永远是 0，而其它指标看起来完全正常。
2. **单 key 失败隔离**：某个 key 抛错不能拖垮整个同步。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from mi_health_hub.cloud.client import MiAuthError
from mi_health_hub.config import load_config
from mi_health_hub.store.repository import Repository, connect, init_db
from mi_health_hub.sync.engine import SyncEngine
from mi_health_hub.sync.raw_archive import RawArchive

CN = timezone(timedelta(hours=8))


def ts(y: int, m: int, d: int, hh: int = 12) -> int:
    return int(datetime(y, m, d, hh, tzinfo=CN).timestamp())


class FakeClient:
    """最小可用的假客户端。"""

    def __init__(self, *, failing_keys: set[str] | None = None, workouts: bool = True) -> None:
        self.failing_keys = failing_keys or set()
        self.workouts = workouts
        self.calls: list[str] = []
        self.budget = type("B", (), {"count": 0, "consume": lambda self_: None})()
        self.pass_token = "token-original"
        self.user_id = "1234"

    def login(self) -> None:
        self.calls.append("login")

    def date_range_to_timestamps(self, start: str, end: str) -> tuple[int, int]:
        return (
            int(datetime.fromisoformat(start).replace(tzinfo=CN).timestamp()),
            int(datetime.fromisoformat(f"{end}T23:59:59").replace(tzinfo=CN).timestamp()),
        )

    def fetch_key(self, key: str, start: str, end: str) -> list[dict]:
        self.calls.append(f"key:{key}")
        if key in self.failing_keys:
            raise RuntimeError(f"{key} 故意失败")
        day = datetime.fromisoformat(start).date()
        payload = {
            "steps": {"steps": 500, "distance": 400, "calories": 20},
            "calories": {"calories": 30},
            "heart_rate": {"bpm": 62, "type": 0},
            "resting_heart_rate": {"bpm": 55},
            "spo2": {"spo2": 97},
            "stress": {"stress": 40},
            "weight": {"weight": 70.0, "bmi": 23.0},
            "sleep": {
                "bedtime": ts(day.year, day.month, day.day, 0),
                "wake_up_time": ts(day.year, day.month, day.day, 8),
                "duration": 480,
                "awake_duration": 30,
                "items": [
                    {
                        "state": 1,
                        "start_time": ts(day.year, day.month, day.day, 0),
                        "end_time": ts(day.year, day.month, day.day, 1),
                    }
                ],
            },
            "abnormal_heart_beat": {},  # 无事件
        }.get(key, {})
        if key == "abnormal_heart_beat":
            return []
        return [{"time": ts(day.year, day.month, day.day), "zone_offset": 28800,
                 "value": json.dumps(payload), "sid": "s1"}]

    def fetch_sport_records(self, start: str, end: str) -> list[dict]:
        self.calls.append("sport")
        if not self.workouts:
            return []
        day = datetime.fromisoformat(start).date()
        start_ts = ts(day.year, day.month, day.day, 19)
        return [
            {
                "time": start_ts,
                "zone_offset": 28800,
                "key": "outdoor_running",
                "category": "outdoor_running",
                "value": json.dumps(
                    {
                        "start_time": start_ts,
                        "end_time": start_ts + 1800,
                        "duration": 1800,
                        "distance": 5000,
                        "avg_hrm": 145,
                        "max_hrm": 170,
                    }
                ),
            }
        ]

    def close(self) -> None:
        pass

    def probe_latest_steps(self, lookback_days: int = 1) -> int | None:
        """哨兵：返回最近的 steps 时间戳。"""
        self.calls.append("probe")
        now = datetime.now(tz=CN)
        return int(now.timestamp())


def make_engine(tmp_path, client: FakeClient) -> tuple[SyncEngine, Repository]:
    config = load_config(tmp_path / "none.yaml")
    config.paths.db = tmp_path / "e.db"
    config.paths.raw = tmp_path / "raw"
    config.paths.output = tmp_path / "out"
    config.paths.backup = tmp_path / "bak"
    config.paths.ensure()
    config.sync.recent_days = 2
    config.sync.chunk_days = 7
    config.user_id = "1234"
    config.pass_token = "token-original"

    conn = connect(config.paths.db)
    init_db(conn)
    repo = Repository(conn)
    archive = RawArchive(config.paths.raw)
    return SyncEngine(config, repo, client, archive), repo  # type: ignore[arg-type]


def test_t1_syncs_all_keys_including_workouts(tmp_path):
    """回归：运动记录必须被同步（曾经漏掉，workout 表一直是 0）。"""
    engine, repo = make_engine(tmp_path, FakeClient())
    result = engine.run("T1")

    assert result.status == "success", result.failed_keys
    counts = repo.table_counts()
    assert counts["daily_activity"] > 0
    assert counts["heart_rate_sample"] > 0
    assert counts["resting_hr"] > 0
    assert counts["sleep_session"] > 0
    assert counts["sleep_stage"] > 0
    assert counts["spo2_sample"] > 0
    assert counts["stress_sample"] > 0
    assert counts["weight_record"] > 0
    assert counts["workout"] > 0, "运动记录没有被同步——检查 _sync_workouts 是否接入流程"
    assert "workouts" in result.ok_keys


def test_workout_uses_separate_endpoint_and_is_archived(tmp_path):
    engine, repo = make_engine(tmp_path, FakeClient())
    engine.run("T1")
    rows = repo.conn.execute("SELECT id, duration_s, distance_m, avg_hr FROM workout").fetchall()
    assert rows, "workout 表为空"
    assert rows[0]["duration_s"] == 1800
    assert rows[0]["distance_m"] == 5000
    assert rows[0]["avg_hr"] == 145

    archived = repo.conn.execute(
        "SELECT key_name FROM raw_response WHERE key_name = 'workouts'"
    ).fetchall()
    assert archived, "运动记录没有归档（改版后无法重放）"


def test_repeated_sync_is_idempotent(tmp_path):
    """连跑两次，第二次不应产生新行（幂等是硬要求）。"""
    engine, repo = make_engine(tmp_path, FakeClient())
    engine.run("T1")
    first = repo.table_counts()
    engine.run("T1")
    second = repo.table_counts()
    assert first == second


def test_calories_failure_does_not_publish_partial_daily_activity(tmp_path):
    client = FakeClient()
    engine, repo = make_engine(tmp_path, client)
    repo.conn.execute(
        "INSERT INTO daily_activity (date, steps, calories, distance_m, quality_flag) "
        "VALUES (?,?,?,?,?)",
        (engine._today().isoformat(), 1234, 88.0, 900.0, "ok"),
    )
    repo.conn.commit()
    client.failing_keys.add("calories")

    result = engine.run("T1", keys=["steps", "calories"])

    assert "steps" in result.failed_keys
    row = repo.conn.execute(
        "SELECT steps, calories FROM daily_activity WHERE date = ?", (engine._today().isoformat(),)
    ).fetchone()
    assert dict(row) == {"steps": 1234, "calories": 88.0}


def test_corrupt_payload_marks_key_failed_without_overwriting_existing_row(tmp_path):
    class CorruptClient(FakeClient):
        def fetch_key(self, key: str, start: str, end: str) -> list[dict]:
            if key == "resting_heart_rate":
                day = datetime.fromisoformat(start).date()
                return [{"time": ts(day.year, day.month, day.day), "zone_offset": 28800,
                         "value": "not json", "sid": "s1"}]
            return super().fetch_key(key, start, end)

    engine, repo = make_engine(tmp_path, CorruptClient())
    today = engine._today().isoformat()
    repo.conn.execute(
        "INSERT INTO resting_hr (date, bpm, quality_flag) VALUES (?,?,?)", (today, 52, "ok")
    )
    repo.conn.commit()

    result = engine.run("T1", keys=["resting_heart_rate"])
    assert "resting_heart_rate" in result.failed_keys
    assert repo.conn.execute("SELECT bpm FROM resting_hr WHERE date = ?", (today,)).fetchone()[0] == 52


def test_one_failing_key_does_not_break_others(tmp_path):
    engine, repo = make_engine(tmp_path, FakeClient(failing_keys={"sleep"}))
    result = engine.run("T1")

    assert "sleep" in result.failed_keys
    assert result.status == "partial"
    # 其它 key 照常入库
    assert repo.table_counts()["heart_rate_sample"] > 0
    assert repo.table_counts()["workout"] > 0
    assert "steps" in result.ok_keys


def test_failure_is_recorded_in_sync_state(tmp_path):
    engine, repo = make_engine(tmp_path, FakeClient(failing_keys={"spo2"}))
    engine.run("T1")
    state = repo.get_sync_state("spo2")
    assert state["consecutive_fail"] >= 1
    assert "故意失败" in state["last_error"]


def test_auth_error_propagates_and_marks_run_failed(tmp_path):
    class AuthFail(FakeClient):
        def login(self) -> None:
            raise MiAuthError("凭据已失效")

    engine, repo = make_engine(tmp_path, AuthFail())
    with pytest.raises(MiAuthError):
        engine.run("T1")

    run = repo.recent_sync_runs(1)[0]
    assert run["status"] == "failed"
    assert "__auth__" in run["failed_keys"]


def test_sentinel_skips_when_cloud_has_no_new_data(tmp_path):
    engine, repo = make_engine(tmp_path, FakeClient())
    # 先把游标推到一个很大的值，模拟本地已是最新
    repo.mark_sync_success("steps", 99_999_999_999)
    result = engine.run_with_sentinel()
    assert result.skipped is True
    assert result.tier == "T0"


def test_keys_filter_syncs_only_requested_sources(tmp_path):
    """解析器修好后只补某一个数据源，不必全量重跑。"""
    engine, repo = make_engine(tmp_path, FakeClient())
    result = engine.run("T1", keys=["sleep"])

    counts = repo.table_counts()
    assert counts["sleep_session"] > 0
    assert counts["heart_rate_sample"] == 0
    assert counts["daily_activity"] == 0
    assert counts["workout"] == 0
    assert result.ok_keys == ["sleep"]
    assert "sport" not in engine.client.calls


def test_keys_filter_can_request_only_workouts(tmp_path):
    engine, repo = make_engine(tmp_path, FakeClient())
    engine.run("T1", keys=["workouts"])
    counts = repo.table_counts()
    assert counts["workout"] > 0
    assert counts["daily_activity"] == 0
    assert counts["sleep_session"] == 0


def test_unknown_key_filter_is_skipped_not_run(tmp_path):
    engine, _ = make_engine(tmp_path, FakeClient())
    result = engine.run("T1", keys=["not_a_thing"])
    assert result.skipped is True
    assert "没有匹配的 key" in (result.note or "")


def test_raw_archive_hash_is_stable_across_runs(tmp_path):
    """回归：归档哈希不能包含归档时间，否则去重失效、raw/ 无限增长。"""
    archive = RawArchive(tmp_path / "raw")
    records = [{"time": 1, "value": "{}"}]

    path1, hash1, count1 = archive.save("steps", 100, 200, records, endpoint="/x")
    path2, hash2, count2 = archive.save("steps", 100, 200, records, endpoint="/x")

    assert hash1 == hash2, "同样内容的哈希不稳定 → 去重失效"
    assert path1 == path2
    assert count1 == count2 == 1
    assert len(list((tmp_path / "raw").rglob("*.json.gz"))) == 1


def test_raw_archive_distinguishes_different_content(tmp_path):
    archive = RawArchive(tmp_path / "raw")
    _, hash_a, _ = archive.save("steps", 100, 200, [{"time": 1}], endpoint="/x")
    _, hash_b, _ = archive.save("steps", 100, 200, [{"time": 2}], endpoint="/x")
    assert hash_a != hash_b


def test_prune_stale_removes_mismatched_index_entries(tmp_path):
    """回归：早期版本把 archived_at 算进了哈希，会留下失配的重复归档。"""
    from mi_health_hub.sync.raw_archive import prune_stale

    archive = RawArchive(tmp_path / "raw")
    conn = connect(tmp_path / "p.db")
    init_db(conn)
    repo = Repository(conn)

    good = [{"time": 1, "value": "{}"}]
    rel, digest, count = archive.save("steps", 100, 200, good, endpoint="/x")
    repo.record_raw("steps", 100, 200, rel, digest, count)

    # 伪造一条“哈希对不上”的旧记录 + 一个会读失败的文件
    bad_file = tmp_path / "raw" / "2025" / "01" / "steps_1_2_deadbeef.json.gz"
    bad_file.parent.mkdir(parents=True, exist_ok=True)
    import gzip as _gzip

    with _gzip.open(bad_file, "wb") as handle:
        handle.write(json.dumps({"key": "steps", "records": [{"time": 9}]}).encode())
    repo.record_raw("steps", 1, 2, "2025/01/steps_1_2_deadbeef.json.gz", "not-the-right-hash", 1)
    repo.record_raw("steps", 3, 4, "2025/01/missing.json.gz", "whatever", 0)

    dry = prune_stale(repo, archive, dry_run=True)
    assert dry["checked"] == 3
    assert dry["stale"] == 1
    assert dry["missing"] == 1
    assert repo.table_counts()["raw_response"] == 3, "dry-run 不应该删任何东西"

    applied = prune_stale(repo, archive, dry_run=False)
    assert applied["stale"] == 1
    assert applied["missing"] == 1
    remaining = repo.conn.execute("SELECT file_path FROM raw_response").fetchall()
    assert [r["file_path"] for r in remaining] == [rel]
    assert not bad_file.exists()


def test_chunking_splits_long_ranges(tmp_path):
    engine, _ = make_engine(tmp_path, FakeClient())
    start = datetime(2025, 7, 1).date()
    end = datetime(2025, 7, 31).date()
    chunks = engine._chunks(start, end, 7)
    assert chunks[0] == (start, datetime(2025, 7, 7).date())
    assert chunks[-1][1] == end
    assert all((c_end - c_start).days <= 6 for c_start, c_end in chunks)
