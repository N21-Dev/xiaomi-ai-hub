"""加密与签名测试。

关键点：与公开的逆向实现对齐——RC4 丢弃前 1024 字节密钥流，
signed_nonce 兼作密钥，签名串拼装顺序不能错。
"""

from __future__ import annotations

import base64
import hashlib
import json

import pytest

from mi_health_hub.cloud.crypto import (
    RC4_DROP,
    decrypt_body,
    encrypt_form,
    gen_nonce,
    gen_signature,
    gen_signed_nonce,
    parse_login_payload,
    rc4_crypt,
)


def _reference_rc4(key: bytes, data: bytes, drop: int) -> bytes:
    """测试内独立实现的参照 RC4，用于交叉验证。"""
    s = list(range(256))
    j = 0
    for i in range(256):
        j = (j + s[i] + key[i % len(key)]) % 256
        s[i], s[j] = s[j], s[i]
    i = j = 0
    out = bytearray()
    for _ in range(drop):
        i = (i + 1) % 256
        j = (j + s[i]) % 256
        s[i], s[j] = s[j], s[i]
    for byte in data:
        i = (i + 1) % 256
        j = (j + s[i]) % 256
        s[i], s[j] = s[j], s[i]
        out.append(byte ^ s[(s[i] + s[j]) % 256])
    return bytes(out)


def test_rc4_drops_1024_keystream_bytes():
    key = b"0123456789abcdef"
    payload = b"the quick brown fox"
    assert rc4_crypt(key, payload) == _reference_rc4(key, payload, RC4_DROP)
    # 确认确实丢弃了：与不丢弃的 RC4 不同
    assert rc4_crypt(key, payload) != _reference_rc4(key, payload, 0)


def test_rc4_roundtrip_and_determinism():
    key = b"a" * 32
    plain = json.dumps({"hello": "世界", "n": 42}).encode()
    assert rc4_crypt(key, rc4_crypt(key, plain)) == plain
    assert rc4_crypt(key, plain) == rc4_crypt(key, plain)


def test_rc4_rejects_empty_key():
    with pytest.raises(ValueError):
        rc4_crypt(b"", b"data")


def test_signed_nonce_is_sha256_of_ssecurity_plus_nonce():
    ssecurity = b"\x01\x02\x03\x04"
    nonce = b"abcdefgh"
    assert gen_signed_nonce(ssecurity, nonce) == hashlib.sha256(ssecurity + nonce).digest()


def test_nonce_shape_is_12_bytes_with_minute_counter():
    nonce = gen_nonce()
    assert len(nonce) == 12
    minute = int.from_bytes(nonce[8:], "big")
    # 分钟数应当是一个合理的当前时间量级（约 2.9e7）
    assert 20_000_000 < minute < 60_000_000


def test_signature_matches_manual_construction():
    signed_nonce = b"\x00" * 32
    values = {"data": '{"key":"steps"}'}
    expected_base = (
        "POST&/app/v1/data/get_fitness_data_by_time&data="
        + values["data"]
        + "&"
        + base64.b64encode(signed_nonce).decode()
    )
    expected = base64.b64encode(hashlib.sha1(expected_base.encode()).digest()).decode()
    assert gen_signature("POST", "/app/v1/data/get_fitness_data_by_time", values, signed_nonce) == expected


def test_signature_includes_rc4_hash_when_present():
    signed_nonce = b"\x11" * 32
    without = {"data": "{}"}
    with_hash = {"data": "{}", "rc4_hash__": "ABC"}
    assert gen_signature("POST", "/p", with_hash, signed_nonce) != gen_signature(
        "POST", "/p", without, signed_nonce
    )


def test_encrypt_form_produces_expected_fields_and_roundtrips():
    ssecurity = b"ss" * 16
    path = "/app/v1/data/get_fitness_data_by_time"
    payload = {"start_time": 1, "end_time": 2, "key": "steps"}

    form, signed_nonce = encrypt_form(payload, ssecurity, path)

    assert set(form) == {"data", "rc4_hash__", "signature", "_nonce"}
    # 每个字段都应是 base64
    for key in ("data", "rc4_hash__", "signature", "_nonce"):
        base64.b64decode(form[key])

    # data 用 signed_nonce 解密后应还原原始 payload
    decrypted = rc4_crypt(signed_nonce, base64.b64decode(form["data"])).decode()
    assert json.loads(decrypted) == payload

    # _nonce 是明文 base64
    assert len(base64.b64decode(form["_nonce"])) == 12


def test_encrypt_form_uses_fresh_nonce_each_call():
    ssecurity = b"ss" * 16
    payload = {"key": "steps"}
    first, _ = encrypt_form(payload, ssecurity, "/p")
    second, _ = encrypt_form(payload, ssecurity, "/p")
    assert first["_nonce"] != second["_nonce"]
    assert first["data"] != second["data"]


def test_decrypt_body_roundtrip():
    body = {"code": 0, "result": {"data_list": [{"time": 1}]}}
    signed_nonce = b"\x22" * 32
    cipher = base64.b64encode(rc4_crypt(signed_nonce, json.dumps(body).encode())).decode()
    assert decrypt_body(cipher, signed_nonce) == body


def test_parse_login_payload_requires_prefix():
    with pytest.raises(RuntimeError):
        parse_login_payload('{"code":0}')
    parsed = parse_login_payload('&&&START&&&{"code":0,"userId":123}')
    assert parsed["userId"] == 123
