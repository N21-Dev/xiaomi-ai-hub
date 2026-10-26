"""小米健康网页登录状态机与凭据自动捕获。"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass

import httpx
import pytest
from fastapi.testclient import TestClient

from mi_health_hub.auth.web_login import XiaomiHealthWebLogin
from mi_health_hub.api.app import create_app
from mi_health_hub.config import load_config

PREFIX = "&&&START&&&"


def _resp(payload: dict, status: int = 200) -> httpx.Response:
    request = httpx.Request("GET", "https://account.xiaomi.com/x")
    return httpx.Response(status, request=request, text=PREFIX + json.dumps(payload))


class FakeCookies(dict):
    def items(self):
        return super().items()


class FakeClient:
    def __init__(self, responses: list[httpx.Response], cookies: dict[str, str] | None = None):
        self.responses = list(responses)
        self.cookies = FakeCookies(cookies or {})
        self.closed = False
        self.calls: list[tuple[str, dict]] = []

    def get(self, url: str, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)

    def close(self):
        self.closed = True


def _login_client(
    *,
    poll_payload: dict | None = None,
    cookies: dict[str, str] | None = None,
) -> FakeClient:
    return FakeClient(
        [
            _resp({
                # miothealth 未登录初始化实测返回 70016，但仍携带完整网页登录参数。
                "code": 70016,
                "description": "登录验证失败",
                "serviceParam": "sp",
                "qs": "qs",
                "callback": "cb",
                "_sign": "sign",
            }),
            _resp({
                "loginUrl": "https://c3.account.xiaomi.com/longPolling/login",
                "qr": "https://account.xiaomi.com/pass/qr/login?ticket=test",
                "qrTips": "请使用小米账号扫码登录",
                "lp": "https://c3.lp.account.xiaomi.com/lp/s",
                "timeout": 60,
            }),
            _resp(poll_payload or {"code": 0}),
        ],
        cookies=cookies,
    )


def _wait_state(login: XiaomiHealthWebLogin, expected: str, timeout: float = 2) -> dict:
    deadline = time.time() + timeout
    status = login.public_status()
    while time.time() < deadline and status["state"] != expected:
        time.sleep(0.01)
        status = login.public_status()
    return status


def test_start_returns_health_login_url_and_uses_miothealth():
    client = _login_client(cookies={"userId": "123", "passToken": "p", "deviceId": "d"})
    accepted = []
    login = XiaomiHealthWebLogin(accepted.append, client_factory=lambda: client)
    status = login.start()
    assert status["state"] in ("waiting", "validating", "success")
    if status["state"] == "waiting":
        assert status["qr_url"].startswith("https://account.xiaomi.com/pass/qr/login")
        assert "小米账号" in status["qr_tips"]
    assert all("account.xiaomi.com" in url for url, _ in client.calls[:2])
    # 第一阶段必须使用健康服务自己的 SID，不能错用小爱的 micoapi
    assert client.calls[0][1]["params"]["sid"] == "miothealth"


def test_captures_cookie_validates_and_never_exposes_secret():
    secret = "top-secret-pass-token"
    accepted: list[dict[str, str]] = []
    client = _login_client(
        cookies={
            "userId": "123456",
            "passToken": secret,
            "deviceId": "device-1",
            "cUserId": "c-user",
        }
    )
    login = XiaomiHealthWebLogin(accepted.append, client_factory=lambda: client)
    login.start()
    status = _wait_state(login, "success")
    assert status["state"] == "success"
    assert status["credentials_updated"] is True
    assert accepted == [{
        "user_id": "123456",
        "pass_token": secret,
        "device_id": "device-1",
        "c_user_id": "c-user",
    }]
    assert secret not in json.dumps(status, ensure_ascii=False)
    assert "123456" not in json.dumps(status, ensure_ascii=False)


def test_payload_fields_override_cookie_values():
    accepted = []
    client = _login_client(
        poll_payload={
            "code": 0,
            "userId": "new-user",
            "passToken": "new-token",
            "deviceId": "new-device",
        },
        cookies={"userId": "old", "passToken": "old-token", "deviceId": "old-device"},
    )
    login = XiaomiHealthWebLogin(accepted.append, client_factory=lambda: client)
    login.start()
    assert _wait_state(login, "success")["state"] == "success"
    assert accepted[0]["user_id"] == "new-user"
    assert accepted[0]["pass_token"] == "new-token"
    assert accepted[0]["device_id"] == "new-device"


def test_duplicate_cookie_names_across_xiaomi_subdomains_are_supported():
    from http.cookiejar import Cookie, CookieJar

    def cookie(name: str, value: str, domain: str) -> Cookie:
        return Cookie(
            version=0,
            name=name,
            value=value,
            port=None,
            port_specified=False,
            domain=domain,
            domain_specified=True,
            domain_initial_dot=domain.startswith("."),
            path="/",
            path_specified=True,
            secure=True,
            expires=None,
            discard=True,
            comment=None,
            comment_url=None,
            rest={},
        )

    jar = CookieJar()
    jar.set_cookie(cookie("cUserId", "old-cuser", ".account.xiaomi.com"))
    jar.set_cookie(cookie("cUserId", "new-cuser", "c3.account.xiaomi.com"))
    jar.set_cookie(cookie("userId", "123", ".account.xiaomi.com"))
    jar.set_cookie(cookie("passToken", "pass", ".account.xiaomi.com"))
    jar.set_cookie(cookie("deviceId", "device", ".account.xiaomi.com"))
    jar.set_cookie(cookie("evil", "nope", ".evil.example"))

    class CookiesWithJar:
        def __init__(self):
            self.jar = jar

        def items(self):
            raise httpx.CookieConflict("duplicate cUserId")

    client = type("Client", (), {"cookies": CookiesWithJar()})()
    values = XiaomiHealthWebLogin._cookie_dict(client)
    assert values["cUserId"] in {"old-cuser", "new-cuser"}
    assert values["userId"] == "123"
    assert values["passToken"] == "pass"
    assert "evil" not in values


def test_missing_cookie_moves_to_error_without_saving():
    accepted = []
    client = _login_client(cookies={"userId": "123"})
    login = XiaomiHealthWebLogin(accepted.append, client_factory=lambda: client)
    login.start()
    status = _wait_state(login, "error")
    assert status["state"] == "error"
    assert accepted == []
    assert status["login_url"] == ""


def test_validation_failure_does_not_commit_credentials():
    def reject(_: dict[str, str]) -> None:
        raise RuntimeError("小米登录失败（code=70016）：登录验证失败")

    client = _login_client(cookies={"userId": "123", "passToken": "bad", "deviceId": "d"})
    login = XiaomiHealthWebLogin(reject, client_factory=lambda: client)
    login.start()
    status = _wait_state(login, "error")
    assert status["state"] == "error"
    assert "70016" in status["message"]
    assert status["credentials_updated"] is False


def test_allows_official_xiaomi_account_subdomains():
    assert XiaomiHealthWebLogin._require_xiaomi_https(
        "https://c3.account.xiaomi.com/longPolling/login", "登录"
    ).startswith("https://")
    assert XiaomiHealthWebLogin._require_xiaomi_https(
        "https://c3.lp.account.xiaomi.com/lp/s", "轮询"
    ).startswith("https://")


def test_rejects_non_xiaomi_login_url():
    client = FakeClient([
        _resp({"code": 70016, "serviceParam": "sp", "qs": "qs", "callback": "cb", "_sign": "s"}),
        _resp({"loginUrl": "https://evil.example/login", "qr": "https://account.xiaomi.com/pass/qr/login", "lp": "https://account.xiaomi.com/lp"}),
    ])
    login = XiaomiHealthWebLogin(lambda _: None, client_factory=lambda: client)
    with pytest.raises(RuntimeError, match="地址无效"):
        login.start()
    assert login.public_status()["state"] == "error"


def test_cancel_is_idempotent():
    login = XiaomiHealthWebLogin(lambda _: None)
    assert login.cancel()["state"] == "idle"
    assert login.cancel()["state"] == "idle"


def _app(tmp_path):
    config = load_config(tmp_path / "none.yaml")
    config.paths.db = tmp_path / "t.db"
    config.paths.raw = tmp_path / "raw"
    config.paths.output = tmp_path / "out"
    config.paths.backup = tmp_path / "bak"
    return create_app(config)


def test_login_status_endpoint_never_returns_credentials(tmp_path):
    client = TestClient(_app(tmp_path))
    response = client.get("/api/auth/xiaomi/status")
    assert response.status_code == 200
    assert set(response.json()) == {
        "state", "login_url", "qr_url", "qr_tips", "message", "expires_at", "credentials_updated"
    }
    assert "pass_token" not in response.text.lower()
    assert "cookie" not in response.text.lower()


def test_proxied_request_cannot_start_or_cancel_login(tmp_path):
    client = TestClient(_app(tmp_path))
    headers = {"cf-connecting-ip": "203.0.113.7"}
    assert client.post("/api/auth/xiaomi/start", headers=headers).status_code == 403
    assert client.post("/api/auth/xiaomi/cancel", headers=headers).status_code == 403


def test_login_callback_validates_before_persisting(tmp_path, monkeypatch):
    from mi_health_hub.api import app as app_mod

    events: list[str] = []

    class StubHealthClient:
        def __init__(self, user_id, pass_token, **kwargs):
            self.user_id = user_id
            self.pass_token = pass_token

        def login(self):
            events.append("validated")
            self.pass_token = "rotated-and-validated"

        def close(self):
            events.append("closed")

    monkeypatch.setenv("MI_CREDENTIALS_FILE", str(tmp_path / "credentials.json"))
    monkeypatch.setattr(app_mod, "MiHealthClient", StubHealthClient)
    app = _app(tmp_path)
    app.state.xiaomi_login.on_credentials({
        "user_id": "123456",
        "pass_token": "browser-token",
        "device_id": "device-1",
        "c_user_id": "c-user",
    })
    stored = json.loads((tmp_path / "credentials.json").read_text(encoding="utf-8"))
    assert events == ["validated", "closed"]
    assert stored["pass_token"] == "rotated-and-validated"
    assert stored["device_id"] == "device-1"
    assert stored["c_user_id"] == "c-user"
