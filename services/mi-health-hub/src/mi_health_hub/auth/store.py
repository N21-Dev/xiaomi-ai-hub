"""凭据持久化。

小米的 `serviceLogin` 会**轮换 passToken**，所以登录后必须把新值写回，
否则下次启动可能用已被作废的旧 token。

安全要求：
- 文件权限必须是 0640（或更严），启动/保存时校验，不合规就拒绝
- 内容不进日志、不进 git
"""

from __future__ import annotations

import json
import logging
import os
import stat
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# 健康服务使用独立凭据文件，不能和 micoapi 等其他 SID 共享轮换 passToken。
DEFAULT_CREDENTIALS_PATH = "/var/lib/mi-health-hub/health-credentials.json"
MAX_ALLOWED_MODE = 0o640


class CredentialError(RuntimeError):
    pass


def _check_permissions(path: Path) -> None:
    """文件权限过宽直接拒绝——凭据泄露等于账号被接管。"""
    if os.name == "nt":  # Windows 无 POSIX 权限位，交给上层 ACL 控制
        return
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & ~MAX_ALLOWED_MODE:
        raise CredentialError(
            f"凭据文件 {path} 权限为 {oct(mode)}，过宽。请执行：chmod 640 {path}"
        )


@dataclass
class Credentials:
    user_id: str
    pass_token: str
    region: str = "cn"
    device_id: str = ""
    c_user_id: str = ""


class CredentialStore:
    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        self.path = Path(path or os.environ.get("MI_CREDENTIALS_FILE") or DEFAULT_CREDENTIALS_PATH)

    def exists(self) -> bool:
        return self.path.is_file()

    def load(self) -> Credentials | None:
        if not self.exists():
            return None
        _check_permissions(self.path)
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if not data.get("user_id") or not data.get("pass_token"):
            return None
        return Credentials(
            user_id=str(data["user_id"]),
            pass_token=str(data["pass_token"]),
            region=str(data.get("region") or "cn"),
            device_id=str(data.get("device_id") or ""),
            c_user_id=str(data.get("c_user_id") or ""),
        )

    def save(self, credentials: Credentials) -> None:
        payload = {
            "user_id": credentials.user_id,
            "pass_token": credentials.pass_token,
            "region": credentials.region,
            "device_id": credentials.device_id,
            "c_user_id": credentials.c_user_id,
            "note": "passToken 等同小米账号登录态，泄露即可被完全接管账号。请勿提交或分享。",
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.exists():
            _check_permissions(self.path)
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        if os.name != "nt":
            os.chmod(temp, MAX_ALLOWED_MODE)
        temp.replace(self.path)
        logger.info("凭据已写入 %s（权限 640）", self.path)
