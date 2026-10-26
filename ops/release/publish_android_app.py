#!/usr/bin/env python3
"""把安卓构建产物发布到中枢的分发目录，供客户端「一键更新」拉取。

构建产物旁边会有 `version.json`（由 `apps/android/scripts/build.sh` 生成），
里面已经带了版本号、文件名和 sha256，这里只需要复制文件 + 写 manifest。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

DIR_NAME = "android"


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_version(apk: Path) -> tuple[int, str]:
    meta_path = apk.with_name("version.json")
    if not meta_path.is_file():
        return 0, ""
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except ValueError:
        return 0, ""
    try:
        code = int(meta.get("versionCode") or 0)
    except (TypeError, ValueError):
        code = 0
    return code, str(meta.get("versionName") or "").strip()


def main() -> int:
    parser = argparse.ArgumentParser(description="发布安卓安装包到中枢分发目录")
    parser.add_argument("--apk", required=True, type=Path, help="构建产物 .apk")
    parser.add_argument("--state-dir", required=True, type=Path,
                        help="中枢的 STATE_DIR（容器内 /data，宿主 ./data/bridge）")
    parser.add_argument("--notes", default="", help="更新说明，会显示在客户端")
    parser.add_argument("--version-code", type=int, default=0, help="覆盖 version.json 里的值")
    parser.add_argument("--version-name", default="", help="覆盖 version.json 里的值")
    parser.add_argument("--dry-run", action="store_true", help="只打印将要写入的内容")
    args = parser.parse_args()

    apk = args.apk.expanduser().resolve()
    if not apk.is_file():
        print(f"找不到安装包：{apk}", file=sys.stderr)
        return 1
    if apk.suffix.lower() != ".apk":
        print("只接受 .apk 文件", file=sys.stderr)
        return 1

    version_code, version_name = read_version(apk)
    version_code = args.version_code or version_code
    version_name = args.version_name or version_name
    if version_code <= 0 or not version_name:
        print("缺少版本信息：请用 build.sh 生成 version.json，或显式传 "
              "--version-code/--version-name", file=sys.stderr)
        return 1

    target_dir = args.state_dir.expanduser().resolve() / DIR_NAME
    manifest = {
        "versionCode": version_code,
        "versionName": version_name,
        "file": apk.name,
        "size": apk.stat().st_size,
        "sha256": file_sha256(apk),
        "publishedAt": datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds"),
        "notes": args.notes.strip()[:200],
    }

    if args.dry_run:
        print(f"目录：{target_dir}")
        print(json.dumps(manifest, ensure_ascii=False, indent=2))
        return 0

    target_dir.mkdir(parents=True, exist_ok=True)
    # 同一目录只保留当前这一份，避免每次更新都留一个旧包
    for stale in target_dir.glob("*.apk"):
        if stale.name != apk.name:
            stale.unlink()
            print(f"删除旧包：{stale.name}")
    shutil.copyfile(apk, target_dir / apk.name)
    (target_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(f"已发布 {version_name}（versionCode {version_code}）")
    print(f"  目录：{target_dir}")
    print(f"  文件：{apk.name}  {manifest['size']} 字节")
    print(f"  指纹：{manifest['sha256']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
