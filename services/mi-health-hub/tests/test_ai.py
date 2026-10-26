"""AI 每日分析：客户端解析、配置加载、缓存与端点。

AIClient 的联网部分用 fake httpx 替代，避免命中真实 NVIDIA 接口。
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from mi_health_hub.ai import AIClient, AIError
from mi_health_hub.ai.client import PROMPT_VERSION
from mi_health_hub.config import AIConfig, load_config
from mi_health_hub.store.repository import Repository


# ---------------------------------------------------------------- 解析
@pytest.mark.parametrize(
    "raw,expected",
    [
        ('{"date":"2026-09-19","status":"good"}', {"date": "2026-09-19", "status": "good"}),
        # 容错：去掉 ```json 围栏（并补上缺失的 date）
        ('```json\n{"a":1}\n```', {"a": 1, "date": "2026-09-19"}),
        ('```\n{"b":2}\n```', {"b": 2, "date": "2026-09-19"}),
        # 前后空白
        ('  {"c":3}  ', {"c": 3, "date": "2026-09-19"}),
    ],
)
def test_parse_json_clean_and_fenced(raw, expected):
    assert AIClient._parse_json(raw, "2026-09-19") == expected


@pytest.mark.parametrize("raw", ["", "   ", None, "不是 JSON", "只是文字没有括号", "[1,2,3]"])
def test_parse_json_rejects_non_object(raw):
    assert AIClient._parse_json(raw, "2026-09-19") is None


def test_parse_json_fills_missing_date():
    obj = AIClient._parse_json('{"status":"good"}', "2026-09-20")
    assert obj["date"] == "2026-09-20"


# ---------------------------------------------------------------- 配置加载
def test_ai_config_loaded_from_env(monkeypatch):
    monkeypatch.setenv("MI_AI_ENABLED", "true")
    monkeypatch.setenv("MI_AI_BASE_URL", "https://example/v1")
    monkeypatch.setenv("MI_AI_API_KEY", "nvapi-test")
    monkeypatch.setenv("MI_AI_MODEL", "nvidia/foo")
    monkeypatch.setenv("MI_AI_MAX_TOKENS", "1234")
    monkeypatch.setenv("MI_AI_TEMPERATURE", "0.5")
    monkeypatch.setenv("MI_AI_AUTO_AFTER_SYNC", "false")

    cfg = load_config("/nonexistent/path.yaml").ai
    assert cfg.enabled is True
    assert cfg.base_url == "https://example/v1"
    assert cfg.api_key == "nvapi-test"
    assert cfg.model == "nvidia/foo"
    assert cfg.max_tokens == 1234
    assert cfg.temperature == 0.5
    assert cfg.auto_after_sync is False
    assert cfg.configured() is True


def test_ai_config_defaults_unconfigured(monkeypatch):
    for name in (
        "MI_AI_ENABLED", "MI_AI_BASE_URL", "MI_AI_API_KEY", "MI_AI_MODEL",
        "MI_AI_MAX_TOKENS", "MI_AI_TEMPERATURE",
    ):
        monkeypatch.delenv(name, raising=False)
    cfg = load_config("/nonexistent/path.yaml").ai
    assert cfg.enabled is False
    assert cfg.configured() is False


# ---------------------------------------------------------------- 客户端（mock httpx）
class _FakeResponse:
    def __init__(self, status_code: int = 200, json_data=None, text: str = ""):
        self.status_code = status_code
        self._json = json_data
        self._text = text or (json.dumps(json_data) if json_data is not None else "")

    @property
    def text(self) -> str:
        return self._text

    def json(self):
        if self._json is not None:
            return self._json
        raise ValueError("no json")


class _FakeClient:
    """模拟 httpx.Client 上下文管理器；post 行为由 factory 决定。"""

    def __init__(self, factory):
        self._factory = factory

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def post(self, url, headers=None, json=None):  # noqa: ARG002
        return self._factory()


def _client_with(content: str, status: int = 200):
    captured = {}

    def factory():
        captured["called"] = True
        return _FakeResponse(
            status_code=status,
            json_data={"choices": [{"message": {"content": content}}]},
        )

    return _FakeClient(factory), captured


def test_generate_returns_parsed_json(monkeypatch):
    expected = {"date": "2026-09-19", "status": "good", "metrics": []}
    fake, captured = _client_with(json.dumps(expected))
    monkeypatch.setattr("mi_health_hub.ai.client.httpx.Client", lambda *a, **k: fake)

    cfg = AIConfig(enabled=True, api_key="x", model="m", base_url="https://h/v1")
    out = AIClient(cfg).generate_daily_analysis({"date": "2026-09-19"})
    assert out["date"] == "2026-09-19"
    assert out["status"] == "good"
    assert out["model"] == "m"
    assert out["prompt_version"] == PROMPT_VERSION
    assert captured["called"]


def test_generate_retries_on_bad_json(monkeypatch):
    expected = {"date": "2026-09-19", "status": "neutral"}
    calls = {"n": 0}

    def factory():
        calls["n"] += 1
        if calls["n"] == 1:
            # 第一次返回带废话的内容
            return _FakeResponse(
                json_data={"choices": [{"message": {"content": "好的，这是结果：\n" + json.dumps(expected)}}]}
            )
        return _FakeResponse(json_data={"choices": [{"message": {"content": json.dumps(expected)}}]})

    fake = _FakeClient(factory)
    monkeypatch.setattr("mi_health_hub.ai.client.httpx.Client", lambda *a, **k: fake)

    cfg = AIConfig(enabled=True, api_key="x", model="m", base_url="https://h/v1")
    out = AIClient(cfg).generate_daily_analysis({"date": "2026-09-19"})
    assert out["status"] == "neutral"
    assert calls["n"] == 2


def test_generate_raises_on_401(monkeypatch):
    fake, _ = _client_with("{}", status=401)
    monkeypatch.setattr("mi_health_hub.ai.client.httpx.Client", lambda *a, **k: fake)
    cfg = AIConfig(enabled=True, api_key="bad", model="m", base_url="https://h/v1")
    with pytest.raises(AIError, match="401"):
        AIClient(cfg).generate_daily_analysis({"date": "2026-09-19"})


def test_generate_raises_when_unconfigured():
    cfg = AIConfig(enabled=False, api_key="", model="", base_url="")
    with pytest.raises(AIError, match="未配置"):
        AIClient(cfg).generate_daily_analysis({"date": "2026-09-19"})


# ---------------------------------------------------------------- 缓存读写
def test_ai_summary_cache_roundtrip(tmp_path):
    from mi_health_hub.store.repository import Repository, connect, init_db

    conn = connect(tmp_path / "t.db")
    init_db(conn)
    repo = Repository(conn)

    assert repo.get_ai_summary("2026-09-19") is None
    summary = {"date": "2026-09-19", "status": "good", "headline": "平稳"}
    repo.upsert_ai_summary("2026-09-19", summary, "nvidia/foo", "1")
    cached = repo.get_ai_summary("2026-09-19")
    assert cached["summary"] == summary
    assert cached["model"] == "nvidia/foo"
    assert cached["prompt_version"] == "1"

    # 同日覆盖
    summary2 = {"date": "2026-09-19", "status": "watch", "headline": "留意"}
    repo.upsert_ai_summary("2026-09-19", summary2, "nvidia/foo", "1")
    assert repo.get_ai_summary("2026-09-19")["summary"] == summary2


# ---------------------------------------------------------------- 端点
def test_ai_summary_endpoint_501_when_unconfigured(tmp_path):
    from mi_health_hub.api.app import create_app

    config = load_config(tmp_path / "none.yaml")
    config.paths.db = tmp_path / "t.db"
    config.ai.enabled = False
    config.ai.api_key = ""
    app = create_app(config)
    resp = TestClient(app).get("/api/ai/summary?date=2026-09-19")
    assert resp.status_code == 501


def test_ai_summary_endpoint_returns_analysis(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from mi_health_hub.api.app import create_app

    expected = {"date": "2026-09-19", "status": "good", "headline": "正常"}

    class _StubClient:
        def __init__(self, *args, **kwargs):  # noqa: ARG002
            pass

        def generate_daily_analysis(self, payload):  # noqa: ARG002
            return expected

    monkeypatch.setattr("mi_health_hub.api.app.AIClient", _StubClient)

    config = load_config(tmp_path / "none.yaml")
    config.paths.db = tmp_path / "t.db"
    config.ai.enabled = True
    config.ai.api_key = "x"
    config.ai.model = "nvidia/foo"
    app = create_app(config)
    resp = TestClient(app).get("/api/ai/summary?date=2026-09-19")
    assert resp.status_code == 200
    assert resp.json()["status"] == "good"


def test_ai_summaries_endpoint_lists_days_and_null_when_ungenerated(tmp_path):
    from fastapi.testclient import TestClient

    from mi_health_hub.api.app import create_app

    config = load_config(tmp_path / "none.yaml")
    config.paths.db = tmp_path / "t.db"
    config.ai.enabled = True
    config.ai.api_key = "x"
    config.ai.model = "nvidia/foo"
    app = create_app(config)
    resp = TestClient(app).get("/api/ai/summaries?days=7")
    assert resp.status_code == 200
    body = resp.json()
    assert body["configured"] is True
    assert len(body["items"]) == 7
    # 没生成过 → summary 全是 null
    assert all(it["summary"] is None for it in body["items"])
    # 顺序：新的在前（items[0] 是今天）
    import datetime as dt

    dates = [it["date"] for it in body["items"]]
    assert dates == sorted(dates, reverse=True)
    assert dates[0] == dt.date.today().isoformat()


# ---------------------------------------------------------------- 503 重试
def test_generate_retries_on_503(monkeypatch):
    expected = {"date": "2026-09-19", "status": "good"}
    calls = {"n": 0}

    def factory():
        calls["n"] += 1
        if calls["n"] == 1:
            return _FakeResponse(status_code=503, text="Service Unavailable")
        return _FakeResponse(
            json_data={"choices": [{"message": {"content": json.dumps(expected)}}]}
        )

    fake = _FakeClient(factory)
    monkeypatch.setattr("mi_health_hub.ai.client.httpx.Client", lambda *a, **k: fake)
    monkeypatch.setattr("mi_health_hub.ai.client.time.sleep", lambda s: None)  # noqa: ARG005

    cfg = AIConfig(enabled=True, api_key="x", model="m", base_url="https://h/v1")
    out = AIClient(cfg).generate_daily_analysis({"date": "2026-09-19"})
    assert out["status"] == "good"
    assert calls["n"] == 2


def test_generate_does_not_retry_on_401(monkeypatch):
    calls = {"n": 0}

    def factory():
        calls["n"] += 1
        return _FakeResponse(status_code=401, text="unauthorized")

    fake = _FakeClient(factory)
    monkeypatch.setattr("mi_health_hub.ai.client.httpx.Client", lambda *a, **k: fake)
    monkeypatch.setattr("mi_health_hub.ai.client.time.sleep", lambda s: None)  # noqa: ARG005

    cfg = AIConfig(enabled=True, api_key="bad", model="m", base_url="https://h/v1")
    with pytest.raises(AIError, match="401"):
        AIClient(cfg).generate_daily_analysis({"date": "2026-09-19"})
    assert calls["n"] == 1


# ---------------------------------------------------------------- 周期区间
def test_resolve_period_range_weekly_and_monthly():
    from datetime import date

    from mi_health_hub.analysis.report import resolve_period_range

    today = date(2026, 9, 19)  # 周六
    start, end = resolve_period_range("weekly", today)
    assert (start, end) == (date(2026, 9, 7), date(2026, 9, 13))  # 上个完整自然周
    start2, end2 = resolve_period_range("monthly", today)
    assert (start2, end2) == (date(2026, 8, 1), date(2026, 8, 31))  # 上个自然月


# ---------------------------------------------------------------- 对话 / 备注 / 报告
def test_chat_notes_report_repository_roundtrip(tmp_path):
    from mi_health_hub.store.repository import Repository, connect, init_db

    conn = connect(tmp_path / "t.db")
    init_db(conn)
    repo = Repository(conn)

    repo.add_chat_message("user", "我这周睡得怎么样？")
    repo.add_chat_message("assistant", "近 7 天平均睡眠 5 小时 48 分。")
    history = repo.get_chat_history(limit=10)
    assert [m["role"] for m in history] == ["user", "assistant"]

    note_id = repo.add_health_note("2026-09-19", "用户自述下午头疼", "chat")
    assert note_id > 0
    notes = repo.get_health_notes(limit=10)
    assert notes[0]["content"] == "用户自述下午头疼"
    between = repo.get_health_notes_between("2026-09-01", "2026-09-30")
    assert len(between) == 1
    assert repo.get_health_notes_between("2026-09-01", "2026-09-18") == []

    report = {"title": "周报", "headline": "整体平稳", "status": "good"}
    repo.upsert_ai_report("weekly", "2026-09-07", "2026-09-13", report, "m", "3")
    got = repo.get_ai_report("weekly", "2026-09-07", "2026-09-13")
    assert got["report"]["headline"] == "整体平稳"
    listed = repo.list_ai_reports(limit=10)
    assert len(listed) == 1 and listed[0]["period_type"] == "weekly"
    # 覆盖
    repo.upsert_ai_report("weekly", "2026-09-07", "2026-09-13",
                          {"title": "周报v2"}, "m", "3")
    assert repo.get_ai_report("weekly", "2026-09-07", "2026-09-13")["report"]["title"] == "周报v2"


def _configured_app(tmp_path):
    from mi_health_hub.api.app import create_app

    config = load_config(tmp_path / "none.yaml")
    config.paths.db = tmp_path / "t.db"
    config.ai.enabled = True
    config.ai.api_key = "x"
    config.ai.model = "nvidia/foo"
    return create_app(config)


def test_chat_endpoint_records_note_and_session_context(tmp_path, monkeypatch):
    class _StubClient:
        def __init__(self, *args, **kwargs):  # noqa: ARG002
            pass

        def chat_with_tools(self, base_context, message, history, executor, max_rounds=3):  # noqa: ARG002
            assert message == "我今天有点头疼"
            assert "健康上下文" in base_context
            assert "workouts" not in base_context  # 运动明细不再预载
            assert history and history[0]["content"] == "我喜欢游泳"  # 会话上下文传入
            return {
                "reply": "已注意到你的不适。你的静息心率近 7 天均值 49bpm，属正常范围。",
                "should_record": True,
                "record_text": "用户自述 2026-09-19 有点头疼",
                "note_date": "2026-09-19",
            }

    monkeypatch.setattr("mi_health_hub.api.app.AIClient", _StubClient)
    client = TestClient(_configured_app(tmp_path))

    resp = client.post("/api/ai/chat", json={
        "message": "我今天有点头疼",
        "history": [
            {"role": "user", "content": "我喜欢游泳"},
            {"role": "assistant", "content": "好的，游泳记录我会结合数据来看。"},
        ],
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["recorded"] is True
    assert body["note_date"] == "2026-09-19"

    # 对话本身不落库：无历史端点可查
    assert client.get("/api/ai/chat/history").status_code in (404, 405)
    notes = client.get("/api/ai/notes").json()["notes"]
    assert notes[0]["content"] == "用户自述 2026-09-19 有点头疼"


def test_chat_endpoint_422_on_empty_message(tmp_path):
    client = TestClient(_configured_app(tmp_path))
    assert client.post("/api/ai/chat", json={"message": "  "}).status_code == 422


def test_report_endpoint_generates_and_caches(tmp_path, monkeypatch):
    from datetime import date, timedelta

    from mi_health_hub.api.app import create_app
    from mi_health_hub.store.repository import connect, init_db

    config = load_config(tmp_path / "none.yaml")
    config.paths.db = tmp_path / "t.db"
    config.ai.enabled = True
    config.ai.api_key = "x"
    config.ai.model = "nvidia/foo"
    create_app(config)

    # 灌最小数据：近 40 天每天的步数与睡眠，保证任何运行日期下周期内都有数据
    conn = connect(config.paths.db)
    init_db(conn)
    today = date.today()
    for i in range(40):
        d = (today - timedelta(days=i)).isoformat()
        conn.execute(
            "INSERT OR REPLACE INTO daily_activity (date, steps) VALUES (?, ?)", (d, 8000)
        )
        conn.execute(
            "INSERT OR REPLACE INTO sleep_session (date, start_ts, end_ts, total_min, "
            "efficiency) VALUES (?, ?, ?, ?, ?)",
            (d, 0, 0, 400, 0.95),
        )
    conn.commit()
    conn.close()

    stub_report = {
        "title": "2026 年 9 月第 2 周健康周报",
        "period_type": "weekly",
        "headline": "整体平稳",
        "status": "good",
        "metrics": [],
        "sections": [{"title": "睡眠", "text": "平均 6 小时 10 分。"}],
        "highlights": [],
        "suggestion": "保持",
        "next_focus": [],
        "caveats": [],
    }

    class _StubClient:
        def __init__(self, *args, **kwargs):  # noqa: ARG002
            pass

        def generate_period_report(self, payload, period_type, start, end):  # noqa: ARG002
            assert period_type == "weekly"
            return dict(stub_report)

    monkeypatch.setattr("mi_health_hub.api.app.AIClient", _StubClient)
    client = TestClient(_configured_app(tmp_path))

    resp = client.post("/api/ai/report", json={"type": "weekly"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["title"] == stub_report["title"]
    assert "_cached" not in body

    # 第二次不带 regenerate → 命中缓存
    resp2 = client.post("/api/ai/report", json={"type": "weekly"})
    assert resp2.status_code == 200
    assert resp2.json()["_cached"] is True

    listed = client.get("/api/ai/reports").json()["reports"]
    assert len(listed) == 1 and listed[0]["report"]["headline"] == "整体平稳"


def test_report_endpoint_rejects_unknown_type(tmp_path):
    client = TestClient(_configured_app(tmp_path))
    assert client.post("/api/ai/report", json={"type": "yearly"}).status_code == 422


def test_chat_report_501_when_unconfigured(tmp_path):
    from mi_health_hub.api.app import create_app

    config = load_config(tmp_path / "none.yaml")
    config.paths.db = tmp_path / "t.db"
    config.ai.enabled = False
    app = create_app(config)
    c = TestClient(app)
    assert c.post("/api/ai/chat", json={"message": "hi"}).status_code == 501
    assert c.post("/api/ai/report", json={"type": "weekly"}).status_code == 501


def test_note_delete_endpoint(tmp_path):
    client = TestClient(_configured_app(tmp_path))
    created = client.post(
        "/api/ai/notes", json={"content": "测试备注", "note_date": "2026-09-19"}
    ).json()
    nid = created["id"]
    assert client.delete(f"/api/ai/notes/{nid}").json()["deleted"] == nid
    assert client.get("/api/ai/notes").json()["notes"] == []
    assert client.delete(f"/api/ai/notes/{nid}").status_code == 404


def test_tool_executor_series_and_unknown(tmp_path):
    import time as time_mod

    from mi_health_hub.api.app import _build_tool_executor, _sync_state
    from mi_health_hub.store.repository import connect, init_db

    db = tmp_path / "t.db"
    conn = connect(db)
    init_db(conn)
    repo = Repository(conn)
    config = load_config(tmp_path / "none.yaml")
    config.paths.db = db
    executor = _build_tool_executor(repo, config)

    out = executor({"tool": "query_series", "args": {"metric": "steps",
                    "start": "2026-09-01", "end": "2026-09-19"}})
    assert "没有数据" in out  # 空库如实返回
    assert "可用指标" in executor({"tool": "query_series", "args": {"metric": "nope"}})
    assert "未知工具" in executor({"tool": "bad_tool", "args": {}})

    st = executor({"tool": "check_sync_status", "args": {}})
    assert "没有同步任务记录" in st

    started = executor({"tool": "trigger_sync", "args": {}})
    assert "已在后台启动" in started
    # 无凭据 → 后台线程快速失败；等它落状态释放锁
    deadline = time_mod.time() + 10
    while time_mod.time() < deadline and _sync_state["running"]:
        time_mod.sleep(0.05)
    assert _sync_state["running"] is False
    assert _sync_state["ok"] is False
    conn.close()


def test_web_search_and_read_page_tools(tmp_path, monkeypatch):
    from mi_health_hub.api import app as app_mod
    from mi_health_hub.store.repository import Repository, connect, init_db

    db = tmp_path / "t.db"
    conn = connect(db)
    init_db(conn)
    repo = Repository(conn)
    executor = app_mod._build_tool_executor(repo, load_config(tmp_path / "none.yaml"))

    fake_html = (
        '<li class="b_algo"><h2><a href="https://example.org/a">睡眠<b>不足</b>危害</a></h2>'
        "<p>长期睡眠不足会增加风险。</p></li>"
    )
    monkeypatch.setattr(app_mod, "_fetch_bing", lambda q, n=8: [
        {"title": "睡眠不足危害", "url": "https://example.org/a", "snippet": "长期睡眠不足会增加风险。"}
    ])
    out = executor({"tool": "web_search", "args": {"query": "睡眠不足"}})
    assert "睡眠不足危害" in out and "https://example.org/a" in out
    assert "缺少" in executor({"tool": "web_search", "args": {}})

    # read_page：内网地址必须拒绝
    assert "拒绝" in executor({"tool": "read_page", "args": {"url": "http://127.0.0.1:8321/"}})
    assert "拒绝" in executor({"tool": "read_page", "args": {"url": "http://192.168.1.1/"}})
    assert "无效" in executor({"tool": "read_page", "args": {"url": "ftp://x"}})
    conn.close()


# ---------------------------------------------------------------- 工具调用循环
def _client_with_sequence(contents: list[str], monkeypatch):
    it = iter(contents)

    def factory():
        return _FakeResponse(json_data={"choices": [{"message": {"content": next(it)}}]})

    fake = _FakeClient(factory)
    monkeypatch.setattr("mi_health_hub.ai.client.httpx.Client", lambda *a, **k: fake)
    return AIConfig(enabled=True, api_key="x", model="m", base_url="https://h/v1")


def test_chat_with_tools_loop(monkeypatch):
    cfg = _client_with_sequence(
        [
            json.dumps({"action": {"tool": "query_ai_summary", "args": {"date": "2026-09-19"}}}),
            json.dumps({"reply": "最终回答", "should_record": False}),
        ],
        monkeypatch,
    )
    seen = {}

    def executor(action):
        seen["action"] = action
        return "TOOL_RESULT"

    out = AIClient(cfg).chat_with_tools("BASE", "九月的解读说了什么", [], executor)
    assert out["reply"] == "最终回答"
    assert seen["action"]["tool"] == "query_ai_summary"


def test_chat_with_tools_max_rounds(monkeypatch):
    cfg = _client_with_sequence(
        [json.dumps({"action": {"tool": "query_ai_summary", "args": {}}})] * 5,
        monkeypatch,
    )
    calls = {"n": 0}

    def executor(action):  # noqa: ARG001
        calls["n"] += 1
        return "TOOL_RESULT"

    out = AIClient(cfg).chat_with_tools("BASE", "问题", [], executor, max_rounds=3)
    assert calls["n"] == 3  # 3 轮工具调用后被强制收尾
    assert "action" in out  # 模型仍返回 action → 原样返回，由端点兜底
