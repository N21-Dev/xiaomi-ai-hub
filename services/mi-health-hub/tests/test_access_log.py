"""Cloudflare / 局域网逐请求访问日志。"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from types import SimpleNamespace

from fastapi.testclient import TestClient
from starlette.datastructures import Headers, URL

from mi_health_hub.api.access_log import (
    AccessLogStore,
    build_access_entry,
    sanitize_query,
    sanitize_referer,
)
from mi_health_hub.api.app import create_app
from mi_health_hub.config import load_config


class FakeRequest:
    def __init__(self, url: str, headers: dict[str, str], peer: str = "172.22.0.1") -> None:
        self.headers = Headers(headers)
        self.client = SimpleNamespace(host=peer)
        self.method = "GET"
        self.url = URL(url)
        self.scope = {"http_version": "1.1"}


def test_sanitize_query_redacts_sensitive_values():
    raw = "date=2026-09-29&api_key=super-secret&ticket=abc&name=normal"
    safe = sanitize_query(raw)
    assert "2026-09-29" in safe
    assert "super-secret" not in safe and "abc" not in safe and "normal" not in safe
    assert safe.count("%3Credacted%3E") == 3


def test_referer_fragment_removed_and_sensitive_query_redacted():
    safe = sanitize_referer("https://health.example.com/page?date=ok&token=secret#private")
    assert "date=ok" in safe
    assert "secret" not in safe and "private" not in safe


def test_cloudflare_entry_uses_original_ip_and_metadata():
    request = FakeRequest(
        "https://health.example.com/api/summary?date=2026-09-29&token=secret",
        {
            "host": "health.example.com",
            "cf-connecting-ip": "203.0.113.19",
            "cf-ipcountry": "cn",
            "cf-ipcity": "Shanghai",
            "cf-region": "Shanghai",
            "cf-ray": "a42b667b0d5ffef7-PDX",
            "user-agent": "Browser/1.0",
            "referer": "https://health.example.com/",
            "x-forwarded-proto": "https",
            "cookie": "MUST-NOT-LOG",
            "x-api-key": "MUST-NOT-LOG",
        },
    )
    entry = build_access_entry(
        request=request,
        status=200,
        duration_ms=12.345,
        response_bytes=712,
        now=datetime.fromisoformat("2026-09-29T21:00:00+08:00"),
    )
    assert entry["source"] == "cloudflare"
    assert entry["client_ip"] == "203.0.113.19"
    assert entry["peer_ip"] == "172.22.0.1"
    assert entry["country"] == "CN" and entry["cf_colo"] == "PDX"
    assert entry["host"] == "health.example.com"
    assert entry["duration_ms"] == 12.35
    raw = json.dumps(entry)
    assert "MUST-NOT-LOG" not in raw and "secret" not in raw


def test_spoofed_cf_ip_without_valid_ray_is_not_trusted():
    request = FakeRequest(
        "http://192.0.2.10:8321/healthz",
        {"host": "192.0.2.10:8321", "cf-connecting-ip": "8.8.8.8"},
        peer="192.0.2.10",
    )
    entry = build_access_entry(request=request, status=200, duration_ms=1, response_bytes=0)
    assert entry["source"] == "lan"
    assert entry["client_ip"] == "192.0.2.10"


def test_store_rotates_at_size_limit_and_stops_at_daily_cap(tmp_path):
    store = AccessLogStore(tmp_path, max_file_bytes=1024 * 1024, max_files_per_day=2)
    # 测试不写 1MB 内容：先造一个接近上限的主文件，再 append 触发 -001 轮转。
    primary = tmp_path / "access-2026-09-29.jsonl"
    primary.write_bytes(b"x" * (1024 * 1024))
    entry = {
        "ts": "2026-09-29T12:00:00+08:00", "source": "lan", "client_ip": "1.1.1.1",
        "peer_ip": "1.1.1.1", "country": "", "city": "", "region": "", "host": "x",
        "method": "GET", "path": "/", "query": "", "status": 200, "duration_ms": 1,
        "request_bytes": 0, "response_bytes": 0, "user_agent": "", "referer": "",
        "cf_ray": "", "cf_colo": "", "http_version": "1.1", "scheme": "http",
    }
    store.append(entry)
    rotated = tmp_path / "access-2026-09-29-001.jsonl"
    assert rotated.exists()
    # 第二个文件也打满后达到单日 2 文件硬上限。
    rotated.write_bytes(b"x" * (1024 * 1024))
    import pytest
    with pytest.raises(OSError, match="容量上限"):
        store.append(entry)


def test_store_writes_0600_rotates_queries_and_summarizes(tmp_path):
    store = AccessLogStore(tmp_path, retention_days=30)
    now = datetime.fromisoformat("2026-09-29T21:00:00+08:00")
    base = {
        "source": "cloudflare", "peer_ip": "172.22.0.1", "country": "CN",
        "city": "", "region": "", "method": "GET", "query": "", "status": 200,
        "duration_ms": 10, "request_bytes": 0, "response_bytes": 100,
        "user_agent": "ua", "referer": "", "cf_ray": "ray", "cf_colo": "PDX",
        "http_version": "1.1", "scheme": "https",
    }
    entries = [
        {**base, "ts": (now - timedelta(minutes=3)).isoformat(), "client_ip": "1.1.1.1", "host": "health.example.com", "path": "/"},
        {**base, "ts": (now - timedelta(minutes=2)).isoformat(), "client_ip": "1.1.1.1", "host": "health.example.com", "path": "/api/summary", "status": 404},
        {**base, "ts": (now - timedelta(minutes=1)).isoformat(), "client_ip": "2.2.2.2", "host": "hub.example.com", "path": "/"},
    ]
    for entry in entries:
        store.append(entry)
    target = tmp_path / "access-2026-09-29.jsonl"
    assert target.exists()
    if os.name != "nt":
        assert target.stat().st_mode & 0o777 == 0o600
    result = store.query(hours=1, host="health.example.com", now=now)
    assert result["count"] == 2 and result["items"][0]["status"] == 404
    summary = store.summary(hours=1, now=now)
    assert summary["total"] == 3 and summary["unique_ips"] == 2
    assert summary["cloudflare"] == 3 and summary["errors"] == 1
    assert {x["host"] for x in summary["hosts"]} == {"health.example.com", "hub.example.com"}


def _app(tmp_path):
    config = load_config(tmp_path / "none.yaml")
    config.paths.db = tmp_path / "health.db"
    config.paths.raw = tmp_path / "raw"
    config.paths.output = tmp_path / "output"
    config.paths.backup = tmp_path / "backup"
    config.paths.access_logs = tmp_path / "access"
    config.server.access_log_enabled = True
    return create_app(config)


def test_middleware_records_request_and_api_is_local_only(tmp_path):
    client = TestClient(_app(tmp_path))
    response = client.get("/api/summary?date=2026-09-29&token=do-not-log")
    assert response.status_code == 200
    lines = list((tmp_path / "access").glob("access-*.jsonl"))
    assert lines
    text = lines[0].read_text(encoding="utf-8")
    assert "/api/summary" in text and "do-not-log" not in text

    local = client.get("/api/access-logs?hours=1")
    assert local.status_code == 200
    body = local.json()
    assert body["count"] >= 1

    blocked = client.get(
        "/api/access-logs?hours=1",
        headers={"cf-connecting-ip": "203.0.113.5", "cf-ray": "abcdef0123456789-PDX"},
    )
    assert blocked.status_code == 403
    # 即使不带 Cloudflare 头，用公网 Host 从 Docker 私网 peer 访问也不能读日志。
    assert client.get("/api/access-logs?hours=1", headers={"host": "health.example.com"}).status_code == 403

    # 查询日志接口本身不写入访问日志，避免刷新页面制造递归噪音。
    before = sum(len(p.read_text(encoding="utf-8").splitlines()) for p in lines)
    client.get("/api/access-logs/summary?hours=1")
    after = sum(len(p.read_text(encoding="utf-8").splitlines()) for p in lines)
    assert after == before


def test_both_domains_are_recorded_separately(tmp_path):
    client = TestClient(_app(tmp_path))
    client.get("/healthz", headers={"host": "health.example.com", "cf-connecting-ip": "1.1.1.1", "cf-ray": "abcdef0123456789-PDX"})
    client.get("/healthz", headers={"host": "hub.example.com", "cf-connecting-ip": "2.2.2.2", "cf-ray": "0123456789abcdef-SEA"})
    summary = client.get("/api/access-logs/summary?hours=1").json()
    hosts = {row["host"]: row for row in summary["hosts"]}
    assert hosts["health.example.com"]["requests"] == 1
    assert hosts["hub.example.com"]["requests"] == 1
