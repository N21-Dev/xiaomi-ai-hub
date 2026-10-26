"""AI 设置：地址校验、持久化、脱敏、运行时热更新、三种协议适配与接口行为。

这些用例覆盖三类高风险点：
1. SSRF 与地址规范化分档（外部输入严格、运维配置宽松）；
2. API Key 的生命周期语义（永不回传、留空保留、显式清除）；
3. 切换协议 / 地址 / 模型后，旧缓存不得被误命中。
"""

from __future__ import annotations

import json
import os

import pytest
from fastapi.testclient import TestClient

from mi_health_hub.ai import AIClient, AIConfigProvider, AISettingsError, AISettingsStore
from mi_health_hub.ai.settings import normalize_base_url, validate_ai_config
from mi_health_hub.config import AIConfig, load_config


def _cfg(**kwargs) -> AIConfig:
    # 密钥长度要超过脱敏阈值（>10 位），否则只会显示「已设置」，测不出首尾保留
    base = dict(enabled=True, api_key="secret-key-123456", model="m", base_url="https://h/v1")  # gitleaks:allow -- synthetic redaction fixture
    base.update(kwargs)
    return AIConfig(**base)


# ---------------------------------------------------------------- 地址校验
@pytest.mark.parametrize(
    "url",
    [
        "not-a-url",
        "ftp://example.com",
        "https://user:pw@example.com/v1",
        "https://api.example.com/v1/chat/completions",
        "https://api.example.com/v1/responses",
        "https://api.example.com/v1/messages",
        "http://api.example.com/v1",          # 公网必须 HTTPS
        "http://127.0.0.1:11434/v1",          # 默认禁内网
        "http://192.168.1.9:8000/v1",
        "http://localhost:8000/v1",
        "http://169.254.1.1/v1",              # 链路本地永远拒绝
        "http://0.0.0.0:8000/v1",             # 未指定地址永远拒绝
    ],
)
def test_normalize_base_url_rejects(url):
    with pytest.raises(AISettingsError):
        normalize_base_url(url)


def test_normalize_base_url_accepts_public_https_and_trims():
    assert normalize_base_url("https://api.openai.com/v1/") == "https://api.openai.com/v1"


def test_normalize_base_url_allows_private_only_when_opted_in():
    got = normalize_base_url("http://192.168.1.9:11434/v1", allow_private=True)
    assert got == "http://192.168.1.9:11434/v1"


def test_normalize_base_url_still_blocks_link_local_even_with_opt_in():
    with pytest.raises(AISettingsError):
        normalize_base_url("http://169.254.169.254/latest", allow_private=True)


# ---------------------------------------------------------------- 范围校验
@pytest.mark.parametrize(
    "kwargs",
    [
        {"protocol": "gemini_native"},
        {"detail_level": "verbose"},
        {"model": "   "},
        {"timeout_s": 4},
        {"timeout_s": 601},
        {"max_tokens": 32},
        {"max_tokens": 128001},
        {"temperature": -0.1},
        {"temperature": 2.1},
        {"custom_instructions": "x" * 4001},
    ],
)
def test_validate_ai_config_rejects_out_of_range(kwargs):
    with pytest.raises(AISettingsError):
        validate_ai_config(_cfg(**kwargs))


def test_strict_url_false_accepts_operator_private_http():
    """运维用环境变量配内网 Ollama 时不能把服务挡在启动之外。"""
    lenient = validate_ai_config(
        _cfg(base_url="http://192.168.1.9:11434/v1/"), strict_url=False
    )
    assert lenient.base_url == "http://192.168.1.9:11434/v1"
    with pytest.raises(AISettingsError):
        validate_ai_config(_cfg(base_url="http://192.168.1.9:11434/v1"))


def test_validate_normalizes_blank_anthropic_version():
    assert validate_ai_config(_cfg(anthropic_version="")).anthropic_version == "2023-06-01"


# ---------------------------------------------------------------- 持久化
def test_settings_store_roundtrip(tmp_path):
    store = AISettingsStore(tmp_path / "ai-settings.json")
    assert store.exists() is False
    # 文件不存在时保留传入配置
    assert store.load(_cfg()).model == "m"

    saved = store.save(_cfg(model="gpt-4o", base_url="https://api.openai.com/v1"))
    assert saved.model == "gpt-4o"
    assert store.exists() is True

    loaded = store.load(_cfg(model="ignored"))
    assert loaded.model == "gpt-4o"
    assert loaded.api_key == "secret-key-123456"  # gitleaks:allow -- synthetic redaction fixture
    assert loaded.base_url == "https://api.openai.com/v1"


