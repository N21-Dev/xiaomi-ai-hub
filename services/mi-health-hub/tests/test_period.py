"""周期统计与运动记录接口测试。"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from mi_health_hub.analysis.period import ACTIVE_BPM, VIGOROUS_BPM, build_period_stats, resolve_period
from mi_health_hub.api.app import create_app
from mi_health_hub.config import load_config
from mi_health_hub.store.repository import Repository, connect, init_db

CN_TZ = timezone(timedelta(hours=8))


def _ts(day: date, hour: int, minute: int = 0) -> int:
    return int(datetime(day.year, day.month, day.day, hour, minute, tzinfo=CN_TZ).timestamp())


@pytest.fixture()
def repo(tmp_path):
    conn = connect(tmp_path / "t.db")
    init_db(conn)
    r = Repository(conn)
    yield r
    conn.close()


def _seed_day(repo: Repository, day: date, *, steps: int, calories: float,
              sleep_min: int, hr_values: list[int], rhr: int = 50) -> None:
    repo.conn.execute(
        "INSERT INTO daily_activity (date, steps, calories, distance_m) VALUES (?,?,?,?)",
        (day.isoformat(), steps, calories, steps * 0.7),
    )
    repo.conn.execute(
        "INSERT INTO sleep_session (date, start_ts, end_ts, total_min, deep_min, rem_min, light_min, "
        "efficiency) VALUES (?,?,?,?,?,?,?,?)",
        (day.isoformat(), _ts(day - timedelta(days=1), 23), _ts(day, 7),
         sleep_min, 70, 100, sleep_min - 170, 0.9),
    )
    repo.conn.execute("INSERT INTO resting_hr (date, bpm) VALUES (?,?)", (day.isoformat(), rhr))
    # 每分钟一个心率采样
    for i, bpm in enumerate(hr_values):
        repo.conn.execute(
            "INSERT OR REPLACE INTO heart_rate_sample (ts, bpm, type) VALUES (?,?,0)",
            (_ts(day, 8, i), bpm),
        )
    repo.conn.commit()


def test_resolve_period_week_and_month():
    ref = date(2026, 9, 21)  # 周一
    w = resolve_period("weekly", ref)
    assert (w.start, w.end) == (date(2026, 9, 21), date(2026, 9, 27))
    assert w.previous.start == date(2026, 9, 14)
    m = resolve_period("monthly", date(2026, 9, 21))
    assert (m.start, m.end) == (date(2026, 9, 1), date(2026, 9, 30))
    assert m.previous.start == date(2026, 8, 1)
    assert m.previous.end == date(2026, 8, 31)
    d = resolve_period("daily", date(2026, 9, 21))
    assert d.days == 1 and d.previous.start == date(2026, 9, 20)


def test_period_metrics_and_hr_derived_minutes(repo):
    day = date(2026, 9, 21)
    # 10 分钟采样：3 个 ≥140（高强度），4 个 100-139（活动），3 个 <100
    hr = [VIGOROUS_BPM + 10] * 3 + [ACTIVE_BPM + 5] * 4 + [60] * 3
    _seed_day(repo, day, steps=10000, calories=1100, sleep_min=420, hr_values=hr)
    stats = build_period_stats(repo, "daily", day)
    cur = stats["current"]
    assert cur["steps_total"] == 10000
    assert cur["calories_avg"] == 1100
    assert cur["sleep_min_avg"] == 420
    assert cur["active_min_total"] == 7  # ≥100 的分钟数（含高强度）
    assert cur["vigorous_min_total"] == 3
    curve = stats["hr_curve"]
    assert len(curve["labels"]) == len(curve["bpm"]) == 1440
    assert curve["labels"][0] == "00:00"
    assert curve["labels"][-1] == "23:59"
    assert curve["range"] == ["00:00", "23:59"]
    assert curve["sample_count"] == len(hr)
    non_null = [(i, value) for i, value in enumerate(curve["bpm"]) if value is not None]
    assert [value for _, value in non_null] == hr
    assert [i for i, _ in non_null] == list(range(8 * 60, 8 * 60 + len(hr)))
    assert curve["bpm"][-1] is None


def test_period_comparison_and_series(repo):
    week_start = date(2026, 9, 21)  # 本周一
    for i in range(7):
        _seed_day(repo, week_start + timedelta(days=i), steps=1000 * (i + 1),
                  calories=100 * (i + 1), sleep_min=400, hr_values=[80] * 5)
    # 上周只放一天，步数应明显低于本周
    _seed_day(repo, week_start - timedelta(days=3), steps=100, calories=10,
              sleep_min=300, hr_values=[80])
    stats = build_period_stats(repo, "weekly", week_start, as_of=date(2026, 10, 1))
    assert stats["days"] == 7
    assert stats["current"]["steps_total"] == 28000
    assert stats["current"]["steps_avg"] == 4000
    assert stats["previous"]["steps_total"] == 100
    assert len(stats["series"]["steps"]) == 7
    assert stats["series"]["dates"][0] == "2026-09-21"


def test_current_week_compares_only_same_elapsed_days(repo):
    week_start = date(2026, 9, 21)
    # 本周只有周一、周二已经发生；未来 5 天即使库中误有数据也不应进入当前统计。
    for i in range(7):
        _seed_day(repo, week_start + timedelta(days=i), steps=1000, calories=100,
                  sleep_min=400, hr_values=[80])
    previous_start = week_start - timedelta(days=7)
    for i in range(7):
        _seed_day(repo, previous_start + timedelta(days=i), steps=100, calories=10,
                  sleep_min=300, hr_values=[80])

    stats = build_period_stats(repo, "weekly", week_start, as_of=date(2026, 9, 22))
    assert stats["is_partial"] is True
    assert stats["elapsed_days"] == 2
    assert stats["current"]["steps_total"] == 2000
    assert stats["previous"]["steps_total"] == 200
    assert stats["previous_range"]["comparison_end"] == "2026-09-15"
    assert len(stats["series_prev"]["steps"]) == 2


def test_suspect_values_and_pure_nap_do_not_enter_period_metrics(repo):
    day = date(2026, 9, 21)
    repo.conn.execute(
        "INSERT INTO daily_activity (date, steps, calories, distance_m, quality_flag) VALUES (?,?,?,?,?)",
        (day.isoformat(), 250000, 9999, 1000, "suspect"),
    )
    repo.conn.execute(
        "INSERT INTO heart_rate_sample (ts, bpm, type, quality_flag) VALUES (?,?,?,?)",
        (_ts(day, 9), 300, 0, "suspect"),
    )
    repo.conn.execute(
        "INSERT INTO sleep_session (date, start_ts, end_ts, total_min, nap_min, is_nap, quality_flag) "
        "VALUES (?,?,?,?,?,?,?)",
        (day.isoformat(), _ts(day, 13), _ts(day, 14), 30, 30, 1, "ok"),
    )
    repo.conn.commit()

    stats = build_period_stats(repo, "daily", day, as_of=day)
    cur = stats["current"]
    assert cur["steps_total"] is None
    assert cur["calories_total"] is None
    assert cur["active_min_total"] is None
    assert cur["sleep_min_total"] is None
    assert cur["nap_min_total"] == 30
    assert stats["hr_curve"]["sample_count"] == 0


def test_empty_period_keeps_missing_metrics_as_null(repo):
    cur = build_period_stats(repo, "daily", date(2026, 9, 21), as_of=date(2026, 9, 21))["current"]
    assert cur["steps_total"] is None
    assert cur["calories_total"] is None
    assert cur["active_min_total"] is None
    assert cur["sleep_min_total"] is None
    assert cur["workout_min_total"] is None


def _seed_night_with_stages(repo: Repository, day: date, *,
                            stages=(("deep", 20), ("light", 30), ("rem", 10))) -> None:
    """一晚长睡眠会话 + 与之对齐的分期分段（23:00 入睡，逐段相接）。"""
    repo.conn.execute(
        "INSERT INTO sleep_session (date, start_ts, end_ts, total_min, deep_min, rem_min, light_min, "
        "efficiency, has_stage, has_rem) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (day.isoformat(), _ts(day - timedelta(days=1), 23), _ts(day, 7), 420, 70, 100, 250, 0.9, 1, 1),
    )
    start = _ts(day - timedelta(days=1), 23)
    for stage, minutes in stages:
        repo.conn.execute(
            "INSERT OR REPLACE INTO sleep_stage (date, stage, start_ts, end_ts, minutes) VALUES (?,?,?,?,?)",
            (day.isoformat(), stage, start, start + minutes * 60, minutes),
        )
        start += minutes * 60
    repo.conn.commit()


def test_weekly_period_returns_per_night_sleep_stages(repo):
    week_start = date(2026, 9, 21)
    _seed_night_with_stages(repo, week_start)
    _seed_night_with_stages(repo, week_start + timedelta(days=1),
                            stages=(("awake", 5), ("deep", 40)))
    stats = build_period_stats(repo, "weekly", week_start, as_of=date(2026, 10, 1))

    stages = stats["sleep_stages"]
    assert sorted(stages) == ["2026-09-21", "2026-09-22"]
    first = stages["2026-09-21"]
    assert [seg["stage"] for seg in first] == ["deep", "light", "rem"]
    # 段与段首尾相接，start_ts 升序
    assert first[0]["end_ts"] == first[1]["start_ts"]
    assert all(set(seg) == {"stage", "start_ts", "end_ts", "minutes"} for seg in first)
    # 当期没有分期的日期不出现键
    assert "2026-09-25" not in stages
    # 日报不附加该字段（日览的分期由 /api/summary 提供）
    assert "sleep_stages" not in build_period_stats(repo, "daily", week_start, as_of=week_start)


def test_period_sleep_stages_drop_dirty_segments(repo):
    day = date(2026, 9, 21)
    _seed_night_with_stages(repo, day)
    base = _ts(day - timedelta(days=1), 23)
    repo.conn.execute(
        "INSERT OR REPLACE INTO sleep_stage (date, stage, start_ts, end_ts, minutes) VALUES (?,?,?,?,?)",
        (day.isoformat(), "light", base + 3600, base + 3600, 0),  # 零长度
    )
    repo.conn.execute(
        "INSERT OR REPLACE INTO sleep_stage (date, stage, start_ts, end_ts, minutes) VALUES (?,?,?,?,?)",
        (day.isoformat(), "rem", base + 7200, base + 3600, 30),  # 时间倒置
    )
    repo.conn.commit()

    stages = build_period_stats(repo, "weekly", day, as_of=date(2026, 10, 1))["sleep_stages"]
    assert [seg["stage"] for seg in stages[day.isoformat()]] == ["deep", "light", "rem"]


def test_empty_period_sleep_stages_is_empty_dict(repo):
    stats = build_period_stats(repo, "monthly", date(2026, 9, 21), as_of=date(2026, 10, 1))
    assert stats["sleep_stages"] == {}


def test_api_stats_period_weekly_includes_sleep_stages(tmp_path):
    config = load_config(tmp_path / "none.yaml")
    config.paths.db = tmp_path / "api.db"
    client = TestClient(create_app(config))
    body = client.get("/api/stats/period?kind=weekly&date=2026-09-21").json()
    assert body["sleep_stages"] == {}


def test_api_stats_period_and_workouts(tmp_path):
    config = load_config(tmp_path / "none.yaml")
    config.paths.db = tmp_path / "api.db"
    app = create_app(config)
    client = TestClient(app)

    assert client.get("/api/stats/period?kind=bogus").status_code == 400
    ok = client.get("/api/stats/period?kind=weekly&date=2026-09-21")
    assert ok.status_code == 200
    body = ok.json()
    assert body["kind"] == "weekly" and body["days"] == 7
    assert "current" in body and "previous" in body and "series" in body

    # 空库的运动目录
    w = client.get("/api/workouts?limit=10")
    assert w.status_code == 200
    assert w.json()["items"] == [] and w.json()["total"] == 0
    assert client.get("/api/workouts/nope").status_code == 404


def test_workouts_list_has_cn_type_and_pace(tmp_path):
    config = load_config(tmp_path / "none.yaml")
    config.paths.db = tmp_path / "w.db"
    conn = connect(config.paths.db)
    init_db(conn)
    conn.execute(
        "INSERT INTO workout (id, type, start_ts, end_ts, duration_s, distance_m, calories, "
        "avg_pace_s, pace_basis, turn_count, avg_swolf, best_swolf, stroke_count) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("w1", "swimming", 1789785918, 1789789154, 3234, 650.0, 203.0, 497.0, "100m", 13, 260.0, 85.0, 254),
    )
    conn.commit()
    conn.close()

    client = TestClient(create_app(config))
    item = client.get("/api/workouts").json()["items"][0]
    assert item["type_cn"] == "游泳"
    assert item["avg_pace_text"] == "8:17/100米"
    assert item["lane_m"] == 50
    assert item["per_lane_pace_text"] == "4:08"
    detail = client.get("/api/workouts/w1").json()
    assert detail["best_swolf"] == 85.0
    assert detail["stroke_count"] == 254
