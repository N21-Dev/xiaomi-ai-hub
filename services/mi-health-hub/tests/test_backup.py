import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from mi_health_hub.store.repository import Repository, connect, init_db
from mi_health_hub.sync.engine import SyncEngine


def engine_for(directory, repo=None):
    cfg = SimpleNamespace(paths=SimpleNamespace(backup=directory))
    return SyncEngine(cfg, repo, None, None)


def test_retention_groups_days_and_weeks_without_requiring_sundays(tmp_path):
    for offset in range(100):
        day = date(2026, 10, 6) - timedelta(days=offset)
        if day.weekday() == 6:
            continue
        for hour in (4, 12):
            (tmp_path / f"health-{day:%Y%m%d}-{hour:02}3000.db").touch()
    unknown = tmp_path / "health-manual.db"
    unknown.touch()
    engine_for(tmp_path)._prune_backups()
    files = sorted((p for p in tmp_path.glob('*.db') if p != unknown), reverse=True)
    days = [datetime.strptime(p.stem.split('-')[1], '%Y%m%d').date() for p in files]
    assert len(files) == 22
    assert len(set(days[:14])) == 14
    assert len({d.isocalendar()[:2] for d in days[14:]}) == 8
    assert all(p.stem.endswith('123000') for p in files)
    assert unknown.exists()


def test_backup_is_restorable_private_and_names_do_not_collide(tmp_path):
    conn = connect(tmp_path / 'source.db')
    init_db(conn)
    conn.execute("INSERT INTO daily_activity(date, steps) VALUES ('2026-10-06', 123)")
    engine = engine_for(tmp_path / 'backup', Repository(conn))
    try:
        first = engine.backup_database()
        assert first is not None
        with sqlite3.connect(first) as restored:
            assert restored.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
            assert restored.execute('SELECT steps FROM daily_activity').fetchone()[0] == 123
        assert Path(first).stat().st_mode & 0o777 == 0o600
        second = engine.backup_database()
        assert second is not None and second != first
    finally:
        conn.close()


def test_bad_backup_does_not_prune_existing_backups(tmp_path, monkeypatch):
    class BadCopy:
        def vacuum_into(self, target):
            target.write_bytes(b'not a sqlite database')

        def integrity_check(self):
            raise AssertionError('must validate the copy, not the source')

    engine = engine_for(tmp_path, BadCopy())
    pruned = []
    monkeypatch.setattr(engine, '_prune_backups', lambda: pruned.append(True))
    assert engine.backup_database() is None
    assert pruned == []
