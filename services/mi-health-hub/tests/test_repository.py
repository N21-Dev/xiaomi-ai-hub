"""仓储层测试。重点是**幂等**：小米会回溯修正历史数据，重复同步必须安全。"""

from __future__ import annotations

from mi_health_hub.store.repository import Repository, connect, init_db


def make_repo(tmp_path) -> Repository:
    conn = connect(tmp_path / "test.db")
    init_db(conn)
    return Repository(conn)


def activity_row(date: str = "2026-09-15", steps: int = 8000) -> dict:
    return {
        "date": date,
        "steps": steps,
        "distance_m": 6000.0,
        "calories": 300.0,
        "quality_flag": "ok",
        "source": "cloud",
        "updated_at": 1,
    }


def sleep_row(date: str = "2026-09-15", total: int = 425) -> dict:
    return {
        "date": date,
        "start_ts": 1,
        "end_ts": 2,
        "total_min": total,
        "in_bed_min": 450,
        "deep_min": 60,
        "light_min": 120,
        "rem_min": 60,
        "awake_min": 15,
        "nap_min": 0,
        "efficiency": 0.94,
        "score": 82,
        "is_nap": 0,
        "avg_hr": 56,
        "min_hr": 47,
        "max_hr": 105,
        "avg_spo2": 96.0,
        "min_spo2": 90,
        "max_spo2": 99,
        "avg_breath": 15.2,
        "awake_cnt": 3,
        "has_stage": 1,
        "has_rem": 1,
        "alg_version": 5,
        "quality_flag": "ok",
        "source": "cloud",
        "updated_at": 1,
        "stages": [
            {"stage": "deep", "start_ts": 100, "end_ts": 200, "minutes": 60},
            {"stage": "light", "start_ts": 200, "end_ts": 300, "minutes": 60},
        ],
    }


def test_schema_applies_and_integrity_is_ok(tmp_path):
    repo = make_repo(tmp_path)
    assert repo.integrity_check() == "ok"


def test_upserting_same_activity_twice_does_not_duplicate(tmp_path):
    repo = make_repo(tmp_path)
    repo.upsert_daily_activity([activity_row()])
    first = repo.table_counts()["daily_activity"]
    repo.upsert_daily_activity([activity_row()])
    assert repo.table_counts()["daily_activity"] == first == 1


def test_later_value_overwrites_earlier_one(tmp_path):
    """小米回溯修正：后拉到的值必须覆盖旧值。"""
    repo = make_repo(tmp_path)
    repo.upsert_daily_activity([activity_row(steps=8000)])
    repo.upsert_daily_activity([activity_row(steps=8500)])
    row = repo.conn.execute("SELECT steps FROM daily_activity WHERE date = '2026-09-15'").fetchone()
    assert row["steps"] == 8500


def test_sleep_upsert_rewrites_stages_without_duplicating(tmp_path):
    repo = make_repo(tmp_path)
    repo.upsert_sleep([sleep_row()])
    repo.upsert_sleep([sleep_row()])
    assert repo.table_counts()["sleep_session"] == 1
    assert repo.table_counts()["sleep_stage"] == 2  # 分期是整体重写，不是累加


def test_sleep_stage_rewrite_replaces_changed_segments(tmp_path):
    repo = make_repo(tmp_path)
    repo.upsert_sleep([sleep_row()])
    changed = sleep_row()
    changed["stages"] = [{"stage": "deep", "start_ts": 100, "end_ts": 400, "minutes": 300}]
    repo.upsert_sleep([changed])
    rows = repo.conn.execute("SELECT stage, minutes FROM sleep_stage").fetchall()
    assert len(rows) == 1
    assert rows[0]["minutes"] == 300