def test_settings_store_ignores_unknown_keys(tmp_path):
    path = tmp_path / "ai-settings.json"
    path.write_text(
        json.dumps({"model": "kept", "totally_unknown": 1}), encoding="utf-8"
    )
    path.chmod(0o600)
    assert AISettingsStore(path).load(_cfg()).model == "kept"


def test_settings_store_rejects_broken_json(tmp_path):
    path = tmp_path / "ai-settings.json"
    path.write_text("{ not json", encoding="utf-8")
    with pytest.raises(AISettingsError):
        AISettingsStore(path).load(_cfg())


def test_settings_file_permission_is_restrictive(tmp_path):
    store = AISettingsStore(tmp_path / "ai-settings.json")
    store.save(_cfg())
    if os.name == "nt":
        pytest.skip("Windows 无 POSIX 权限位")
    import stat

    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600


@pytest.mark.skipif(os.name == "nt", reason="Windows 无 POSIX 权限位")
def test_settings_store_rejects_world_readable_file(tmp_path):
    path = tmp_path / "ai-settings.json"
    AISettingsStore(path).save(_cfg())
    os.chmod(path, 0o644)
    with pytest.raises(AISettingsError, match="权限"):
        AISettingsStore(path).load(_cfg())


def test_settings_save_writes_no_plaintext_to_db(tmp_path):
    """密钥只进独立 JSON 文件，绝不写进健康数据库。"""
    from mi_health_hub.store.repository import connect, init_db

    store = AISettingsStore(tmp_path / "ai-settings.json")
    store.save(_cfg())
    conn = connect(tmp_path / "health.db")
    init_db(conn)
    row = conn.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()
    assert row[0] > 0
    conn.close()
    assert "secret-key" in (tmp_path / "ai-settings.json").read_text(encoding="utf-8")


# ---------------------------------------------------------------- 脱敏
def test_public_view_never_exposes_key():
    secret = "secret-key-123456"  # gitleaks:allow -- synthetic redaction fixture
    view = AISettingsStore.public_view(_cfg(api_key=secret), source="settings")
    assert "api_key" not in view
    assert secret not in json.dumps(view, ensure_ascii=False)
    assert view["api_key_set"] is True
    assert view["api_key_masked"] == "secr…3456"


def test_public_view_short_key_is_fully_masked():
    view = AISettingsStore.public_view(_cfg(api_key="abc123"), source="settings")
    assert view["api_key_masked"] == "已设置"
    assert "abc123" not in json.dumps(view, ensure_ascii=False)


# ---------------------------------------------------------------- 运行时提供器
def _clean_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith("MI_AI"):
            monkeypatch.delenv(key, raising=False)


def test_provider_prefers_saved_settings_over_env_defaults(tmp_path, monkeypatch):
    """既有部署用环境变量配过 AI 时，设置页仍必须能改。"""
    _clean_env(monkeypatch)
    monkeypatch.setenv("MI_AI_MODEL", "from-env")
    store = AISettingsStore(tmp_path / "ai-settings.json")
    provider = AIConfigProvider(_cfg(model="from-env"), store)
    assert provider.locked_fields == []
    assert provider.source() == "environment-default"

    provider.save(_cfg(model="from-page"))
    assert provider.snapshot().model == "from-page"
    assert provider.source() == "settings"


def test_provider_env_lock_is_opt_in(tmp_path, monkeypatch):
    _clean_env(monkeypatch)
    monkeypatch.setenv("MI_AI_MODEL", "locked-model")
    monkeypatch.setenv("MI_AI_LOCK_ENV", "true")
    store = AISettingsStore(tmp_path / "ai-settings.json")
    provider = AIConfigProvider(_cfg(model="locked-model"), store)
    assert provider.locked_fields == ["model"]

    provider.save(_cfg(model="from-page"))
    assert provider.snapshot().model == "locked-model"
    assert provider.source() == "settings+environment"


def test_provider_reset_removes_settings_file(tmp_path, monkeypatch):
    _clean_env(monkeypatch)
    store = AISettingsStore(tmp_path / "ai-settings.json")
    initial = _cfg(model="initial", base_url="https://initial/v1")
    provider = AIConfigProvider(initial, store)
    provider.save(_cfg(model="changed", base_url="https://changed/v1"))
    assert provider.snapshot().model == "changed"

    provider.reset()
    assert provider.snapshot().model == "initial"
    # 必须真正删除文件：否则以后改 .env 会被旧快照盖住
    assert store.exists() is False
    assert AIConfigProvider(initial, store).snapshot().model == "initial"


