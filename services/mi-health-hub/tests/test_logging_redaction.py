"""脱敏测试。凭据泄露等于账号被接管，这里必须覆盖到位。"""

from __future__ import annotations

import logging

from mi_health_hub.logging_setup import REDACTED, SensitiveFilter, scrub, scrub_mapping


def test_scrub_masks_passtoken_in_key_value_form():
    text = "login failed for passToken=AbCdEf123456 userId=1234567"  # gitleaks:allow -- synthetic redaction fixture
    cleaned = scrub(text)
    assert "AbCdEf123456" not in cleaned
    assert "1234567" not in cleaned
    assert REDACTED in cleaned


def test_scrub_masks_cookie_header():
    cleaned = scrub("Cookie: serviceToken=xyz; userId=99; passToken=secret")
    assert "xyz" not in cleaned
    assert "secret" not in cleaned


def test_scrub_masks_long_random_tokens():
    token = "a" * 80
    cleaned = scrub(f"got token {token} from server")
    assert token not in cleaned


def test_scrub_kills_login_payload_prefix_content():
    cleaned = scrub('&&&START&&&{"userId":123,"ssecurity":"abc","passToken":"def"}')
    assert cleaned == "&&&START&&&" + REDACTED
    assert "ssecurity" not in cleaned


def test_scrub_mapping_masks_sensitive_keys_recursively():
    payload = {
        "ok": True,
        "passToken": "secret-value",
        "nested": {"user_id": "1234567", "note": "passToken=secret-value"},
        "items": [{"cookie": "a=1; b=2"}],
    }
    cleaned = scrub_mapping(payload)
    assert cleaned["passToken"] == REDACTED
    assert cleaned["nested"]["user_id"] == REDACTED
    assert "secret-value" not in cleaned["nested"]["note"]
    assert cleaned["items"][0]["cookie"] == REDACTED
    assert cleaned["ok"] is True


def test_sensitive_filter_cleans_log_record_args():
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="connecting with passToken=%s",
        args=("SuperSecretToken123",),
        exc_info=None,
    )
    assert SensitiveFilter().filter(record) is True
    rendered = record.getMessage()
    assert "SuperSecretToken123" not in rendered
    assert REDACTED in rendered


def test_sensitive_filter_handles_broken_format_args():
    """格式化失败不能让日志系统抛异常，否则容器会静默丢日志。"""
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="value %d",
        args=("not-an-int",),
        exc_info=None,
    )
    assert SensitiveFilter().filter(record) is True
    assert isinstance(record.msg, str)


def test_scrub_leaves_normal_text_intact():
    assert scrub("同步完成，写入 120 行") == "同步完成，写入 120 行"
    assert scrub("") == ""