def test_heart_rate_upsert_is_idempotent_on_composite_key(tmp_path):
    repo = make_repo(tmp_path)
    rows = [
        {"ts": 1, "bpm": 60, "type": 0, "quality_flag": "ok"},
        {"ts": 1, "bpm": 65, "type": 1, "quality_flag": "ok"},
    ]
    repo.upsert_heart_rate(rows)
    repo.upsert_heart_rate(rows)
    assert repo.table_counts()["heart_rate_sample"] == 2
    # 同 ts 不同 type 视为两条，同 (ts,type) 则覆盖
    repo.upsert_heart_rate([{"ts": 1, "bpm": 70, "type": 0, "quality_flag": "ok"}])
    bpm = repo.conn.execute(
        "SELECT bpm FROM heart_rate_sample WHERE ts = 1 AND type = 0"
    ).fetchone()["bpm"]
    assert bpm == 70


def test_sync_state_tracks_failures_and_resets_on_success(tmp_path):
    repo = make_repo(tmp_path)
    repo.mark_sync_failure("steps", "boom")
    repo.mark_sync_failure("steps", "boom again")
    state = repo.get_sync_state("steps")
    assert state["consecutive_fail"] == 2
    assert "boom again" in state["last_error"]

    repo.mark_sync_success("steps", 12345)
    state = repo.get_sync_state("steps")
    assert state["consecutive_fail"] == 0
    assert state["last_error"] is None
    assert state["cursor_ts"] == 12345


def test_sync_success_without_cursor_keeps_previous_cursor(tmp_path):
    repo = make_repo(tmp_path)
    repo.mark_sync_success("steps", 999)
    repo.mark_sync_success("steps", None)
    assert repo.get_sync_state("steps")["cursor_ts"] == 999


def test_sync_run_status_reflects_partial_failure(tmp_path):
    repo = make_repo(tmp_path)
    run_id = repo.start_sync_run("T1")
    repo.finish_sync_run(run_id, ["steps"], {"sleep": "timeout"}, 12)
    run = repo.recent_sync_runs(1)[0]
    assert run["status"] == "partial"
    assert run["request_count"] == 12


def test_anomaly_event_upsert_deduplicates(tmp_path):
    repo = make_repo(tmp_path)
    event = {
        "date": "2026-09-15",
        "ts": 100,
        "kind": "abnormal_heart_beat",
        "severity": "warn",
        "message": "x",
        "payload_json": "{}",
    }
    repo.upsert_anomaly_events([event])
    repo.upsert_anomaly_events([event])
    assert repo.table_counts()["anomaly_event"] == 1


def test_workout_upsert_idempotent(tmp_path):
    repo = make_repo(tmp_path)
    row = {
        "id": "w1",
        "type": "swimming",
        "start_ts": 1,
        "end_ts": 2,
        "duration_s": 1800,
        "distance_m": 650.0,
        "calories": 203.0,
        "avg_hr": 121,
        "max_hr": 163,
        "steps": None,
        "quality_flag": "ok",
        "raw_json": "{}",
        "source": "cloud",
        "updated_at": 1,
    }
    repo.upsert_workouts([row])
    repo.upsert_workouts([row])
    assert repo.table_counts()["workout"] == 1


def test_workout_accepts_rows_without_optional_extras(tmp_path):
    """老数据/部分运动类型没有训练负荷、游泳专项字段，缺键不能让 upsert 崩掉。"""
    repo = make_repo(tmp_path)
    repo.upsert_workouts([
        {
            "id": "w2",
            "type": "running",
            "start_ts": 1,
            "end_ts": 2,
            "duration_s": 600,
            "distance_m": 1500.0,
            "calories": 90.0,
            "avg_hr": 150,
            "max_hr": 175,
            "quality_flag": "ok",
            "raw_json": "{}",
            "source": "cloud",
            "updated_at": 1,
        }
    ])
    assert repo.table_counts()["workout"] == 1


