"""手动「立即同步」端点：触发、状态轮询、并发互斥。"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from mi_health_hub.api.app import _sync_state, create_app
from mi_health_hub.config import load_config


@pytest.fixture(autouse=True)
def _reset_sync_state():
    yield
    _sync_state.update(
        running=False, started_at=None, finished_at=None, ok=None, rows=0, note=""
    )


def _app(tmp_path):
    config = load_config(tmp_path / "none.yaml")
    config.paths.db = tmp_path / "t.db"
    return create_app(config)


def test_sync_trigger_and_status(tmp_path):
    client = TestClient(_app(tmp_path))
    resp = client.post("/api/admin/sync")
    assert resp.status_code == 202
    assert resp.json()["started"] is True

    # 等后台线程结束（无凭据 → 客户端登录失败 → 快速失败落状态）
    deadline = time.time() + 15
    st = {"running": True}
    while time.time() < deadline:
        st = client.get("/api/admin/sync/status").json()
        if not st["running"]:
            break
        time.sleep(0.1)
    assert st["running"] is False
    assert st["ok"] is False  # 无凭据必然失败
    assert st["note"]  # 带错误说明
    assert st["finished_at"] is not None


def test_manual_sync_forces_t1_without_sentinel(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from mi_health_hub.api import app as app_mod

    calls: list[str] = []

    class FakeEngine:
        client = SimpleNamespace(close=lambda: None)

        def run(self, tier):
            calls.append(tier)
            return SimpleNamespace(
                tier=tier,
                status="success",
                rows_written=7,
                failed_keys={},
                note=None,
            )

        def run_with_sentinel(self):
            raise AssertionError("手动同步不应调用哨兵")

        def backup_database(self):
            return None

    monkeypatch.setattr(app_mod, "build_engine", lambda cfg, repo: FakeEngine())
    client = TestClient(_app(tmp_path))
    assert client.post("/api/admin/sync").status_code == 202

    deadline = time.time() + 5
    while time.time() < deadline and _sync_state["running"]:
        time.sleep(0.05)

    assert calls == ["T1"]
    assert _sync_state["ok"] is True
    assert _sync_state["rows"] == 7
    assert "tier=T1" in _sync_state["note"]


def test_sync_trigger_conflict_while_running(tmp_path, monkeypatch):
    import threading

    from mi_health_hub.api import app as app_mod
    from mi_health_hub.scheduler import _sync_lock

    ev = threading.Event()

    def _blocked(cfg):  # noqa: ARG001
        ev.wait(5)
        _sync_lock.release()

    monkeypatch.setattr(app_mod, "_run_manual_sync", _blocked)
    client = TestClient(_app(tmp_path))

    r1 = client.post("/api/admin/sync")
    assert r1.status_code == 202
    # 后台线程还挂着（ev 未 set）→ state.running=True → 409
    r2 = client.post("/api/admin/sync")
    assert r2.status_code == 409
    ev.set()
    deadline = time.time() + 5
    while time.time() < deadline and _sync_state["running"]:
        time.sleep(0.05)
