"""API 鉴权规则测试。

背景：经 docker 端口映射后，uvicorn 看到的客户端 IP 是网桥网关（如 172.22.0.1），
不是 127.0.0.1。如果只认回环，连本机访问都会被拒——这是实际踩过的坑。
"""

from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient

from mi_health_hub.api.app import _is_trusted, create_app
from mi_health_hub.config import Config, load_config


@pytest.mark.parametrize(
    "host,trust_private,expected",
    [
        ("127.0.0.1", True, True),
        ("127.0.0.1", False, True),          # 回环始终可信
        ("::1", True, True),
        ("172.22.0.1", True, True),          # docker 网桥网关
        ("172.22.0.1", False, False),        # 关闭私有网段信任后不再豁免
        ("192.0.2.10", True, True),
        ("10.0.0.5", True, True),
        ("169.254.1.1", True, True),         # link-local
        ("8.8.8.8", True, False),            # 公网
        ("8.8.8.8", False, False),
        ("testclient", True, True),
        ("not-an-ip", True, False),
        ("unknown", True, True),
    ],
)
def test_trust_rules(host, trust_private, expected):
    assert _is_trusted(host, trust_private) is expected


def _app(tmp_path, **server_overrides):
    config: Config = load_config(tmp_path / "none.yaml")
    config.paths.db = tmp_path / "t.db"
    config.paths.raw = tmp_path / "raw"
    config.paths.output = tmp_path / "out"
    config.paths.backup = tmp_path / "bak"
    config.server.api_host = "127.0.0.1"
    for key, value in server_overrides.items():
        setattr(config.server, key, value)
    return create_app(config)


def test_trusted_client_reaches_status_without_key(tmp_path):
    client = TestClient(_app(tmp_path))
    assert client.get("/api/admin/status").status_code == 200


def test_healthz_never_requires_auth(tmp_path):
    client = TestClient(_app(tmp_path))
    assert client.get("/healthz").status_code == 200
    assert client.get("/healthz").json()["ok"] is True


def test_index_page_served_without_auth(tmp_path):
    resp = TestClient(_app(tmp_path)).get("/")
    assert resp.status_code == 200
    assert "健康空间" in resp.text


def test_status_reports_credentials_not_configured(tmp_path):
    body = TestClient(_app(tmp_path)).get("/api/admin/status").json()
    assert body["credentials_configured"] is False
    assert "integrity" in body


def test_status_reports_whether_auth_is_required(tmp_path):
    """局域网可信模式下前端要据此隐藏 Key 输入框。"""
    trusted = TestClient(_app(tmp_path, trust_private=True)).get("/api/admin/status").json()
    assert trusted["auth_required"] is False
    assert trusted["trust_private"] is True

    strict = TestClient(_app(tmp_path, trust_private=False)).get("/api/admin/status").json()
    assert strict["auth_required"] is True


def test_trusted_lan_client_can_read_without_key(tmp_path):
    """信任局域网时的预期行为：不给 Key 也能读数据。"""
    client = TestClient(_app(tmp_path, trust_private=True))
    assert client.get("/api/admin/status").status_code == 200
    assert client.get("/api/summary").status_code == 200


def test_remote_client_blocked_when_no_api_key_configured(tmp_path):
    client = TestClient(_app(tmp_path, trust_private=False), client=("8.8.8.8", 50000))
    resp = client.get("/api/admin/status", headers={"X-Forwarded-For": "8.8.8.8"})
    assert resp.status_code == 403


def test_api_key_enforced_for_untrusted_source(tmp_path):
    app = _app(tmp_path, trust_private=False, api_key="s3cret")
    client = TestClient(app, client=("8.8.8.8", 50000))
    assert client.get("/api/admin/status").status_code == 401
    assert client.get("/api/admin/status", headers={"X-API-Key": "wrong"}).status_code == 401
    assert client.get("/api/admin/status", headers={"X-Forwarded-For": "127.0.0.1"}).status_code == 401
    ok = client.get("/api/admin/status", headers={"X-API-Key": "s3cret"})
    assert ok.status_code == 200


def test_unknown_metric_returns_404(tmp_path):
    client = TestClient(_app(tmp_path))
    resp = client.get("/api/series/whatever?start=2026-01-01&end=2026-01-02")
    assert resp.status_code == 404


def test_ai_daily_endpoint_returns_analysis_payload(tmp_path):
    """供 AI 解读的摘要：必须带数据质量、基线、偏移与规则命中。"""
    client = TestClient(_app(tmp_path))
    resp = client.get("/api/ai/daily?date=2026-09-19")
    assert resp.status_code == 200
    body = resp.json()
    for field in ("date", "data_quality", "today", "baseline_30d", "delta",
                  "trend_7d", "rule_hits", "insufficient_data"):
        assert field in body, f"缺少字段 {field}"
    assert body["date"] == "2026-09-19"
    # 设备没有 HRV，必须永远在「数据不足」列表里
    assert "hrv" in body["insufficient_data"]
    # 空库时应明确说明没数据，而不是给出 0 值
    assert set(body["data_quality"]["missing"]) >= {"sleep", "activity"}


def test_ai_daily_marks_empty_database_honestly(tmp_path):
    """没有数据时不能编造 0 值 —— 缺失要显式体现为 null + missing 列表。"""
    client = TestClient(_app(tmp_path))
    body = client.get("/api/ai/daily?date=2026-09-19").json()
    assert body["today"]["sleep"] is None
    assert body["today"]["resting_hr"] is None
    assert body["today"]["activity"] is None
    assert body["data_quality"]["has_sleep"] is False