def test_provider_reset_is_idempotent(tmp_path, monkeypatch):
    _clean_env(monkeypatch)
    store = AISettingsStore(tmp_path / "ai-settings.json")
    provider = AIConfigProvider(_cfg(model="initial"), store)
    provider.reset()
    provider.reset()
    assert provider.snapshot().model == "initial"


def test_provider_preview_does_not_persist(tmp_path):
    store = AISettingsStore(tmp_path / "ai-settings.json")
    provider = AIConfigProvider(_cfg(), store)
    provider.preview(_cfg(model="only-preview"))
    assert provider.snapshot().model == "m"
    assert store.exists() is False


# ---------------------------------------------------------------- 指纹
def test_fingerprint_tracks_switchable_fields():
    base = _cfg()
    for change in (
        {"protocol": "openai_responses"},
        {"base_url": "https://other/v1"},
        {"model": "other-model"},
        {"detail_level": "concise"},
        {"include_health_notes": False},
        {"custom_instructions": "用更温和的语气"},
    ):
        assert base.fingerprint() != _cfg(**change).fingerprint(), change


def test_fingerprint_ignores_api_key_and_whitespace():
    assert _cfg().fingerprint() == _cfg(api_key="rotated-key").fingerprint()
    assert _cfg(base_url="https://h/v1").fingerprint() == _cfg(base_url="https://h/v1/").fingerprint()


# ---------------------------------------------------------------- 协议适配
class _Resp:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def _capture(monkeypatch, payload):
    seen = {}

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def post(self, url, headers=None, json=None):
            seen.update(url=url, headers=headers, body=json)
            return _Resp(payload=payload)

    monkeypatch.setattr("mi_health_hub.ai.client.httpx.Client", lambda *a, **k: _Client())
    return seen


def test_chat_completions_request_shape(monkeypatch):
    seen = _capture(monkeypatch, {"choices": [{"message": {"content": '{"ok":true}'}}]})
    out = AIClient(_cfg()).test_connection()
    assert out["ok"] is True
    assert seen["url"] == "https://h/v1/chat/completions"
    assert seen["headers"]["Authorization"] == "Bearer secret-key-123456"
    assert seen["body"]["response_format"] == {"type": "json_object"}
    assert seen["body"]["max_tokens"] == 8000
    assert "system" in json.dumps(seen["body"]["messages"])


def test_responses_request_shape(monkeypatch):
    seen = _capture(monkeypatch, {"output_text": '{"ok":true}'})
    AIClient(_cfg(protocol="openai_responses")).test_connection()
    assert seen["url"] == "https://h/v1/responses"
    assert seen["body"]["max_output_tokens"] == 8000
    assert "max_tokens" not in seen["body"]
    assert seen["body"]["text"] == {"format": {"type": "json_object"}}


def test_anthropic_request_shape(monkeypatch):
    seen = _capture(monkeypatch, {"content": [{"type": "text", "text": '{"ok":true}'}]})
    AIClient(_cfg(protocol="anthropic_messages")).test_connection()
    assert seen["url"] == "https://h/v1/messages"
    assert seen["headers"]["x-api-key"] == "secret-key-123456"
    assert seen["headers"]["anthropic-version"] == "2023-06-01"
    assert "Authorization" not in seen["headers"]
    # Anthropic 的 system 是顶层字段，messages 里不能再出现 system
    assert isinstance(seen["body"]["system"], str)
    assert all(m["role"] in ("user", "assistant") for m in seen["body"]["messages"])


def test_responses_output_text_fallback_to_output_blocks(monkeypatch):
    _capture(
        monkeypatch,
        {"output": [{"content": [{"type": "output_text", "text": '{"ok":true}'}]}]},
    )
    assert AIClient(_cfg(protocol="openai_responses")).test_connection()["ok"] is True


def test_anthropic_ignores_thinking_blocks(monkeypatch):
    _capture(
        monkeypatch,
        {"content": [
            {"type": "thinking", "thinking": "内部推理"},
            {"type": "text", "text": '{"ok":true}'},
        ]},
    )
    result = AIClient(_cfg(protocol="anthropic_messages")).test_connection()
    assert result["ok"] is True
    assert result["protocol"] == "anthropic_messages"


def test_error_message_does_not_leak_upstream_body(monkeypatch):
    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def post(self, url, headers=None, json=None):  # noqa: ARG002
            return _Resp(status_code=401)

    monkeypatch.setattr("mi_health_hub.ai.client.httpx.Client", lambda *a, **k: _Client())
    from mi_health_hub.ai import AIError

    with pytest.raises(AIError) as excinfo:
        AIClient(_cfg()).test_connection()
    assert "401" in str(excinfo.value)
    assert "secret-key" not in str(excinfo.value)


