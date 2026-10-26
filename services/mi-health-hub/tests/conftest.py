"""Keep every test's runtime state out of installation and production paths."""

import pytest


@pytest.fixture(autouse=True)
def isolate_runtime_paths(tmp_path, monkeypatch):
    root = tmp_path / "runtime"
    paths = {
        "MI_DB_PATH": root / "health.db",
        "MI_RAW_DIR": root / "raw",
        "MI_OUTPUT_DIR": root / "output",
        "MI_BACKUP_DIR": root / "backup",
        "MI_ACCESS_LOG_DIR": root / "access-logs",
        "MI_CREDENTIALS_FILE": root / "credentials.json",
    }
    for name, path in paths.items():
        monkeypatch.setenv(name, str(path))
