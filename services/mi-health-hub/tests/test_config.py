"""配置测试：环境变量优先于 YAML，且凭据缺失必须显式报错而不是静默兜底。"""

from __future__ import annotations

import pytest

from mi_health_hub.config import ConfigError, load_config


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for key in list(__import__("os").environ):
        if key.startswith("MI_"):
            monkeypatch.delenv(key, raising=False)


def test_defaults_are_applied_when_nothing_provided(tmp_path):
    config = load_config(tmp_path / "missing.yaml")
    assert config.region == "cn"
    assert config.sync.chunk_days == 7
    assert config.sync.recent_days == 3
    assert config.sync.correct_days == 14
    assert config.server.api_host == "127.0.0.1"
    assert config.paths.db.name == "health.db"
    assert config.paths.access_logs.name == "access-logs"
    assert config.server.access_log_enabled is True
    assert config.server.access_log_retention_days == 90


def test_env_overrides_yaml(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text(
        "region: sg\nsync:\n  chunk_days: 1\nschedule:\n  incremental_time: '01:00'\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("MI_REGION", "cn")
    monkeypatch.setenv("MI_CHUNK_DAYS", "10")

    config = load_config(path)
    assert config.region == "cn"          # env 覆盖 yaml
    assert config.sync.chunk_days == 10   # env 覆盖 yaml
    assert config.schedule.incremental_time == "01:00"  # yaml 未被覆盖


def test_access_log_settings_can_be_overridden(tmp_path, monkeypatch):
    monkeypatch.setenv("MI_ACCESS_LOG_ENABLED", "false")
    monkeypatch.setenv("MI_ACCESS_LOG_RETENTION_DAYS", "30")
    monkeypatch.setenv("MI_ACCESS_LOG_DIR", str(tmp_path / "logs"))
    config = load_config(tmp_path / "none.yaml")
    assert config.server.access_log_enabled is False
    assert config.server.access_log_retention_days == 30
    assert config.paths.access_logs == tmp_path / "logs"


def test_sentinel_can_be_disabled_by_env(tmp_path, monkeypatch):
    monkeypatch.setenv("MI_SENTINEL_ENABLED", "false")
    assert load_config(tmp_path / "none.yaml").sync.sentinel_enabled is False


def test_invalid_integer_env_reports_clearly(tmp_path, monkeypatch):
    monkeypatch.setenv("MI_CHUNK_DAYS", "seven")
    with pytest.raises(ConfigError) as excinfo:
        load_config(tmp_path / "none.yaml")
    assert "MI_CHUNK_DAYS" in str(excinfo.value)


def test_validate_credentials_fails_without_values(tmp_path):
    config = load_config(tmp_path / "none.yaml")
    with pytest.raises(ConfigError) as excinfo:
        config.validate_credentials()
    assert "MI_USER_ID" in str(excinfo.value)


def test_max_hr_uses_tanaka_when_age_given(tmp_path):
    config = load_config(tmp_path / "none.yaml")
    config.profile.age = 30
    assert config.profile.resolved_max_hr() == int(round(208 - 0.7 * 30))


def test_explicit_max_hr_wins_over_age(tmp_path):
    config = load_config(tmp_path / "none.yaml")
    config.profile.age = 30
    config.profile.max_hr = 185
    assert config.profile.resolved_max_hr() == 185


def test_yaml_with_non_mapping_toplevel_is_rejected(tmp_path):
    path = tmp_path / "bad.yaml"
    path.write_text("- just\n- a\n- list\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(path)
