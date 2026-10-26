#!/usr/bin/env python3
"""Initialize a self-hosted checkout without putting passwords in Git or argv."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import secrets
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def write_private(path: Path, text: str) -> None:
    """Exclusive creation prevents overwriting an existing installation."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        output.write(text)


def initialize(with_health: bool) -> None:
    template = ROOT / ".env.example"
    targets = [ROOT / name for name in (
        ".env", "config/bridge.env", "config/health.env", "config/health-api-key",
    )]
    if (ROOT / "config").is_symlink() or (ROOT / "data").is_symlink():
        raise ValueError("config 和 data 必须是当前安装目录中的真实目录")
    for target in targets:
        if target.exists() or target.is_symlink():
            raise ValueError(f"{target.relative_to(ROOT)} 已存在，所有原配置保留；请使用 build、admin 或 start")
    for component in ("bridge", "health"):
        if (ROOT / "data" / component).is_symlink():
            raise ValueError("数据目录不能是符号链接")
    uid = os.getuid() if hasattr(os, "getuid") else 10001
    gid = os.getgid() if hasattr(os, "getgid") else 10001
    if uid == 0:
        uid = gid = 10001
    values = template.read_text(encoding="utf-8")
    values += f"\nPUID={uid}\nPGID={gid}\n"
    health_key = secrets.token_urlsafe(32)
    bridge_template = (ROOT / "config/bridge.env.example").read_text(encoding="utf-8")
    if with_health:
        bridge_template += "\nHEALTH_SERVICE_URL=http://mi-health-hub:8321\nHEALTH_WEB_URL=http://localhost:8321\nHEALTH_API_KEY_FILE=/run/secrets/health-api-key\n"
        values += "COMPOSE_PROFILES=health\n"
    write_private(ROOT / "config/bridge.env", bridge_template)
    write_private(ROOT / "config/health.env", f"MI_API_KEY={health_key}\nMI_TRUST_PRIVATE=false\nMI_AI_ENABLED=false\nMI_SENTINEL_ENABLED=false\n")
    write_private(ROOT / "config/health-api-key", health_key + "\n")
    write_private(ROOT / ".env", values)
    for component in ("bridge", "health"):
        directory = ROOT / "data" / component
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            os.chown(directory, uid, gid)
    for path in (ROOT / "config/bridge.env", ROOT / "config/health.env", ROOT / "config/health-api-key"):
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            os.chown(path, uid, gid)
    print("本地配置和独立数据目录已创建；密钥未输出。下一步：python3 ops/setup.py build")


def compose(*arguments: str, capture_output: bool = False) -> subprocess.CompletedProcess:
    if not shutil.which("docker"):
        raise ValueError("请先安装 Docker Engine 和 Docker Compose v2")
    return subprocess.run(["docker", "compose", *arguments], cwd=ROOT, check=True,
                          text=True, capture_output=capture_output)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("init", "build", "admin", "start", "check"))
    parser.add_argument("--with-health", action="store_true", help="init 时同时配置独立健康服务")
    parser.add_argument("--username", help="admin 时的管理员名称；省略则交互输入")
    args = parser.parse_args()
    try:
        if args.command == "init":
            initialize(args.with_health)
        elif args.command == "build":
            compose("build")
        elif args.command == "check":
            compose("config", "--quiet")
        elif args.command == "admin":
            username = args.username or input("管理员名称: ").strip()
            if not username:
                raise ValueError("管理员名称不能为空")
            compose("run", "--rm", "--no-deps", "--entrypoint", "python", "xiaomi-ai-bridge",
                    "-m", "xiaomi_ai_bridge.panel_auth", "set-admin", "--username", username)
            running = compose("ps", "--status", "running", "-q", "xiaomi-ai-bridge", capture_output=True)
            if running.stdout.strip():
                # Authentication state is cached in the server process. Reload
                # it immediately so a password reset revokes old sessions.
                compose("restart", "--no-deps", "xiaomi-ai-bridge")
                print("账号已保存，正在运行的中枢已重启以撤销旧会话。")
            else:
                print("账号已保存。运行 python3 ops/setup.py start 启动服务。")
        else:
            compose("up", "-d", "--no-build", "--wait", "--wait-timeout", "120")
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"初始化未完成：{error}\n")


if __name__ == "__main__":
    main()
