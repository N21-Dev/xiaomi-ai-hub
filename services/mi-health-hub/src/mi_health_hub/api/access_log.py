"""Cloudflare Tunnel / 局域网逐请求访问日志。

免费方案不依赖 Cloudflare Logpush：读取 Cloudflare 转发到源站的请求头，按天写入
JSONL。日志仅包含访问元数据，不记录 Cookie、Authorization、API Key、请求体或敏感
查询参数明文。
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import threading
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

CN_TZ = timezone(timedelta(hours=8))
_SAFE_HOST_RE = re.compile(r"^[a-z0-9.-]{1,253}(?::\d{1,5})?$", re.I)
_CF_RAY_RE = re.compile(r"^[0-9a-f]{16,32}(?:-([A-Z]{3}))?$", re.I)
# 只允许已知的低敏诊断参数值进入日志；未知参数保留键名、值一律打码。
# 这样即使攻击者主动把隐私/密钥塞进任意参数，也不会借日志落盘。
_SAFE_QUERY_KEYS = frozenset({
    "date", "start", "end", "kind", "type", "offset", "limit", "period",
    "days", "hours", "tier", "regenerate", "key", "keys", "metric",
})
_LOG_FILE_RE = re.compile(r"^access-(\d{4}-\d{2}-\d{2})(?:-(\d{3}))?\.jsonl$")


def _bounded(value: object, limit: int) -> str:
    text = str(value or "").replace("\r", " ").replace("\n", " ")
    return text[:limit]


def _valid_ip(value: object) -> str:
    text = str(value or "").strip()
    try:
        return str(ipaddress.ip_address(text))
    except ValueError:
        return ""


def sanitize_query(raw: str) -> str:
    """保留可诊断参数，但对疑似凭据/身份字段隐去值。"""
    if not raw:
        return ""
    try:
        pairs = parse_qsl(raw, keep_blank_values=True, max_num_fields=100)
    except ValueError:
        return "<invalid-query>"
    safe: list[tuple[str, str]] = []
    for raw_key, raw_value in pairs:
        key = _bounded(raw_key, 100)
        lowered = key.lower().replace("-", "_")
        safe.append(
            (key, _bounded(raw_value, 200) if lowered in _SAFE_QUERY_KEYS else "<redacted>")
        )
    return _bounded(urlencode(safe, doseq=True), 1000)


def sanitize_referer(raw: str) -> str:
    if not raw:
        return ""
    try:
        parts = urlsplit(raw)
        if parts.scheme not in {"http", "https"}:
            return ""
        return _bounded(
            urlunsplit((parts.scheme, parts.netloc, parts.path, sanitize_query(parts.query), "")),
            600,
        )
    except ValueError:
        return ""


def classify_source(headers: Any, peer_ip: str) -> str:
    """区分 Cloudflare / LAN / 直连。

    Tunnel 连接在本部署中来自 Docker/局域网私网地址；公网直连端口只绑定 NAS 内网 IP。
    只有「私网 peer + 合法 CF-Ray + 合法 CF-Connecting-IP」才信任为 Cloudflare。
    这不是密码学认证，但可阻止普通公网直连伪造 Cloudflare 头；源站若将来直接暴露公网，
    应再配防火墙或 Authenticated Origin Pulls。
    """
    cf_ip = _valid_ip(headers.get("cf-connecting-ip"))
    ray = str(headers.get("cf-ray") or "").strip()
    try:
        peer = ipaddress.ip_address(peer_ip)
    except ValueError:
        peer = None
    peer_trusted = bool(peer and (peer.is_loopback or peer.is_private or peer.is_link_local))
    if peer_trusted and cf_ip and _CF_RAY_RE.fullmatch(ray):
        return "cloudflare"
    if peer_trusted:
        return "lan"
    return "direct"


def build_access_entry(
    *,
    request: Any,
    status: int,
    duration_ms: float,
    response_bytes: int,
    now: datetime | None = None,
) -> dict[str, Any]:
    """从 Starlette Request 构造安全、定长的日志记录。"""
    headers = request.headers
    peer_ip = _valid_ip(request.client.host if request.client else "")
    source = classify_source(headers, peer_ip)
    cf_ip = _valid_ip(headers.get("cf-connecting-ip"))
    client_ip = cf_ip if source == "cloudflare" else peer_ip
    ray = _bounded(headers.get("cf-ray"), 80)
    ray_match = _CF_RAY_RE.fullmatch(ray)
    cf_colo = ray_match.group(1).upper() if ray_match and ray_match.group(1) else ""
    host = _bounded(headers.get("host"), 253).lower()
    if not _SAFE_HOST_RE.fullmatch(host):
        host = "invalid-host"
    scheme = _bounded(headers.get("x-forwarded-proto") or request.url.scheme, 12).lower()
    if scheme not in {"http", "https"}:
        scheme = "unknown"
    request_bytes = 0
    try:
        request_bytes = max(0, int(headers.get("content-length") or 0))
    except (TypeError, ValueError):
        pass
    timestamp = (now or datetime.now(tz=CN_TZ)).astimezone(CN_TZ).isoformat(timespec="milliseconds")
    return {
        "ts": timestamp,
        "source": source,
        "client_ip": client_ip,
        "peer_ip": peer_ip,
        "country": _bounded(headers.get("cf-ipcountry"), 8).upper(),
        "city": _bounded(headers.get("cf-ipcity"), 120),
        "region": _bounded(headers.get("cf-region"), 120),
        "host": host,
        "method": _bounded(request.method, 12).upper(),
        "path": _bounded(request.url.path, 1000),
        "query": sanitize_query(request.url.query),
        "status": int(status),
        "duration_ms": round(max(0.0, duration_ms), 2),
        "request_bytes": request_bytes,
        "response_bytes": max(0, int(response_bytes or 0)),
        "user_agent": _bounded(headers.get("user-agent"), 600),
        "referer": sanitize_referer(str(headers.get("referer") or "")),
        "cf_ray": ray,
        "cf_colo": cf_colo,
        "http_version": _bounded(request.scope.get("http_version"), 12),
        "scheme": scheme,
    }


class AccessLogStore:
    """线程安全 JSONL 存储；按中国时区日期轮转并自动清理旧日志。"""

    def __init__(
        self,
        directory: str | os.PathLike[str],
        *,
        retention_days: int = 90,
        max_file_bytes: int = 20 * 1024 * 1024,
        max_files_per_day: int = 5,
    ) -> None:
        self.directory = Path(directory)
        self.retention_days = max(1, min(int(retention_days), 3650))
        self.max_file_bytes = max(1 * 1024 * 1024, min(int(max_file_bytes), 1024 * 1024 * 1024))
        self.max_files_per_day = max(1, min(int(max_files_per_day), 1000))
        self._lock = threading.RLock()
        self._last_prune_date = ""
        self.directory.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            os.chmod(self.directory, 0o700)

    def append(self, entry: dict[str, Any]) -> None:
        day = str(entry.get("ts") or "")[:10]
        try:
            datetime.strptime(day, "%Y-%m-%d")
        except ValueError:
            day = datetime.now(tz=CN_TZ).date().isoformat()
        line = json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n"
        with self._lock:
            target = self._writable_target(day, len(line.encode("utf-8")))
            with target.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(line)
            if os.name != "nt":
                os.chmod(target, 0o600)
            if self._last_prune_date != day:
                self._prune(datetime.fromisoformat(day).date())
                self._last_prune_date = day

    def _writable_target(self, day: str, incoming_bytes: int) -> Path:
        primary = self.directory / f"access-{day}.jsonl"
        candidates = [primary, *sorted(self.directory.glob(f"access-{day}-???.jsonl"))]
        current = candidates[-1]
        try:
            if current.stat().st_size + incoming_bytes <= self.max_file_bytes:
                return current
        except OSError:
            return current
        suffix = 1 if current == primary else int(current.stem.rsplit("-", 1)[-1]) + 1
        if suffix >= self.max_files_per_day:
            # 达到单日硬上限后不再增长；调用方会把日志失败隔离于业务请求。
            raise OSError("访问日志达到单日容量上限")
        return self.directory / f"access-{day}-{suffix:03d}.jsonl"

    def _prune(self, today: Any) -> None:
        cutoff = today - timedelta(days=self.retention_days)
        for path in self.directory.glob("access-*.jsonl"):
            match = _LOG_FILE_RE.fullmatch(path.name)
            if not match:
                continue
            try:
                day = datetime.strptime(match.group(1), "%Y-%m-%d").date()
                if day < cutoff:
                    path.unlink()
            except (OSError, ValueError):
                continue

    def query(
        self,
        *,
        hours: int = 24,
        host: str = "",
        ip: str = "",
        path: str = "",
        status: int | None = None,
        limit: int = 200,
        max_scan: int = 100_000,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        current = (now or datetime.now(tz=CN_TZ)).astimezone(CN_TZ)
        cutoff = current - timedelta(hours=max(1, hours))
        host_filter = host.strip().lower()
        ip_filter = ip.strip()
        path_filter = path.strip().lower()
        found: list[dict[str, Any]] = []
        scanned = 0
        truncated = False
        with self._lock:
            files = sorted(self.directory.glob("access-*.jsonl"), reverse=True)
            for log_path in files:
                match = _LOG_FILE_RE.fullmatch(log_path.name)
                if not match:
                    continue
                try:
                    file_day = datetime.strptime(match.group(1), "%Y-%m-%d").date()
                except ValueError:
                    continue
                if file_day < (cutoff - timedelta(days=1)).date():
                    break
                try:
                    lines = self._read_recent_lines(log_path, max_scan - scanned)
                except OSError:
                    continue
                for line in lines:
                    scanned += 1
                    if scanned > max_scan:
                        truncated = True
                        break
                    try:
                        item = json.loads(line)
                        when = datetime.fromisoformat(str(item.get("ts") or ""))
                    except (json.JSONDecodeError, TypeError, ValueError):
                        continue
                    if when < cutoff:
                        continue
                    if host_filter and str(item.get("host") or "").lower() != host_filter:
                        continue
                    if ip_filter and str(item.get("client_ip") or "") != ip_filter:
                        continue
                    if path_filter and path_filter not in str(item.get("path") or "").lower():
                        continue
                    if status is not None and int(item.get("status") or 0) != status:
                        continue
                    found.append(item)
                    if len(found) >= limit:
                        return {
                            "items": found,
                            "count": len(found),
                            "scanned": scanned,
                            "truncated": True,
                        }
                if truncated:
                    break
        return {"items": found, "count": len(found), "scanned": scanned, "truncated": truncated}

    @staticmethod
    def _read_recent_lines(path: Path, max_lines: int, block_size: int = 64 * 1024):
        """从文件尾部有界读取，避免日志文件增大后一次性占满内存。"""
        if max_lines <= 0:
            return
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            position = handle.tell()
            buffer = b""
            yielded = 0
            while position > 0 and yielded < max_lines:
                size = min(block_size, position)
                position -= size
                handle.seek(position)
                buffer = handle.read(size) + buffer
                parts = buffer.split(b"\n")
                buffer = parts[0]
                for raw in reversed(parts[1:]):
                    if raw:
                        yield raw.decode("utf-8", "replace")
                        yielded += 1
                        if yielded >= max_lines:
                            return
            if buffer and yielded < max_lines:
                yield buffer.decode("utf-8", "replace")

    def summary(self, *, hours: int = 24, host: str = "", now: datetime | None = None) -> dict[str, Any]:
        result = self.query(hours=hours, host=host, limit=100_000, max_scan=100_000, now=now)
        items = result["items"]
        ips = {str(x.get("client_ip") or "") for x in items if x.get("client_ip")}
        sources = Counter(str(x.get("source") or "unknown") for x in items)
        hosts: dict[str, dict[str, Any]] = {}
        for item in items:
            name = str(item.get("host") or "unknown")
            block = hosts.setdefault(name, {"requests": 0, "ips": set()})
            block["requests"] += 1
            if item.get("client_ip"):
                block["ips"].add(str(item["client_ip"]))
        country_counts = Counter(str(x.get("country") or "--") for x in items)
        path_counts = Counter(str(x.get("path") or "/") for x in items)
        status_counts = Counter(int(x.get("status") or 0) for x in items)
        return {
            "hours": hours,
            "total": len(items),
            "unique_ips": len(ips),
            "cloudflare": sources.get("cloudflare", 0),
            "direct": sources.get("direct", 0) + sources.get("lan", 0),
            "errors": sum(v for k, v in status_counts.items() if k >= 400),
            "hosts": [
                {"host": name, "requests": block["requests"], "unique_ips": len(block["ips"])}
                for name, block in sorted(hosts.items(), key=lambda pair: pair[1]["requests"], reverse=True)
            ],
            "countries": [
                {"country": name, "requests": count} for name, count in country_counts.most_common(12)
            ],
            "top_paths": [
                {"path": name, "requests": count} for name, count in path_counts.most_common(15)
            ],
            "status_codes": [
                {"status": code, "requests": count} for code, count in sorted(status_counts.items())
            ],
            "truncated": bool(result["truncated"]),
        }
