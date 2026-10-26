"""`/api/summary` 的睡眠分期分段输出。

健康日览的睡眠时间轴直接消费 `sleep_stages`：它必须是**按 start_ts 升序的真实分段**，
并且在云端没有提供分期的日子里给出空数组而不是 500——前端据此显示「本期无分期数据」
的说明，而不是画一条空白轴。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from mi_health_hub.api.app import create_app
from mi_health_hub.config import load_config
from mi_health_hub.store.repository import connect, init_db

DAY = "2026-10-11"


def _client(tmp_path) -> tuple[TestClient, object]:
    config = load_config(tmp_path / "none.yaml")
    config.paths.db = tmp_path / "t.db"
    config.paths.raw = tmp_path / "raw"
    config.paths.output = tmp_path / "out"
    config.paths.backup = tmp_path / "bak"
    config.server.api_host = "127.0.0.1"
    return TestClient(create_app(config)), config


def _seed_sleep(config, stages: list[tuple[str, int, int]]) -> None:
    """写入一天的睡眠会话与分期分段。segments 用 (stage, start_ts, end_ts) 描述。"""
    conn = connect(config.paths.db)
    init_db(conn)
    conn.execute(
        "INSERT OR REPLACE INTO sleep_session (date, start_ts, end_ts, total_min, in_bed_min, "
        "deep_min, light_min, rem_min, awake_min, has_stage, has_rem) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 1)",
        (DAY, 1791649320, 1791679140, 497, 520, 96, 291, 110, 40),
    )
    for stage, start, end in stages:
        conn.execute(
            "INSERT OR REPLACE INTO sleep_stage (date, stage, start_ts, end_ts, minutes) "
            "VALUES (?, ?, ?, ?, ?)",
            (DAY, stage, start, end, (end - start) // 60),
        )
    conn.commit()
    conn.close()


def test_summary_returns_stage_segments_in_time_order(tmp_path):
    client, config = _client(tmp_path)
    _seed_sleep(config, [
        ("light", 1791649320, 1791650100),
        ("deep", 1791650100, 1791650340),
        ("rem", 1791650340, 1791651720),
        ("awake", 1791651720, 1791651840),
    ])

    body = client.get(f"/api/summary?date={DAY}").json()

    assert body["date"] == DAY
    assert body["sleep"]["total_min"] == 497
    assert [s["stage"] for s in body["sleep_stages"]] == ["light", "deep", "rem", "awake"]
    assert [s["start_ts"] for s in body["sleep_stages"]] == sorted(
        s["start_ts"] for s in body["sleep_stages"]
    )
    first = body["sleep_stages"][0]
    assert first["start_ts"] == 1791649320
    assert first["end_ts"] == 1791650100
    assert first["minutes"] == 13
    # 每段都要带得动「这段时间持续了多久」——时间轴与悬浮提示全靠它。
    assert all(s["end_ts"] > s["start_ts"] for s in body["sleep_stages"])


def test_summary_stage_segments_are_empty_when_cloud_has_no_stages(tmp_path):
    """有睡眠会话但云端没给分期时，必须是空列表而不是报错或 None。"""
    client, config = _client(tmp_path)
    _seed_sleep(config, [])

    body = client.get(f"/api/summary?date={DAY}").json()

    assert body["sleep"] is not None
    assert body["sleep_stages"] == []


def test_summary_is_empty_when_there_is_no_sleep_at_all(tmp_path):
    client, config = _client(tmp_path)
    conn = connect(config.paths.db)
    init_db(conn)
    conn.commit()
    conn.close()

    body = client.get(f"/api/summary?date={DAY}").json()

    assert body["sleep"] is None
    assert body["sleep_stages"] == []


def test_summary_drops_defective_segments(tmp_path):
    """零长度、时间倒置的脏分段必须被过滤，否则会把时间轴撑破。"""
    client, config = _client(tmp_path)
    _seed_sleep(config, [
        ("deep", 1791649320, 1791649320),   # 零长度
        ("light", 1791650000, 1791651000),
        ("rem", 1791652000, 1791651000),    # 时间倒置
        ("awake", 1791653000, 1791653100),
    ])

    body = client.get(f"/api/summary?date={DAY}").json()

    assert [s["stage"] for s in body["sleep_stages"]] == ["light", "awake"]


def test_summary_only_returns_the_requested_day(tmp_path):
    client, config = _client(tmp_path)
    _seed_sleep(config, [("light", 1791649320, 1791650100)])

    other = client.get("/api/summary?date=2026-10-10").json()

    assert other["sleep"] is None
    assert other["sleep_stages"] == []


@pytest.mark.parametrize("day", [DAY, None])
def test_summary_stays_reachable_with_and_without_explicit_date(tmp_path, day):
    """不传 date 时按「今天」取数，接口不应因此 500。"""
    client, config = _client(tmp_path)
    _seed_sleep(config, [("light", 1791649320, 1791650100)])

    query = f"?date={day}" if day else ""
    resp = client.get(f"/api/summary{query}")

    assert resp.status_code == 200
    assert isinstance(resp.json()["sleep_stages"], list)