def test_workout_extras_are_persisted(tmp_path):
    repo = make_repo(tmp_path)
    repo.upsert_workouts([
        {
            "id": "w3",
            "type": "swimming",
            "start_ts": 1,
            "end_ts": 2,
            "duration_s": 3234,
            "distance_m": 650.0,
            "calories": 203.0,
            "total_cal": 222.0,
            "avg_hr": 121,
            "max_hr": 163,
            "min_hr": 88,
            "avg_pace_s": 497.0,
            "max_pace_s": 124.0,
            "pace_basis": "100m",
            "train_effect": 2.2,
            "train_load": 28.0,
            "recover_time_h": 12,
            "stroke_count": 254,
            "turn_count": 13,
            "zone_warmup_s": 1518,
            "quality_flag": "ok",
            "raw_json": "{}",
            "source": "cloud",
            "updated_at": 1,
        }
    ])
    row = repo.conn.execute("SELECT * FROM workout WHERE id = 'w3'").fetchone()
    assert row["pace_basis"] == "100m"
    assert row["avg_pace_s"] == 497.0
    assert row["stroke_count"] == 254
    assert row["turn_count"] == 13
    assert row["train_load"] == 28.0
    assert row["zone_warmup_s"] == 1518


def test_vacuum_into_creates_readable_backup(tmp_path):
    repo = make_repo(tmp_path)
    repo.upsert_daily_activity([activity_row()])
    target = tmp_path / "backup" / "copy.db"
    repo.vacuum_into(target)
    assert target.is_file()

    restored = connect(target)
    count = restored.execute("SELECT COUNT(*) FROM daily_activity").fetchone()[0]
    assert count == 1


def test_deprecated_pace_column_is_dropped(tmp_path):
    """旧的 avg_pace_s_km 语义是错的（对游泳差 10 倍），升级时应被删掉。"""
    db = tmp_path / "old.db"
    conn = connect(db)
    init_db(conn)                     # 建出完整结构
    conn.execute("ALTER TABLE workout ADD COLUMN avg_pace_s_km REAL")   # 模拟旧库的遗留列
    conn.execute(
        "INSERT INTO workout (id, type, start_ts, end_ts, avg_pace_s_km) "
        "VALUES ('w', 'swimming', 1, 2, 497)"
    )

    init_db(conn)                     # 再次升级
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(workout)")}
    assert "avg_pace_s_km" not in cols
    assert "avg_pace_s" in cols and "pace_basis" in cols
    # 删列不能把行丢掉
    assert conn.execute("SELECT COUNT(*) FROM workout").fetchone()[0] == 1


def test_coverage_is_refreshed_for_every_source(tmp_path):
    """回归：data_coverage 曾经永远是 0 行（调用方与实现支持的 key 对不上）。"""
    repo = make_repo(tmp_path)
    repo.upsert_daily_activity([activity_row()])
    repo.refresh_coverage("2026-09-15", "steps")
    repo.refresh_coverage("2026-09-15", "sleep")
    coverage = repo.coverage_range("2026-09-15", "2026-09-15")
    keys = {row["key_name"] for row in coverage}
    assert keys == {"steps", "sleep"}
    assert all(row["sample_count"] >= 0 for row in coverage)


def test_coverage_counts_samples_for_sample_tables(tmp_path):
    repo = make_repo(tmp_path)
    repo.upsert_heart_rate(
        [
            {"ts": 1789785918, "bpm": 60, "type": 0, "quality_flag": "ok"},
            {"ts": 1789785978, "bpm": 62, "type": 0, "quality_flag": "ok"},
        ]
    )
    from datetime import datetime, timedelta, timezone

    from mi_health_hub.analysis.daily import CN_TZ

    day = datetime.fromtimestamp(1789785918, tz=CN_TZ).strftime("%Y-%m-%d")
    repo.refresh_coverage(day, "heart_rate")
    row = repo.conn.execute(
        "SELECT * FROM data_coverage WHERE date = ? AND key_name = 'heart_rate'", (day,)
    ).fetchone()
    assert row is not None
    assert row["sample_count"] == 2
    assert row["first_ts"] == 1789785918
    assert row["last_ts"] == 1789785978


def test_raw_response_deduplicates_by_content_hash(tmp_path):
    repo = make_repo(tmp_path)
    for _ in range(2):
        repo.record_raw("steps", 1, 2, "2026/09/a.json.gz", "hash1", 10)
    assert repo.table_counts()["raw_response"] == 1