def test_ai_daily_sleep_block_exposes_new_fields(tmp_path):
    """睡眠块要把血氧 min/max、呼吸率、has_rem 一并带出，模型才读得全。"""
    from datetime import datetime, timedelta, timezone

    from mi_health_hub.analysis.daily import CN_TZ

    app = _app(tmp_path)
    # 直接写一条睡眠记录再取摘要
    from mi_health_hub.store.repository import Repository, connect, init_db

    conn = connect(tmp_path / "t.db")
    init_db(conn)
    repo = Repository(conn)
    repo.upsert_sleep([
        {
            "date": "2026-09-19",
            "start_ts": int(datetime(2026, 9, 19, 0, 35, tzinfo=CN_TZ).timestamp()),
            "end_ts": int(datetime(2026, 9, 19, 8, 31, tzinfo=CN_TZ).timestamp()),
            "total_min": 473, "in_bed_min": 483, "deep_min": 73, "light_min": 241,
            "rem_min": 159, "awake_min": 3, "nap_min": 0, "efficiency": 0.97,
            "score": None, "is_nap": 0, "avg_hr": 49, "min_hr": 40, "max_hr": 85,
            "avg_spo2": 98.0, "min_spo2": 96, "max_spo2": 99, "avg_breath": 15.2,
            "awake_cnt": 1, "has_stage": 1, "has_rem": 1, "alg_version": 5,
            "quality_flag": "ok", "source": "cloud", "updated_at": 1, "stages": [],
        }
    ])
    conn.close()

    body = TestClient(app).get("/api/ai/daily?date=2026-09-19").json()
    sleep = body["today"]["sleep"]
    assert sleep["min_spo2"] == 96
    assert sleep["max_spo2"] == 99
    assert sleep["avg_breath"] == 15.2
    assert sleep["has_rem"] == 1
    assert sleep["bedtime"] == "00:35"
    assert sleep["wake_time"] == "08:31"
    # 卧床 483 与实际睡眠 473 必须区分开
    assert sleep["in_bed_min"] == 483 and sleep["total_min"] == 473


# ------------------------------------------------------------------ 线程安全
class _ThreadGuard:
    """SQLite 连接代理：一旦在创建线程之外被使用就立刻报错。

    用来钉住「连接必须在端点自己的线程里创建」这条约束。
    """

    def __init__(self, conn) -> None:
        self._conn = conn
        self._owner = threading.get_ident()

    def __getattr__(self, name):  # noqa: ANN001
        attr = getattr(self._conn, name)
        if not callable(attr):
            return attr

        def guarded(*args, **kwargs):  # noqa: ANN002, ANN003
            current = threading.get_ident()
            if current != self._owner:
                raise AssertionError(
                    f"SQLite 连接在创建线程({self._owner})之外的线程({current})被使用"
                )
            return attr(*args, **kwargs)

        return guarded


def test_api_never_uses_connection_across_threads(tmp_path, monkeypatch):
    """回归：FastAPI 在线程池里执行同步的 `yield` 依赖，连接会跨线程使用。

    症状是**间歇性 500**（单独 curl 正常、页面并发就挂），很难查。
    这里用代理连接把约束钉死：连接必须在端点线程内创建和使用。
    """
    from mi_health_hub.api import app as app_module

    real_connect = app_module.connect
    guarded_conns: list[_ThreadGuard] = []

    def spy_connect(path):  # noqa: ANN001
        guard = _ThreadGuard(real_connect(path))
        guarded_conns.append(guard)
        return guard

    monkeypatch.setattr(app_module, "connect", spy_connect)

    client = TestClient(_app(tmp_path))
    for path in (
        "/api/admin/status",
        "/api/summary",
        "/api/series/steps?start=2026-01-01&end=2026-12-31",
        "/api/series/resting_hr?start=2026-01-01&end=2026-12-31",
        "/api/coverage?start=2026-01-01&end=2026-12-31",
        "/api/anomalies",
        "/api/workouts",
    ):
        resp = client.get(path)
        assert resp.status_code == 200, f"{path} → HTTP {resp.status_code}"

    assert guarded_conns, "没有捕获到连接，测试本身失效了"


def test_concurrent_requests_all_succeed(tmp_path, monkeypatch):
    """并发几十个请求，全部应返回 200。

    旧实现（yield 依赖建连接）在这种并发下会大量 500。
    """
    import asyncio

    import httpx

    from mi_health_hub.api import app as app_module

    real_connect = app_module.connect

    def spy_connect(path):  # noqa: ANN001
        return _ThreadGuard(real_connect(path))

    monkeypatch.setattr(app_module, "connect", spy_connect)
    app = _app(tmp_path)

    paths = [
        "/api/admin/status",
        "/api/summary",
        "/api/series/steps?start=2026-01-01&end=2026-12-31",
        "/api/series/resting_hr?start=2026-01-01&end=2026-12-31",
        "/api/coverage?start=2026-01-01&end=2026-12-31",
        "/api/anomalies",
        "/api/workouts",
    ] * 6

    async def fire() -> list[int]:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            responses = await asyncio.gather(*(client.get(p) for p in paths))
            return [r.status_code for r in responses]

    codes = asyncio.run(fire())
    failures = sorted({code for code in codes if code != 200})
    assert not failures, f"并发下出现非 200 响应: {failures}"