# ---------------------------------------------------------------- 接口
def _app(tmp_path, **overrides):
    from mi_health_hub.api.app import create_app

    config = load_config(tmp_path / "none.yaml")
    config.paths.db = tmp_path / "t.db"
    for key, value in overrides.items():
        setattr(config.ai, key, value)
    return create_app(config), config


def _settings_body(**overrides):
    body = {
        "enabled": True,
        "protocol": "openai_chat_completions",
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o",
        "timeout_s": 120,
        "max_tokens": 8000,
        "temperature": 0.3,
        "auto_after_sync": True,
        "auto_period_reports": True,
        "json_mode": True,
        "allow_private_base_url": False,
        "anthropic_version": "2023-06-01",
        "detail_level": "detailed",
        "include_health_notes": True,
        "web_tools_enabled": True,
        "custom_instructions": "",
    }
    body.update(overrides)
    return body


def test_settings_get_never_returns_api_key(tmp_path):
    app, _ = _app(tmp_path, enabled=True, api_key="top-secret", model="m")
    resp = TestClient(app).get("/api/ai/settings")
    assert resp.status_code == 200
    data = resp.json()
    assert "top-secret" not in resp.text
    assert data["api_key_set"] is True
    assert data["config_fingerprint"]


def test_settings_put_saves_and_keeps_blank_key(tmp_path):
    app, _ = _app(tmp_path, enabled=True, api_key="keep-me", model="m")
    client = TestClient(app)
    resp = client.put("/api/ai/settings", json=_settings_body())
    assert resp.status_code == 200, resp.text
    assert resp.json()["model"] == "gpt-4o"

    stored = json.loads((tmp_path / "ai-settings.json").read_text(encoding="utf-8"))
    assert stored["api_key"] == "keep-me"
    assert stored["model"] == "gpt-4o"


def test_settings_put_replaces_and_clears_key(tmp_path):
    app, _ = _app(tmp_path, api_key="old-key", model="m")
    client = TestClient(app)

    assert client.put(
        "/api/ai/settings", json=_settings_body(api_key="new-key")
    ).status_code == 200
    stored = json.loads((tmp_path / "ai-settings.json").read_text(encoding="utf-8"))
    assert stored["api_key"] == "new-key"

    resp = client.put("/api/ai/settings", json=_settings_body(clear_api_key=True))
    assert resp.status_code == 200
    assert resp.json()["api_key_set"] is False
    stored = json.loads((tmp_path / "ai-settings.json").read_text(encoding="utf-8"))
    assert stored["api_key"] == ""


def test_settings_put_rejects_ssrf_target(tmp_path):
    app, _ = _app(tmp_path, api_key="k", model="m")
    resp = TestClient(app).put(
        "/api/ai/settings",
        json=_settings_body(base_url="http://169.254.169.254/latest/meta-data"),
    )
    assert resp.status_code == 422
    # 校验失败不得落盘
    assert not (tmp_path / "ai-settings.json").exists()


def test_settings_put_rejects_endpoint_path(tmp_path):
    app, _ = _app(tmp_path, api_key="k", model="m")
    resp = TestClient(app).put(
        "/api/ai/settings",
        json=_settings_body(base_url="https://api.openai.com/v1/chat/completions"),
    )
    assert resp.status_code == 422
    assert "根地址" in resp.json()["detail"]


def test_settings_delete_restores_defaults(tmp_path):
    app, config = _app(tmp_path, api_key="k", model="startup-model")
    client = TestClient(app)
    client.put("/api/ai/settings", json=_settings_body(model="page-model"))
    assert client.get("/api/ai/settings").json()["model"] == "page-model"

    resp = client.delete("/api/ai/settings")
    assert resp.status_code == 200
    assert resp.json()["model"] == "startup-model"
    assert config.ai.model == "startup-model"
    # 恢复默认 = 删除页面设置，回到环境变量/YAML 说了算
    assert not (tmp_path / "ai-settings.json").exists()


def test_settings_write_blocked_for_proxied_requests(tmp_path):
    """公网反代进来的匿名请求不能改 AI 目标地址。"""
    app, _ = _app(tmp_path, api_key="k", model="m")
    client = TestClient(app)
    blocked = {"cf-connecting-ip": "203.0.113.7"}

    assert client.put(
        "/api/ai/settings", json=_settings_body(), headers=blocked
    ).status_code == 403
    assert client.delete("/api/ai/settings", headers=blocked).status_code == 403
    assert client.post(
        "/api/ai/settings/test", json=_settings_body(), headers=blocked
    ).status_code == 403
    assert not (tmp_path / "ai-settings.json").exists()


