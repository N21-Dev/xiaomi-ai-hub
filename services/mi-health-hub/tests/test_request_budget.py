from concurrent.futures import ThreadPoolExecutor
from datetime import date

import pytest

from mi_health_hub.cloud.client import MiRateLimitError, RequestBudget
from mi_health_hub.cli import _make_client
from mi_health_hub.config import load_config
from mi_health_hub.store.repository import Repository, connect, init_db
from mi_health_hub.sync.engine import build_engine


def test_budget_survives_new_instances_and_reset(tmp_path):
    path = tmp_path / "quota.sqlite3"
    first = RequestBudget(2, storage_path=path, account="cn:test")
    first.consume()
    second = RequestBudget(2, storage_path=path, account="cn:test")
    second.consume()
    second.reset()
    with pytest.raises(MiRateLimitError):
        second.consume()
    assert first.count == 1
    assert path.stat().st_mode & 0o777 == 0o600


def test_day_rollover_and_account_isolation(tmp_path, monkeypatch):
    path = tmp_path / "quota.sqlite3"
    monkeypatch.setattr(RequestBudget, "_today", staticmethod(lambda: date(2026, 10, 6)))
    budget = RequestBudget(1, storage_path=path, account="a")
    budget.consume()
    RequestBudget(1, storage_path=path, account="b").consume()
    monkeypatch.setattr(RequestBudget, "_today", staticmethod(lambda: date(2026, 10, 7)))
    budget.consume()
    assert budget.count == 1
    with pytest.raises(MiRateLimitError):
        budget.consume()


def test_concurrent_clients_cannot_exceed_daily_limit(tmp_path):
    path = tmp_path / "quota.sqlite3"

    def attempt(_):
        try:
            RequestBudget(7, storage_path=path, account="a").consume()
            return True
        except MiRateLimitError:
            return False

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(attempt, range(24))) == 7


def test_engine_and_cli_share_budget(tmp_path):
    cfg = load_config(tmp_path / "missing.yaml")
    cfg.paths.db = tmp_path / "health.db"
    cfg.paths.raw = tmp_path / "raw"
    cfg.sync.max_requests_per_day = 1
    conn = connect(cfg.paths.db)
    init_db(conn)
    engine = build_engine(cfg, Repository(conn))
    cli = _make_client(cfg)
    try:
        engine.client.budget.consume()
        with pytest.raises(MiRateLimitError):
            cli.budget.consume()
    finally:
        engine.client.close()
        cli.close()
        conn.close()
