"""安卓安装包的分发目录。

约定：`STATE_DIR/android/` 下放 `manifest.json` 和对应的 `.apk`。
manifest 由 `ops/release/publish_android_app.py` 生成，客户端只读拿它能拿到版本与下载地址。

这里不做鉴权：安装包本身不是秘密，而更新不该被「还没登录」挡住。
也正因为如此，解析必须收紧——只接受 manifest 里写明的、位于本目录下的 `.apk`，
避免任何形式的路径穿越或大文件拖库。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

DIR_NAME = "android"
MANIFEST_NAME = "manifest.json"
MAX_APK_BYTES = 200 * 1024 * 1024


def release_dir(state_dir: Path) -> Path:
    return Path(state_dir) / DIR_NAME


def resolve(state_dir: Path) -> tuple[dict[str, Any], Path] | None:
    """返回 (manifest, apk 路径)；任何一项不合法都当作「没有发布」。"""
    directory = release_dir(state_dir)
    try:
        raw = json.loads((directory / MANIFEST_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None

    try:
        version_code = int(raw["versionCode"])
    except (KeyError, TypeError, ValueError):
        return None
    version_name = str(raw.get("versionName") or "").strip()
    file_name = str(raw.get("file") or "").strip()
    if version_code <= 0 or not version_name or not file_name:
        return None

    try:
        base = directory.resolve(strict=True)
        target = (base / file_name).resolve(strict=True)
    except OSError:
        return None
    # 只允许目录内的 .apk，且不能顺着符号链接跑到别处
    if target.parent != base or target.suffix.lower() != ".apk":
        return None
    try:
        size = target.stat().st_size
    except OSError:
        return None
    if not 0 < size <= MAX_APK_BYTES:
        return None

    manifest: dict[str, Any] = {
        "versionCode": version_code,
        "versionName": version_name,
        "file": file_name,
        "size": size,
        "sha256": str(raw.get("sha256") or "").strip().lower(),
    }
    for key in ("publishedAt", "notes", "minSdk"):
        value = raw.get(key)
        if value not in (None, ""):
            manifest[key] = value
    return manifest, target