def test_settings_write_allowed_with_admin_key_even_when_proxied(tmp_path):
    config_from = load_config(tmp_path / "none.yaml")
    config_from.server.api_key = "admin-key"
    config_from.ai.enabled = True
    config_from.ai.api_key = "k"
    config_from.ai.model = "m"
    from mi_health_hub.api.app import create_app

    config_from.paths.db = tmp_path / "t.db"
    client = TestClient(create_app(config_from))
    resp = client.put(
        "/api/ai/settings",
        json=_settings_body(model="remote-model"),
        headers={"cf-connecting-ip": "203.0.113.7", "x-api-key": "admin-key"},
    )
    assert resp.status_code == 200
    assert resp.json()["model"] == "remote-model"


def test_settings_test_endpoint_surfaces_upstream_error(tmp_path, monkeypatch):
    from mi_health_hub.ai import AIError

    app, _ = _app(tmp_path, api_key="k", model="m")

    class _Boom:
        def __init__(self, *a, **k):
            pass

        def test_connection(self):
            raise AIError("API Key 无效或已过期（HTTP 401）", status_code=401)

    monkeypatch.setattr("mi_health_hub.api.app.AIClient", _Boom)
    resp = TestClient(app).post("/api/ai/settings/test", json=_settings_body(api_key="k"))
    assert resp.status_code == 502
    assert "401" in resp.json()["detail"]


def test_settings_models_endpoint_returns_list(tmp_path, monkeypatch):
    app, _ = _app(tmp_path, api_key="k", model="m")

    class _Models:
        def __init__(self, *a, **k):
            pass

        def list_models(self):
            return ["gpt-4o", "gpt-4o-mini"]

    monkeypatch.setattr("mi_health_hub.api.app.AIClient", _Models)
    resp = TestClient(app).post("/api/ai/settings/models", json=_settings_body(api_key="k"))
    assert resp.status_code == 200
    assert resp.json()["models"] == ["gpt-4o", "gpt-4o-mini"]


def test_settings_test_does_not_persist(tmp_path, monkeypatch):
    app, _ = _app(tmp_path, api_key="k", model="m")

    class _Ok:
        def __init__(self, *a, **k):
            pass

        def test_connection(self):
            return {"ok": True, "message": "ok", "latency_ms": 1,
                    "model": "gpt-4o", "protocol": "openai_chat_completions"}

    monkeypatch.setattr("mi_health_hub.api.app.AIClient", _Ok)
    assert TestClient(app).post(
        "/api/ai/settings/test", json=_settings_body(model="gpt-4o")
    ).status_code == 200
    assert not (tmp_path / "ai-settings.json").exists()


# ---------------------------------------------------------------- 缓存失效
def test_switching_model_invalidates_daily_cache(tmp_path):
    """换同名模型但不同地址/协议时，旧缓存不能被复用。"""
    from mi_health_hub.store.repository import Repository, connect, init_db

    conn = connect(tmp_path / "t.db")
    init_db(conn)
    repo = Repository(conn)
    ai = _cfg(model="gpt-4o", base_url="https://a/v1")
    repo.upsert_ai_summary("2026-09-19", {"status": "good"}, ai.model, "4", ai.fingerprint())

    cached = repo.get_ai_summary("2026-09-19")
    assert cached["config_fingerprint"] == ai.fingerprint()
    assert cached["config_fingerprint"] != _cfg(
        model="gpt-4o", base_url="https://b/v1"
    ).fingerprint()
    assert cached["config_fingerprint"] != _cfg(
        model="gpt-4o", protocol="anthropic_messages"
    ).fingerprint()
    # 换密钥不该让缓存失效（密钥不进指纹），但地址/协议变化必须失效
    assert cached["config_fingerprint"] == _cfg(
        model="gpt-4o", base_url="https://a/v1", api_key="rotated-key-value"
    ).fingerprint()
    conn.close()


def test_switching_model_invalidates_report_cache(tmp_path):
    from mi_health_hub.store.repository import Repository, connect, init_db

    conn = connect(tmp_path / "t.db")
    init_db(conn)
    repo = Repository(conn)
    ai = _cfg(model="gpt-4o")
    repo.upsert_ai_report(
        "weekly", "2026-09-07", "2026-09-13", {"title": "周报"}, ai.model, "4",
        ai.fingerprint(),
    )
    report = repo.get_ai_report("weekly", "2026-09-07", "2026-09-13")["report"]
    assert report["config_fingerprint"] == ai.fingerprint()
    assert report["prompt_version"] == "4"
    assert report["model"] == "gpt-4o"
    conn.close()
