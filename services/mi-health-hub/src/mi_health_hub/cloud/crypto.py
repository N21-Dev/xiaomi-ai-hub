"""小米云请求的加密与签名。

协议要点（依据公开的逆向实现验证）：
1. 每次请求生成 `nonce` = 8 字节随机 + 大端 uint32(当前分钟数)
2. `signed_nonce` = SHA-256(ssecurity + nonce)，它同时充当 RC4 密钥
3. 表单先放 `data`（JSON 串），并据此算 `rc4_hash__`
4. 把表单所有值用 RC4 加密（**跳过前 1024 字节密钥流**）后 base64
5. 再对加密后的表单算 `signature`，最后附加明文 `_nonce`
6. 响应体是 base64，RC4 解密后才是 JSON

注意：**中国区同样需要加密与签名**，不是明文请求。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import struct
from datetime import datetime, timezone
from typing import Any

RC4_DROP = 1024


def gen_nonce() -> bytes:
    """8 字节随机 + 当前分钟数（大端 uint32）。"""
    raw = bytearray(os.urandom(8))
    raw.extend(struct.pack(">I", int(datetime.now(tz=timezone.utc).timestamp() // 60)))
    return bytes(raw)


def gen_signed_nonce(ssecurity: bytes, nonce: bytes) -> bytes:
    """signed_nonce = SHA-256(ssecurity ‖ nonce)，兼作 RC4 密钥。"""
    return hashlib.sha256(ssecurity + nonce).digest()


def rc4_crypt(key: bytes, payload: bytes) -> bytes:
    """RC4 流密码，丢弃前 1024 字节密钥流。加解密同一函数。"""
    if not key:
        raise ValueError("RC4 密钥不能为空")
    state = list(range(256))
    j = 0
    key_len = len(key)
    for i in range(256):
        j = (j + state[i] + key[i % key_len]) % 256
        state[i], state[j] = state[j], state[i]

    i = 0
    j = 0

    def next_byte() -> int:
        nonlocal i, j
        i = (i + 1) % 256
        j = (j + state[i]) % 256
        state[i], state[j] = state[j], state[i]
        return state[(state[i] + state[j]) % 256]

    for _ in range(RC4_DROP):
        next_byte()

    out = bytearray(len(payload))
    for index, value in enumerate(payload):
        out[index] = value ^ next_byte()
    return bytes(out)


def gen_signature(method: str, path: str, values: dict[str, str], signed_nonce: bytes) -> str:
    """signature = base64(SHA-1("METHOD&path&data=...&[rc4_hash__=...]&" + base64(signed_nonce)))。"""
    base = f"{method}&{path}&data={values['data']}"
    if "rc4_hash__" in values:
        base += f"&rc4_hash__={values['rc4_hash__']}"
    base += "&" + base64.b64encode(signed_nonce).decode()
    return base64.b64encode(hashlib.sha1(base.encode()).digest()).decode()


def encrypt_form(
    payload: dict[str, Any], ssecurity: bytes, api_path: str, method: str = "POST"
) -> tuple[dict[str, str], bytes]:
    """把业务 payload 组装成加密表单，返回 (表单字段, 本次使用的 signed_nonce)。"""
    nonce = gen_nonce()
    signed_nonce = gen_signed_nonce(ssecurity, nonce)

    form: dict[str, str] = {"data": json.dumps(payload, separators=(",", ":"))}
    form["rc4_hash__"] = gen_signature(method, api_path, form, signed_nonce)

    encrypted = {
        key: base64.b64encode(rc4_crypt(signed_nonce, value.encode())).decode()
        for key, value in form.items()
    }
    encrypted["signature"] = gen_signature(method, api_path, encrypted, signed_nonce)
    encrypted["_nonce"] = base64.b64encode(nonce).decode()
    return encrypted, signed_nonce


def decrypt_body(body_text: str, signed_nonce: bytes) -> dict[str, Any]:
    """解密响应体为 dict。"""
    raw = base64.b64decode(body_text)
    plaintext = rc4_crypt(signed_nonce, raw)
    return json.loads(plaintext)


def parse_login_payload(text: str) -> dict[str, Any]:
    """解析 account.xiaomi.com 的登录响应（`&&&START&&&` 前缀 + JSON）。"""
    prefix = "&&&START&&&"
    if not text.startswith(prefix):
        raise RuntimeError("account.xiaomi.com 返回了非预期的登录响应格式")
    return json.loads(text[len(prefix) :])
