from datetime import datetime, timedelta

from fastapi.testclient import TestClient

from mi_health_hub.ai.cache import input_fingerprint
from mi_health_hub.api.app import CN_TZ, create_app
from mi_health_hub.config import load_config
from mi_health_hub.store.repository import Repository, connect, init_db


def configured(tmp_path):
    cfg = load_config(tmp_path / 'missing.yaml')
    cfg.paths.db = tmp_path / 'health.db'
    cfg.ai.enabled = True
    cfg.ai.api_key = 'test-key'
    cfg.ai.model = 'test-model'
    return cfg, create_app(cfg)


def test_input_hash_ignores_build_time_but_tracks_measurements_and_ranges():
    a = {'generated_at': 'first', 'today': {'steps': 10}, 'window_days': 7}
    assert input_fingerprint(a) == input_fingerprint({**a, 'generated_at': 'later'})
    assert input_fingerprint(a) != input_fingerprint({**a, 'today': {'steps': 11}})
    assert input_fingerprint(a) != input_fingerprint({**a, 'window_days': 30})


def test_old_cache_schema_migrates_without_losing_reports(tmp_path):
    conn = connect(tmp_path / 'old.db')
    init_db(conn)
    repo = Repository(conn)
    try:
        repo.upsert_ai_summary('2026-10-06', {'headline': 'old report'}, 'model', '1')
        conn.execute('ALTER TABLE ai_daily_summary DROP COLUMN input_fingerprint')
        init_db(conn)
        init_db(conn)
        cached = repo.get_ai_summary('2026-10-06')
        assert cached['summary']['headline'] == 'old report'
        assert cached['input_fingerprint'] is None
    finally:
        conn.close()


def test_daily_cache_invalidates_after_correction_or_range_change(tmp_path, monkeypatch):
    cfg, app = configured(tmp_path)
    calls = []

    class AI:
        def __init__(self, *args):
            pass

        def generate_daily_analysis(self, payload):
            calls.append(payload)
            return {'headline': f'analysis {len(calls)}'}

    monkeypatch.setattr('mi_health_hub.api.app.AIClient', AI)
    client = TestClient(app)
    url = '/api/ai/summary?date=2026-10-06'
    assert client.get(url).status_code == 200
    assert client.get(url).json()['_cached'] is True
    conn = connect(cfg.paths.db)
    try:
        conn.execute("INSERT INTO daily_activity(date, steps) VALUES ('2026-10-06', 1200)")
        assert client.get(url).json()['headline'] == 'analysis 2'
        conn.execute("UPDATE daily_activity SET steps=1500 WHERE date='2026-10-06'")
        assert client.get(url).json()['headline'] == 'analysis 3'
        assert client.get(url + '&baseline_days=14&trend_days=14').json()['headline'] == 'analysis 4'
        assert client.get(url + '&baseline_days=14&trend_days=14').json()['_cached'] is True
        assert len(calls) == 4
    finally:
        conn.close()


def test_period_cache_invalidates_after_data_or_note_changes(tmp_path, monkeypatch):
    cfg, app = configured(tmp_path)
    calls = []

    class AI:
        def __init__(self, *args):
            pass

        def generate_period_report(self, payload, *args):
            calls.append(payload)
            return {'headline': f'report {len(calls)}'}

    monkeypatch.setattr('mi_health_hub.api.app.AIClient', AI)
    today = datetime.now(tz=CN_TZ).date()
    last_sunday = (today - timedelta(days=today.weekday() + 1)).isoformat()
    conn = connect(cfg.paths.db)
    try:
        conn.execute('INSERT INTO daily_activity(date, steps) VALUES (?, 100)', (last_sunday,))
        client = TestClient(app)
        def generate():
            response = client.post('/api/ai/report', json={'type': 'weekly'})
            assert response.status_code == 200
            return response.json()
        assert generate()['headline'] == 'report 1'
        assert generate()['_cached'] is True
        conn.execute('UPDATE daily_activity SET steps=200 WHERE date=?', (last_sunday,))
        assert generate()['headline'] == 'report 2'
        conn.execute(
            "INSERT INTO health_note(note_date, content, source, created_at) VALUES (?, 'tired', 'manual', 1)",
            (last_sunday,),
        )
        assert generate()['headline'] == 'report 3'
        assert generate()['_cached'] is True
    finally:
        conn.close()
