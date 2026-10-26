"""只读 API。默认只绑 127.0.0.1；对局域网开放时建议配置 MI_API_KEY。

设计约定：
- API **不触发同步**（同步由调度器或管理端点单独触发），避免一次页面刷新就打到小米接口。
- **每个请求一条 SQLite 连接，在该请求的线程内创建并关闭。**

⚠️ 连接必须在**端点函数体内**创建，不要写成 FastAPI 的 `yield` 依赖。
FastAPI 会在线程池里执行同步的 `yield` 依赖：`__enter__` 可能落在 A 线程，
而端点函数跑在 B 线程，于是 sqlite3 抛
`ProgrammingError: SQLite objects created in a thread can only be used in that same thread`。
症状是**间歇性 500**——单独 curl 能过，页面并发几个请求就挂，非常难查。
（这个坑在调度器里也踩过一次，那次是主线程建连接、任务线程用。）
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from .. import __version__
from ..auth.store import CredentialStore, Credentials
from ..auth.web_login import XiaomiHealthWebLogin
from ..cloud.client import MiHealthClient, RequestBudget
from ..ai import (
    AIClient,
    AIConfigProvider,
    AIError,
    AISettingsError,
    AISettingsStore,
    CHAT_USER_TEMPLATE,
    PROMPT_VERSION,
    workout_type_zh,
)
from ..analysis.daily import build_daily_payload, _format_pace
from ..ai.cache import cache_matches, input_fingerprint
from ..analysis.period import build_period_stats
from ..analysis.report import build_report_payload, resolve_period_range
from ..config import AIConfig, Config
from .access_log import AccessLogStore, build_access_entry
from .public_web import PublicWebError, read_public_page
from ..scheduler import _sync_lock
from ..store.repository import Repository, connect, init_db
from ..sync.engine import build_engine

logger = logging.getLogger(__name__)

CN_TZ = timezone(timedelta(hours=8))


class ChatTurn(BaseModel):
    """对话历史中的一轮。"""

    role: str  # user / assistant
    content: str


class ChatRequest(BaseModel):
    """POST /api/ai/chat 的请求体。

    history 由前端在内存里维护（刷新页面即消失），服务端**不落库**：
    健康数据是常态上下文，多轮上下文仅限本次对话。
    """

    message: str
    history: list[ChatTurn] = []


class NoteRequest(BaseModel):
    """POST /api/ai/notes 的请求体。"""

    content: str
    note_date: str | None = None


class ReportRequest(BaseModel):
    """POST /api/ai/report 的请求体。"""

    type: str  # weekly / monthly
    regenerate: bool = False


class AISettingsRequest(BaseModel):
    """保存或测试 AI 设置。API Key 留空表示保留已保存值。"""

    enabled: bool = False
    protocol: str
    base_url: str
    api_key: str | None = Field(default=None, max_length=4096)
    clear_api_key: bool = False
    model: str = Field(min_length=1, max_length=300)
    timeout_s: float = Field(default=120, ge=5, le=600)
    max_tokens: int = Field(default=8000, ge=64, le=128000)
    temperature: float = Field(default=0.3, ge=0, le=2)
    auto_after_sync: bool = True
    auto_period_reports: bool = True
    json_mode: bool = True
    allow_private_base_url: bool = False
    anthropic_version: str = Field(default="2023-06-01", max_length=40)
    detail_level: str = "detailed"
    include_health_notes: bool = True
    web_tools_enabled: bool = True
    custom_instructions: str = Field(default="", max_length=4000)


# 手动「立即同步」的状态（单实例内存态，重启即清零）
_sync_state: dict[str, Any] = {
    "running": False,
    "started_at": None,
    "finished_at": None,
    "ok": None,
    "rows": 0,
    "note": "",
}


def _run_manual_sync(cfg: Config) -> None:
    """后台线程体：强制增量同步（与定时任务共用 _sync_lock 互斥）。

    手动同步不能走只检查 steps 游标的 T0 哨兵，否则心率、睡眠等数据在
    steps 未推进时会被错误跳过。任何异常都只落状态不打断服务；连接在本线程内
    创建（SQLite 跨线程限制）。
    """
    conn = None
    try:
        conn = connect(cfg.paths.db)
        init_db(conn)
        repo = Repository(conn)
        engine = build_engine(cfg, repo)
        try:
            result = engine.run("T1")
            _sync_state["rows"] = result.rows_written
            _sync_state["ok"] = not result.failed_keys
            _sync_state["note"] = (
                f"tier={result.tier} status={result.status} rows={result.rows_written}"
                + (f" failed={','.join(result.failed_keys)}" if result.failed_keys else "")
                + (f" note={result.note}" if result.note else "")
            )
            if result.rows_written:
                try:
                    engine.backup_database()
                except Exception:  # noqa: BLE001 - 备份失败不影响同步结果
                    logger.warning("同步后备份失败", exc_info=True)
        finally:
            engine.client.close()
    except Exception as exc:  # noqa: BLE001 - 后台线程不能把异常抛给没人接的地方
        logger.warning("手动同步失败：%s", type(exc).__name__)
        _sync_state["ok"] = False
        _sync_state["note"] = f"{type(exc).__name__}: {exc}"
    finally:
        if conn is not None:
            conn.close()
        _sync_lock.release()
        _sync_state["running"] = False
        _sync_state["finished_at"] = time.time()


def _fmt_duration_min(seconds: float) -> str:
    total_min = int(seconds // 60)
    return f"{total_min // 60}小时{total_min % 60}分" if total_min >= 60 else f"{total_min}分"


def _fmt_workout_row(r, today: str) -> str:
    """单条运动记录 → 一行文本（工具返回用）。"""
    day = datetime.fromtimestamp(r["start_ts"], tz=CN_TZ)
    label = "今天 " if day.date().isoformat() == today else day.strftime("%m-%d ")
    label += day.strftime("%H:%M")
    dur = int(r["duration_s"] or 0)
    parts = [f"[{label}] {workout_type_zh(r['type'])}", f"时长{dur // 60}分{dur % 60}秒"]
    if r["distance_m"]:
        parts.append(f"距离{r['distance_m']:.0f}米")
    if r["calories"]:
        parts.append(f"消耗{r['calories']:.0f}千卡")
    if r["avg_hr"]:
        parts.append(f"心率均值{r['avg_hr']}/最高{r['max_hr'] or '–'}")
    pace = _format_pace(r["avg_pace_s"], r["pace_basis"])
    if pace:
        parts.append(f"平均配速{pace}")
    best = _format_pace(r["max_pace_s"], r["pace_basis"])
    if best:
        parts.append(f"最快{best}")
    if r["train_load"]:
        parts.append(f"训练负荷{r['train_load']:.0f}")
    if r["train_effect"]:
        parts.append(f"训练效果{r['train_effect']}")
    if r["recover_time_h"]:
        parts.append(f"建议恢复{r['recover_time_h']}小时")
    if r["stroke_count"]:
        parts.append(f"划水{r['stroke_count']:.0f}次")
    if r["turn_count"]:
        parts.append(f"转身{r['turn_count']:.0f}次")
        if r["best_swolf"]:
            parts.append(f"最佳SWOLF{r['best_swolf']:.0f}")
        # 游泳：从转身数反推泳道长度，给出单程配速（用户泳池设置为 50m/程）
        t = (r["type"] or "").lower()
        if "swim" in t and r["turn_count"] and r["distance_m"] and r["avg_pace_s"]:
            lane = float(r["distance_m"]) / float(r["turn_count"])
            if 20 <= lane <= 100:
                per_len = int(round(float(r["avg_pace_s"]) * lane / 100.0))
                parts.append(f"泳道{lane:.0f}米 · 单程配速{per_len // 60}:{per_len % 60:02d}")
    return "- " + " · ".join(parts)


def _parse_date_arg(args: dict[str, Any], key: str, default) -> Any:
    """从工具 args 里解析日期，解析失败用默认值。"""
    from datetime import date as date_cls

    raw = str(args.get(key) or "").strip()
    try:
        return date_cls.fromisoformat(raw)
    except ValueError:
        return default


def _tool_query_workouts(repo: Repository, args: dict[str, Any]) -> str:
    today = datetime.now(tz=CN_TZ).date()
    start = _parse_date_arg(args, "start", today - timedelta(days=30))
    end = _parse_date_arg(args, "end", today)
    if start > end:
        start, end = end, start
    wtype = str(args.get("type") or "").strip().lower()
    rows = repo.conn.execute(
        "SELECT * FROM workout WHERE date(start_ts, 'unixepoch', '+8 hours') BETWEEN ? AND ? "
        "AND quality_flag = 'ok' ORDER BY start_ts DESC LIMIT 60",
        (start.isoformat(), end.isoformat()),
    ).fetchall()
    if wtype:
        rows = [r for r in rows if wtype in (r["type"] or "").lower()]
    if not rows:
        scope = f"，类型过滤 {wtype}" if wtype else ""
        return f"{start} ~ {end} 没有符合条件{scope}的运动记录。"
    head = f"共 {len(rows)} 条" + ("（已达上限，请缩小日期范围）" if len(rows) == 60 else "")
    return head + "\n" + "\n".join(_fmt_workout_row(r, today.isoformat()) for r in rows)


def _tool_workout_stats(repo: Repository, args: dict[str, Any]) -> str:
    today = datetime.now(tz=CN_TZ).date()
    start = _parse_date_arg(args, "start", today - timedelta(days=90))
    end = _parse_date_arg(args, "end", today)
    if start > end:
        start, end = end, start
    rows = repo.conn.execute(
        "SELECT type, COUNT(*) AS n, SUM(duration_s) AS dur, SUM(distance_m) AS dist, "
        "SUM(COALESCE(train_load, 0)) AS load FROM workout "
        "WHERE date(start_ts, 'unixepoch', '+8 hours') BETWEEN ? AND ? "
        "AND quality_flag = 'ok' GROUP BY type ORDER BY n DESC",
        (start.isoformat(), end.isoformat()),
    ).fetchall()
    if not rows:
        return f"{start} ~ {end} 没有任何运动记录。"
    lines = [
        f"{workout_type_zh(r['type'])}（{r['type']}）：{r['n']} 次 · 共{_fmt_duration_min(r['dur'] or 0)} · "
        f"总距离{(r['dist'] or 0) / 1000:.1f} 公里 · 总训练负荷{(r['load'] or 0):.0f}"
        for r in rows
    ]
    return f"{start} ~ {end} 运动汇总：\n" + "\n".join(lines)


def _tool_query_summary(repo: Repository, args: dict[str, Any]) -> str:
    today = datetime.now(tz=CN_TZ).date()
    day = _parse_date_arg(args, "date", today)
    cached = repo.get_ai_summary(day.isoformat())
    if not cached:
        return f"{day} 没有生成过 AI 解读。"
    return json.dumps({"date": day.isoformat(), **cached["summary"]}, ensure_ascii=False)


def _tool_query_series(repo: Repository, args: dict[str, Any]) -> str:
    metric = str(args.get("metric") or "").strip()
    if metric not in SERIES_QUERIES:
        return f"未知指标 {metric}。可用指标：{', '.join(SERIES_QUERIES)}"
    today = datetime.now(tz=CN_TZ).date()
    start = _parse_date_arg(args, "start", today - timedelta(days=30))
    end = _parse_date_arg(args, "end", today)
    if start > end:
        start, end = end, start
    sql, unit = SERIES_QUERIES[metric]
    rows = repo.conn.execute(sql, (start.isoformat(), end.isoformat())).fetchall()
    if not rows:
        return f"{metric} 在 {start} ~ {end} 没有数据。"
    vals = [float(r["y"]) for r in rows if r["y"] is not None]
    summary = (
        f"共 {len(rows)} 天；均值 {sum(vals) / len(vals):.1f} {unit}，"
        f"最低 {min(vals):.0f}，最高 {max(vals):.0f}"
        if vals else f"共 {len(rows)} 天"
    )
    points = " ".join(f"{r['x']}={r['y']}" for r in rows)
    return f"{metric}（{unit}）{start} ~ {end}：{summary}\n{points}"


def _tool_query_day(repo: Repository, args: dict[str, Any]) -> str:
    today = datetime.now(tz=CN_TZ).date()
    day = _parse_date_arg(args, "date", today)
    payload = build_daily_payload(repo, day.isoformat())
    text = json.dumps(payload, ensure_ascii=False)
    if len(text) > 6000:
        text = text[:6000] + "…（过长已截断）"
    return f"{day} 的健康档案：\n{text}"


def _tool_query_anomalies(repo: Repository, args: dict[str, Any]) -> str:
    try:
        limit = max(1, min(50, int(args.get("limit") or 20)))
    except (TypeError, ValueError):
        limit = 20
    rows = repo.conn.execute(
        "SELECT * FROM anomaly_event ORDER BY date DESC, ts DESC LIMIT ?", (limit,)
    ).fetchall()
    if not rows:
        return "没有异常事件记录。"
    lines = [
        f"- [{r['date']}] {r['kind']}（{r['severity']}）：{r['message'] or ''}"
        for r in rows
    ]
    return "最近异常事件：\n" + "\n".join(lines)


def _fetch_bing(query: str, count: int = 8) -> list[dict[str, str]]:
    """必应（国内可达）网页搜索，解析自然结果，免密钥。"""
    import html as html_mod
    import re as re_mod
    import urllib.parse

    url = "https://www.bing.com/search?" + urllib.parse.urlencode(
        {"q": query, "count": str(count), "setlang": "zh-CN", "mkt": "zh-CN"}
    )
    page = read_public_page(url)
    items: list[dict[str, str]] = []
    for m in re_mod.finditer(r'<li class="b_algo".*?</li>', page, re_mod.S):
        block = m.group(0)
        am = re_mod.search(r'<h2[^>]*><a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', block, re_mod.S)
        if not am:
            continue
        title = re_mod.sub(r"<[^>]+>", "", am.group(2))
        sm = re_mod.search(r'<p[^>]*>(.*?)</p>', block, re_mod.S)
        snippet = re_mod.sub(r"<[^>]+>", "", sm.group(1)) if sm else ""
        items.append({
            "title": html_mod.unescape(title).strip(),
            "url": am.group(1),
            "snippet": html_mod.unescape(snippet).strip()[:300],
        })
        if len(items) >= count:
            break
    return items


def _tool_web_search(args: dict[str, Any]) -> str:
    query = str(args.get("query") or "").strip()
    if not query:
        return "缺少 query 参数。"
    try:
        items = _fetch_bing(query)
    except Exception as exc:  # noqa: BLE001
        return f"搜索失败：{type(exc).__name__}: {str(exc)[:120]}。可稍后重试或换个关键词。"
    if not items:
        return f"搜索「{query}」没有返回结果，建议换个更具体的关键词。"
    lines = [f"搜索「{query}」共 {len(items)} 条结果："]
    for i, it in enumerate(items, 1):
        lines.append(f"{i}. {it['title']}\n   {it['url']}\n   {it['snippet']}")
    return "\n".join(lines)


def _tool_read_page(args: dict[str, Any]) -> str:
    import html as html_mod
    from html.parser import HTMLParser

    url = str(args.get("url") or "").strip()

    class _Text(HTMLParser):
        def __init__(self) -> None:
            super().__init__()
            self.parts: list[str] = []
            self.skip = 0

        def handle_starttag(self, tag, attrs):  # noqa: ARG002
            if tag in ("script", "style", "noscript"):
                self.skip += 1

        def handle_endtag(self, tag):
            if tag in ("script", "style", "noscript") and self.skip:
                self.skip -= 1

        def handle_data(self, data):
            if not self.skip:
                self.parts.append(data)

    try:
        page = read_public_page(url)
    except PublicWebError as exc:
        return f"读取失败：{exc}"
    parser = _Text()
    try:
        parser.feed(page)
    except Exception:  # noqa: BLE001 - 解析失败就按纯文本处理
        parser.parts = [page]
    text = html_mod.unescape(" ".join(parser.parts))
    text = " ".join(text.split())
    if len(text) > 4000:
        text = text[:4000] + "…（过长已截断）"
    return f"{url} 正文：\n{text}" if text else f"{url} 没有可提取的正文。"


def _tool_trigger_sync(cfg: Config) -> str:
    if _sync_state["running"] or not _sync_lock.acquire(blocking=False):
        return "已有同步任务在运行。可用 check_sync_status 查看进度。"
    _sync_state.update(
        running=True, started_at=time.time(), finished_at=None,
        ok=None, rows=0, note="",
    )
    threading.Thread(target=_run_manual_sync, args=(cfg,), name="manual-sync", daemon=True).start()
    return (
        "强制增量同步已在后台启动（重拉最近时间窗的全部数据源），通常 1-3 分钟完成。"
        "完成后才能查到新数据；用户下次问数据时可用 check_sync_status 确认状态。"
    )


def _tool_check_sync_status() -> str:
    st = dict(_sync_state)
    if st["running"]:
        waited = int(time.time() - st["started_at"]) if st["started_at"] else 0
        return f"同步进行中，已运行 {waited} 秒。请稍后再查或提醒用户稍等。"
    if st["finished_at"] is None:
        return "当前没有同步任务记录（本次会话尚未触发过同步）。"
    state = "成功" if st["ok"] else "失败"
    return f"上次同步已结束：{state}，写入 {st['rows']} 行。详情：{st['note']}"


def _build_tool_executor(repo: Repository, cfg: Config):
    """构造对话工具执行器（绑定当前请求的 repo，连接同线程）。"""

    def execute(action: dict[str, Any]) -> str:
        tool = str(action.get("tool") or "")
        args = action.get("args") if isinstance(action.get("args"), dict) else {}
        try:
            if tool == "query_workouts":
                return _tool_query_workouts(repo, args)
            if tool == "workout_stats":
                return _tool_workout_stats(repo, args)
            if tool == "query_ai_summary":
                return _tool_query_summary(repo, args)
            if tool == "query_series":
                return _tool_query_series(repo, args)
            if tool == "query_day":
                return _tool_query_day(repo, args)
            if tool == "query_anomalies":
                return _tool_query_anomalies(repo, args)
            if tool == "trigger_sync":
                return _tool_trigger_sync(cfg)
            if tool == "check_sync_status":
                return _tool_check_sync_status()
            if tool == "web_search":
                return _tool_web_search(args)
            if tool == "read_page":
                return _tool_read_page(args)
            return (
                f"未知工具 {tool}。可用工具：query_workouts / workout_stats / "
                "query_ai_summary / query_series / query_day / query_anomalies / "
                "trigger_sync / check_sync_status / web_search / read_page"
            )
        except Exception as exc:  # noqa: BLE001 - 工具失败要回给模型而不是炸掉请求
            return f"工具执行失败：{type(exc).__name__}: {exc}"

    return execute

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

SERIES_QUERIES: dict[str, tuple[str, str]] = {
    "steps": (
        "SELECT date AS x, steps AS y FROM daily_activity WHERE date BETWEEN ? AND ? "
        "AND quality_flag = 'ok' ORDER BY date",
        "steps",
    ),
    "resting_hr": (
        "SELECT date AS x, bpm AS y FROM resting_hr WHERE date BETWEEN ? AND ? "
        "AND quality_flag = 'ok' ORDER BY date",
        "bpm",
    ),
    "calories": (
        "SELECT date AS x, calories AS y FROM daily_activity WHERE date BETWEEN ? AND ? "
        "AND quality_flag = 'ok' ORDER BY date",
        "千卡",
    ),
    "sleep_total": (
        "SELECT date AS x, total_min AS y FROM sleep_session WHERE date BETWEEN ? AND ? "
        "AND quality_flag = 'ok' AND is_nap = 0 ORDER BY date",
        "min",
    ),
    "sleep_deep": (
        "SELECT date AS x, deep_min AS y FROM sleep_session WHERE date BETWEEN ? AND ? "
        "AND quality_flag = 'ok' AND is_nap = 0 AND has_stage = 1 ORDER BY date",
        "min",
    ),
    "sleep_rem": (
        "SELECT date AS x, rem_min AS y FROM sleep_session WHERE date BETWEEN ? AND ? "
        "AND quality_flag = 'ok' AND is_nap = 0 AND has_stage = 1 AND has_rem = 1 ORDER BY date",
        "min",
    ),
    "nap": (
        "SELECT date AS x, nap_min AS y FROM sleep_session WHERE date BETWEEN ? AND ? "
        "AND quality_flag = 'ok' ORDER BY date",
        "min",
    ),
    # 由心率采样推算（云端 active_min 为空）：按分钟取最大 bpm
    "active_min": (
        "WITH per_min AS (SELECT CAST(ts / 60 AS INTEGER) AS m, MAX(bpm) AS bpm "
        "FROM heart_rate_sample WHERE date(ts, 'unixepoch', '+8 hours') BETWEEN ? AND ? "
        "AND quality_flag = 'ok' AND bpm IS NOT NULL GROUP BY m) "
        "SELECT date(m * 60, 'unixepoch', '+8 hours') AS x, SUM(bpm >= 100) AS y "
        "FROM per_min GROUP BY x ORDER BY x",
        "min",
    ),
    "vigorous_min": (
        "WITH per_min AS (SELECT CAST(ts / 60 AS INTEGER) AS m, MAX(bpm) AS bpm "
        "FROM heart_rate_sample WHERE date(ts, 'unixepoch', '+8 hours') BETWEEN ? AND ? "
        "AND quality_flag = 'ok' AND bpm IS NOT NULL GROUP BY m) "
        "SELECT date(m * 60, 'unixepoch', '+8 hours') AS x, SUM(bpm >= 140) AS y "
        "FROM per_min GROUP BY x ORDER BY x",
        "min",
    ),
    "hr_avg": (
        "WITH per_min AS (SELECT CAST(ts / 60 AS INTEGER) AS m, MAX(bpm) AS bpm "
        "FROM heart_rate_sample WHERE date(ts, 'unixepoch', '+8 hours') BETWEEN ? AND ? "
        "AND quality_flag = 'ok' AND bpm IS NOT NULL GROUP BY m) "
        "SELECT date(m * 60, 'unixepoch', '+8 hours') AS x, ROUND(AVG(bpm), 1) AS y "
        "FROM per_min GROUP BY x ORDER BY x",
        "bpm",
    ),
    "sleep_efficiency": (
        "SELECT date AS x, efficiency AS y FROM sleep_session WHERE date BETWEEN ? AND ? "
        "AND quality_flag = 'ok' AND is_nap = 0 ORDER BY date",
        "ratio",
    ),
    "spo2_min": (
        "SELECT date(ts, 'unixepoch', '+8 hours') AS x, MIN(spo2) AS y FROM spo2_sample "
        "WHERE date(ts, 'unixepoch', '+8 hours') BETWEEN ? AND ? AND quality_flag = 'ok' "
        "GROUP BY x ORDER BY x",
        "percent",
    ),
    "stress_avg": (
        "SELECT date(ts, 'unixepoch', '+8 hours') AS x, ROUND(AVG(stress), 1) AS y "
        "FROM stress_sample WHERE date(ts, 'unixepoch', '+8 hours') BETWEEN ? AND ? "
        "AND quality_flag = 'ok' GROUP BY x ORDER BY x",
        "score",
    ),
}


def _is_trusted(host: str, trust_private: bool) -> bool:
    """判断来源是否可信。

    ⚠️ 经过 docker 端口映射后，uvicorn 看到的客户端 IP **不是** 127.0.0.1，
    而是网桥网关（如 172.22.0.1）。所以只认回环会导致本机访问也被拒。
    这里把「回环 + 私有网段」视为可信，真正的访问控制在**宿主机端口绑定**
    与 SSH 隧道 / 反向代理上。对局域网开放且需要鉴权时，把 trust_private 设为 false。
    """
    if host in {"", "unknown", "testclient", "localhost"}:
        return True
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False

    if addr.is_loopback:
        return True
    if trust_private and (addr.is_private or addr.is_link_local):
        return True
    return False


def _client_host(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def create_app(config: Config, ai_provider: AIConfigProvider | None = None) -> FastAPI:
    app = FastAPI(
        title="mi-health-hub",
        version=__version__,
        description="小米手表健康数据自托管分析平台（只读 API）",
    )

    config.paths.ensure()
    if ai_provider is None:
        settings_path = os.environ.get("MI_AI_SETTINGS_FILE") or str(
            config.paths.db.parent / "ai-settings.json"
        )
        ai_provider = AIConfigProvider(config.ai, AISettingsStore(settings_path))
    # provider 会先合并持久化设置；把完整快照原子挂回 Config，调度器和同步引擎
    # 后续创建任务时即可读取到设置页的最新值。
    config.ai = ai_provider.snapshot()
    app.state.ai_provider = ai_provider

    credential_store = CredentialStore()

    def accept_xiaomi_credentials(data: dict[str, str]) -> None:
        """网页登录完成后的原子验证与提交边界。

        先用 sid=miothealth 真实登录；只有拿到 ssecurity/cookie 后才写文件和热更新 Config，
        避免把小爱音箱可用、健康接口不可用的 token 覆盖进健康服务。
        """
        candidate = Credentials(
            user_id=str(data.get("user_id") or ""),
            pass_token=str(data.get("pass_token") or ""),
            region=config.region,
            device_id=str(data.get("device_id") or ""),
            c_user_id=str(data.get("c_user_id") or ""),
        )
        client = MiHealthClient(
            user_id=candidate.user_id,
            pass_token=candidate.pass_token,
            region=candidate.region,
            interval_ms=config.sync.request_interval_ms,
            timeout_s=config.sync.http_timeout_s,
            retries=config.sync.request_retries,
            max_pages=config.sync.max_pages,
            budget=RequestBudget(limit=20),
        )
        try:
            client.login()
            # login 可能再次轮换 passToken，持久化最终通过 miothealth 验证的那个。
            candidate.user_id = client.user_id
            candidate.pass_token = client.pass_token
        finally:
            client.close()
        credential_store.save(candidate)
        config.user_id = candidate.user_id
        config.pass_token = candidate.pass_token
        config.region = candidate.region

    xiaomi_login = XiaomiHealthWebLogin(accept_xiaomi_credentials)
    app.state.xiaomi_login = xiaomi_login

    access_logs = AccessLogStore(
        config.paths.access_logs,
        retention_days=config.server.access_log_retention_days,
    )
    app.state.access_logs = access_logs

    if config.server.access_log_enabled:
        @app.middleware("http")
        async def write_access_log(request: Request, call_next):  # noqa: ANN001
            started = time.perf_counter()
            status = 500
            response_bytes = 0
            try:
                response = await call_next(request)
                status = response.status_code
                try:
                    response_bytes = int(response.headers.get("content-length") or 0)
                except (TypeError, ValueError):
                    response_bytes = 0
                return response
            finally:
                duration_ms = (time.perf_counter() - started) * 1000
                # 容器内 healthcheck 每分钟访问一次 /healthz，不属于真实访客，跳过噪音；
                # 公网/局域网访问 /healthz 仍会记录。
                peer_host = _client_host(request)
                internal_healthcheck = (
                    request.url.path == "/healthz"
                    and not request.headers.get("cf-ray")
                    and peer_host in {"127.0.0.1", "::1"}
                )
                # 本地日志页成功查询不反复污染自身日志；公网探测该接口产生的 403 仍记录。
                internal_log_query = (
                    request.url.path.startswith("/api/access-logs") and status < 400
                )
                if not internal_healthcheck and not internal_log_query:
                    try:
                        access_logs.append(
                            build_access_entry(
                                request=request,
                                status=status,
                                duration_ms=duration_ms,
                                response_bytes=response_bytes,
                            )
                        )
                    except Exception as exc:  # noqa: BLE001 - 访问日志不能影响业务请求
                        logger.warning("访问日志写入失败：%s", type(exc).__name__)

    # 建库只在启动时做一次：每次请求都跑一遍 DDL 是浪费
    bootstrap = connect(config.paths.db)
    try:
        init_db(bootstrap)
    finally:
        bootstrap.close()

    @contextmanager
    def open_repo() -> Iterator[Repository]:
        """在**当前线程**开一条连接，用完关闭。

        必须由端点函数体调用（不要做成 FastAPI 依赖），原因见模块 docstring。
        """
        conn = connect(config.paths.db)
        try:
            yield Repository(conn)
        finally:
            conn.close()

    def require_auth(request: Request, x_api_key: str | None = Header(default=None)) -> None:
        """可信来源免鉴权；其余必须带正确 API Key。"""
        host = _client_host(request)
        if _is_trusted(host, config.server.trust_private):
            return
        if not config.server.api_key:
            raise HTTPException(
                status_code=403,
                detail="服务未配置 MI_API_KEY，默认拒绝非可信来源的访问",
            )
        if x_api_key != config.server.api_key:
            raise HTTPException(status_code=401, detail="API Key 无效")

    def require_local_admin(request: Request) -> None:
        """敏感管理数据仅限 NAS 局域网地址直连，不接受公网反代头。"""
        forwarded = any(
            request.headers.get(name)
            for name in ("cf-connecting-ip", "x-forwarded-for", "forwarded", "cf-ray")
        )
        request_host = (request.headers.get("host") or "").lower().split(":", 1)[0]
        local_host = request_host in {"192.0.2.10", "127.0.0.1", "localhost", "testserver"}
        if not forwarded and local_host and _is_trusted(_client_host(request), True):
            return
        raise HTTPException(
            status_code=403,
            detail="访问日志仅允许通过 NAS 局域网地址查看：http://192.0.2.10:8321/#/access",
        )

    def require_settings_admin(
        request: Request, x_api_key: str | None = Header(default=None)
    ) -> None:
        """AI 设置含密钥且能改变数据外发目标，只允许局域网直连或显式管理密钥。

        普通健康接口沿用现有 trust_private 行为；设置接口额外拒绝经过公网反代的
        匿名请求，防止他人把 Base URL 改到恶意服务后触发健康数据外泄。
        """
        if config.server.api_key and x_api_key == config.server.api_key:
            return
        forwarded = any(
            request.headers.get(name)
            for name in ("cf-connecting-ip", "x-forwarded-for", "forwarded")
        )
        if not forwarded and _is_trusted(_client_host(request), True):
            return
        raise HTTPException(
            status_code=403,
            detail="AI 设置仅允许局域网直连修改；请用 NAS 内网地址打开设置页",
        )

    def current_ai() -> AIConfig:
        return ai_provider.snapshot()

    def build_ai_candidate(body: AISettingsRequest, current: AIConfig) -> AIConfig:
        from dataclasses import replace

        if body.clear_api_key:
            key = ""
        elif body.api_key not in (None, ""):
            key = str(body.api_key).strip()
        else:
            key = current.api_key
        return replace(
            current,
            enabled=body.enabled,
            protocol=body.protocol,
            base_url=body.base_url,
            api_key=key,
            model=body.model,
            timeout_s=body.timeout_s,
            max_tokens=body.max_tokens,
            temperature=body.temperature,
            auto_after_sync=body.auto_after_sync,
            auto_period_reports=body.auto_period_reports,
            json_mode=body.json_mode,
            allow_private_base_url=body.allow_private_base_url,
            anthropic_version=body.anthropic_version,
            detail_level=body.detail_level,
            include_health_notes=body.include_health_notes,
            web_tools_enabled=body.web_tools_enabled,
            custom_instructions=body.custom_instructions,
        )

    def public_ai_settings(ai: AIConfig) -> dict[str, Any]:
        data = ai_provider.store.public_view(ai, source=ai_provider.source())
        data["locked_fields"] = ai_provider.locked_fields
        data["privacy_notice"] = "启用后，健康摘要、健康备注及对话内容会发送到所配置的 AI 服务。"
        return data

    # ------------------------------------------------------------ 小米健康网页登录
    @app.get("/api/auth/xiaomi/status", tags=["认证"], dependencies=[Depends(require_auth)])
    def xiaomi_login_status() -> dict[str, object]:
        """公开网页登录状态；不含 userId、Cookie 或 token。"""
        return xiaomi_login.public_status()

    @app.post(
        "/api/auth/xiaomi/start",
        tags=["认证"],
        dependencies=[Depends(require_settings_admin)],
    )
    def xiaomi_login_start() -> dict[str, object]:
        """仅局域网/管理 Key 可发起 miothealth 网页登录。"""
        try:
            return xiaomi_login.start()
        except Exception as exc:  # noqa: BLE001 - 外部认证边界
            logger.warning("无法启动小米健康网页登录：%s", type(exc).__name__)
            raise HTTPException(status_code=502, detail="无法获取小米登录地址，请稍后重试") from exc

    @app.post(
        "/api/auth/xiaomi/cancel",
        tags=["认证"],
        dependencies=[Depends(require_settings_admin)],
    )
    def xiaomi_login_cancel() -> dict[str, object]:
        return xiaomi_login.cancel()

    # ------------------------------------------------------------ 访问日志（仅局域网直连查看）
    @app.get(
        "/api/access-logs",
        tags=["访问日志"],
        dependencies=[Depends(require_local_admin)],
    )
    def access_log_list(
        hours: int = Query(default=24, ge=1, le=720),
        host: str = Query(default="", max_length=253),
        ip: str = Query(default="", max_length=64),
        path: str = Query(default="", max_length=300),
        status: int | None = Query(default=None, ge=100, le=599),
        limit: int = Query(default=200, ge=1, le=1000),
    ) -> dict[str, Any]:
        return access_logs.query(
            hours=hours,
            host=host,
            ip=ip,
            path=path,
            status=status,
            limit=limit,
        )

    @app.get(
        "/api/access-logs/summary",
        tags=["访问日志"],
        dependencies=[Depends(require_local_admin)],
    )
    def access_log_summary(
        hours: int = Query(default=24, ge=1, le=720),
        host: str = Query(default="", max_length=253),
    ) -> dict[str, Any]:
        return access_logs.summary(hours=hours, host=host)

    # ------------------------------------------------------------ AI 设置
    @app.get("/api/ai/settings", tags=["AI 设置"], dependencies=[Depends(require_auth)])
    def ai_settings_get() -> dict[str, Any]:
        """读取可公开的 AI 设置；API Key 永远不回传。"""
        return public_ai_settings(current_ai())

    @app.put("/api/ai/settings", tags=["AI 设置"], dependencies=[Depends(require_settings_admin)])
    def ai_settings_save(body: AISettingsRequest) -> dict[str, Any]:
        """校验并原子保存 AI 设置。API Key 留空表示保留现值。"""
        try:
            saved = ai_provider.save(build_ai_candidate(body, current_ai()))
        except AISettingsError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        config.ai = saved
        return public_ai_settings(saved)

    @app.delete("/api/ai/settings", tags=["AI 设置"], dependencies=[Depends(require_settings_admin)])
    def ai_settings_reset() -> dict[str, Any]:
        """删除页面设置，恢复启动时的 YAML/环境变量默认配置。"""
        try:
            restored = ai_provider.reset()
        except (AISettingsError, OSError) as exc:
            raise HTTPException(status_code=500, detail="恢复默认设置失败") from exc
        config.ai = restored
        return public_ai_settings(restored)

    @app.post("/api/ai/settings/test", tags=["AI 设置"], dependencies=[Depends(require_settings_admin)])
    def ai_settings_test(body: AISettingsRequest) -> dict[str, Any]:
        """不保存设置，以最小请求验证地址、鉴权、协议与模型。"""
        from dataclasses import replace

        try:
            candidate = ai_provider.preview(build_ai_candidate(body, current_ai()))
            return AIClient(replace(candidate, enabled=True)).test_connection()
        except AISettingsError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except AIError as exc:
            raise HTTPException(status_code=502, detail=f"连接测试失败：{exc}") from exc

    @app.post("/api/ai/settings/models", tags=["AI 设置"], dependencies=[Depends(require_settings_admin)])
    def ai_settings_models(body: AISettingsRequest) -> dict[str, Any]:
        """尝试读取上游模型列表；不支持 /models 时仍可在页面手工填写。"""
        from dataclasses import replace

        try:
            candidate = ai_provider.preview(build_ai_candidate(body, current_ai()))
            return {"models": AIClient(replace(candidate, enabled=True)).list_models()}
        except AISettingsError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except AIError as exc:
            raise HTTPException(status_code=502, detail=f"获取模型列表失败：{exc}") from exc

    # ------------------------------------------------------------ 基础
    @app.on_event("shutdown")
    def shutdown_xiaomi_login() -> None:
        xiaomi_login.close()

    @app.get("/healthz", tags=["基础"])
    def healthz() -> dict[str, Any]:
        return {"ok": True, "version": __version__}

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        page = WEB_DIR / "index.html"
        if not page.is_file():
            raise HTTPException(status_code=500, detail="web 资源缺失")
        # 单文件前端更新频繁，禁止 Safari/代理复用旧版路由与绘图逻辑。
        return FileResponse(
            page,
            headers={
                "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                "Pragma": "no-cache",
                "Expires": "0",
            },
        )

    @app.get("/vendor/{asset:path}", include_in_schema=False)
    def vendor_asset(asset: str) -> FileResponse:
        """本地打包的第三方资源（Chart.js 等），带目录穿越防护。"""
        root = (WEB_DIR / "vendor").resolve()
        target = (root / asset).resolve()
        if root not in target.parents or not target.is_file():
            raise HTTPException(status_code=404, detail="资源不存在")
        return FileResponse(target)

    @app.get("/api/admin/status", tags=["基础"], dependencies=[Depends(require_auth)])
    def status() -> dict[str, Any]:
        with open_repo() as repo:
            return {
                "db": str(config.paths.db),
                "region": config.region,
                "credentials_configured": bool(config.user_id and config.pass_token),
                # 前端据此决定要不要显示 API Key 输入框
                "auth_required": not config.server.trust_private,
                "trust_private": config.server.trust_private,
                "integrity": repo.integrity_check(),
                "table_counts": repo.table_counts(),
                "sync_state": repo.all_sync_states(),
                "recent_runs": repo.recent_sync_runs(limit=10),
            }

    # ------------------------------------------------------------ 手动同步
    @app.post("/api/admin/sync", tags=["管理"], status_code=202, dependencies=[Depends(require_auth)])
    def trigger_sync() -> dict[str, Any]:
        """手动触发一次强制 T1 增量同步（后台线程执行，立即返回）。

        手动操作会重拉最近时间窗的全部数据源，不受 steps 哨兵限制。与调度任务共用
        同一把互斥锁：已有同步在跑（手动或定时）时返回 409。
        """
        if _sync_state["running"] or not _sync_lock.acquire(blocking=False):
            raise HTTPException(status_code=409, detail="已有同步任务在运行，请稍后再试")
        _sync_state.update(
            running=True, started_at=time.time(), finished_at=None,
            ok=None, rows=0, note="",
        )
        threading.Thread(target=_run_manual_sync, args=(config,), name="manual-sync", daemon=True).start()
        return {"started": True}

    @app.get("/api/admin/sync/status", tags=["管理"], dependencies=[Depends(require_auth)])
    def sync_status() -> dict[str, Any]:
        """手动同步的进行状态（前端轮询用）。"""
        return dict(_sync_state)

    # ------------------------------------------------------------ 概览
    @app.get("/api/summary", tags=["数据"], dependencies=[Depends(require_auth)])
    def summary(date: str | None = Query(default=None, description="YYYY-MM-DD，默认今天")) -> dict[str, Any]:
        day = date or datetime.now(tz=CN_TZ).date().isoformat()
        with open_repo() as repo:
            activity = repo.conn.execute(
                "SELECT * FROM daily_activity WHERE date = ? AND quality_flag = 'ok'", (day,)
            ).fetchone()
            sleep = repo.conn.execute(
                "SELECT * FROM sleep_session WHERE date = ? AND quality_flag = 'ok'", (day,)
            ).fetchone()
            rhr = repo.conn.execute(
                "SELECT * FROM resting_hr WHERE date = ? AND quality_flag = 'ok'", (day,)
            ).fetchone()
            workouts = repo.conn.execute(
                "SELECT COUNT(*) AS n, SUM(duration_s) AS secs FROM workout "
                "WHERE date(start_ts, 'unixepoch', '+8 hours') = ? AND quality_flag = 'ok'",
                (day,),
            ).fetchone()
            # 睡眠分期分段：每晚几十段，带真实起止时间，供前端画睡眠时间轴。
            # 只取有实际时长、起止有序的段，避免脏数据把时间轴撑破。
            stages = repo.conn.execute(
                "SELECT stage, start_ts, end_ts, minutes FROM sleep_stage "
                "WHERE date = ? AND end_ts > start_ts ORDER BY start_ts",
                (day,),
            ).fetchall()

            return {
                "date": day,
                "activity": dict(activity) if activity else None,
                "sleep": dict(sleep) if sleep else None,
                "sleep_stages": [dict(row) for row in stages],
                "resting_hr": dict(rhr) if rhr else None,
                "workout": (
                    {"count": workouts["n"], "duration_s": workouts["secs"]}
                    if workouts and workouts["n"]
                    else None
                ),
            }

    # ------------------------------------------------------------ 时间序列
    @app.get("/api/series/{metric}", tags=["数据"], dependencies=[Depends(require_auth)])
    def series(
        metric: str,
        start: str = Query(description="YYYY-MM-DD"),
        end: str = Query(description="YYYY-MM-DD"),
    ) -> dict[str, Any]:
        try:
            start_day = datetime.fromisoformat(start).date()
            end_day = datetime.fromisoformat(end).date()
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="start/end 必须是 YYYY-MM-DD") from exc
        if start_day > end_day:
            raise HTTPException(status_code=422, detail="start 不能晚于 end")
        if (end_day - start_day).days > 3660:
            raise HTTPException(status_code=422, detail="单次查询跨度不能超过 10 年")
        if metric not in SERIES_QUERIES:
            raise HTTPException(
                status_code=404,
                detail=f"未知指标 {metric}，可用：{', '.join(SERIES_QUERIES)}",
            )
        sql, unit = SERIES_QUERIES[metric]
        with open_repo() as repo:
            rows = repo.conn.execute(sql, (start, end)).fetchall()
            return {
                "metric": metric,
                "unit": unit,
                "start": start,
                "end": end,
                "points": [dict(row) for row in rows],
            }

    # ------------------------------------------------------------ 周期统计
    @app.get("/api/stats/period", tags=["数据"], dependencies=[Depends(require_auth)])
    def stats_period(
        kind: str = Query(default="daily", description="daily / weekly / monthly"),
        date_: str | None = Query(default=None, alias="date", description="YYYY-MM-DD，默认今天"),
    ) -> dict[str, Any]:
        """日/周/月聚合：卡路里、步数、活动时间、高强度时间、睡眠 + 环比 + 逐日序列。

        活动时间与高强度时间由心率采样推算（云端 active_min 为空）：
        按分钟取最大 bpm，≥100 记活动分钟，≥140 记高强度分钟。
        """
        if kind not in ("daily", "weekly", "monthly"):
            raise HTTPException(status_code=400, detail="kind 必须是 daily/weekly/monthly")
        if date_:
            try:
                ref = datetime.fromisoformat(date_).date()
            except ValueError as exc:
                raise HTTPException(status_code=422, detail="date 必须是 YYYY-MM-DD") from exc
        else:
            ref = datetime.now(tz=CN_TZ).date()
        with open_repo() as repo:
            return build_period_stats(repo, kind, ref)

    # ------------------------------------------------------------ 运动
    @app.get("/api/workouts", tags=["数据"], dependencies=[Depends(require_auth)])
    def workouts(
        limit: int = Query(default=50, le=500),
        offset: int = Query(default=0, ge=0),
        workout_type: str | None = Query(default=None, alias="type", description="按运动类型过滤"),
        start: str | None = Query(default=None, description="起始日期 YYYY-MM-DD"),
        end: str | None = Query(default=None, description="结束日期 YYYY-MM-DD"),
    ) -> dict[str, Any]:
        """运动记录目录（新的在前，支持分页/类型/日期过滤）。"""
        where: list[str] = []
        params: list[Any] = []
        if workout_type:
            where.append("type = ?")
            params.append(workout_type)
        if start:
            where.append("date(start_ts, 'unixepoch', '+8 hours') >= ?")
            params.append(start)
        if end:
            where.append("date(start_ts, 'unixepoch', '+8 hours') <= ?")
            params.append(end)
        clause = ("WHERE " + " AND ".join(where)) if where else ""
        with open_repo() as repo:
            total = repo.conn.execute(
                f"SELECT COUNT(*) AS n FROM workout {clause}", tuple(params)
            ).fetchone()["n"]
            rows = repo.conn.execute(
                "SELECT id, type, start_ts, end_ts, duration_s, distance_m, calories, total_cal, "
                "avg_hr, max_hr, min_hr, avg_pace_s, max_pace_s, pace_basis, train_effect, "
                "train_load, recover_time_h, stroke_count, turn_count, avg_swolf, best_swolf, "
                "steps, vitality FROM workout "
                f"{clause} ORDER BY start_ts DESC LIMIT ? OFFSET ?",
                tuple(params + [limit, offset]),
            ).fetchall()
            items = []
            for row in rows:
                item = dict(row)
                item["type_cn"] = workout_type_zh(item["type"])
                # 配速/单程配速的展示文本（前端不用自己算）
                item["avg_pace_text"] = _format_pace(item["avg_pace_s"], item["pace_basis"])
                item["max_pace_text"] = _format_pace(item["max_pace_s"], item["pace_basis"])
                if "swim" in (item["type"] or "").lower() and item["turn_count"] and item["distance_m"]:
                    lane = float(item["distance_m"]) / float(item["turn_count"])
                    if 20 <= lane <= 100:
                        item["lane_m"] = round(lane)
                        per = int(round(float(item["avg_pace_s"] or 0) * lane / 100.0))
                        item["per_lane_pace_text"] = f"{per // 60}:{per % 60:02d}" if per else None
                items.append(item)
            # 类型清单（含中文名），供筛选器用
            type_rows = repo.conn.execute(
                "SELECT type, COUNT(*) AS count FROM workout GROUP BY type ORDER BY count DESC"
            ).fetchall()
            types = [{"type": r["type"], "type_cn": workout_type_zh(r["type"]), "count": r["count"]}
                     for r in type_rows]
            return {"items": items, "total": total, "limit": limit, "offset": offset, "types": types}

    @app.get("/api/workouts/{workout_id}", tags=["数据"], dependencies=[Depends(require_auth)])
    def workout_detail(workout_id: str) -> dict[str, Any]:
        """单次运动详情：库里所有字段原样返回（含心率区间、游泳专项、原始 JSON）。"""
        with open_repo() as repo:
            row = repo.conn.execute(
                "SELECT * FROM workout WHERE id = ?", (workout_id,)
            ).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="没有这条运动记录")
            item = dict(row)
            item["type_cn"] = workout_type_zh(item["type"])
            item["avg_pace_text"] = _format_pace(item["avg_pace_s"], item["pace_basis"])
            item["max_pace_text"] = _format_pace(item["max_pace_s"], item["pace_basis"])
            if "swim" in (item["type"] or "").lower() and item["turn_count"] and item["distance_m"]:
                lane = float(item["distance_m"]) / float(item["turn_count"])
                if 20 <= lane <= 100:
                    item["lane_m"] = round(lane)
                    per = int(round(float(item["avg_pace_s"] or 0) * lane / 100.0))
                    item["per_lane_pace_text"] = f"{per // 60}:{per % 60:02d}" if per else None
            return item

    @app.get("/api/workout_types", tags=["数据"], dependencies=[Depends(require_auth)])
    def workout_types() -> dict[str, Any]:
        with open_repo() as repo:
            rows = repo.conn.execute(
                "SELECT type, COUNT(*) AS count, SUM(duration_s) AS total_s, "
                "SUM(distance_m) AS total_m FROM workout GROUP BY type ORDER BY count DESC"
            ).fetchall()
            return {"items": [dict(row) for row in rows]}

    # ------------------------------------------------------------ 覆盖度与异常
    @app.get("/api/coverage", tags=["数据"], dependencies=[Depends(require_auth)])
    def coverage(start: str = Query(...), end: str = Query(...)) -> dict[str, Any]:
        with open_repo() as repo:
            return {"items": repo.coverage_range(start, end)}

    @app.get("/api/anomalies", tags=["数据"], dependencies=[Depends(require_auth)])
    def anomalies(limit: int = Query(default=100, le=1000)) -> dict[str, Any]:
        with open_repo() as repo:
            rows = repo.conn.execute(
                "SELECT * FROM anomaly_event ORDER BY date DESC, ts DESC LIMIT ?", (limit,)
            ).fetchall()
            return {"items": [dict(row) for row in rows]}

    # ------------------------------------------------------------ AI 分析用
    @app.get("/api/ai/daily", tags=["AI"], dependencies=[Depends(require_auth)])
    def ai_daily(
        date: str | None = Query(default=None, description="YYYY-MM-DD，默认今天"),
        baseline_days: int = Query(default=30, ge=7, le=180),
        trend_days: int = Query(default=7, ge=3, le=30),
    ) -> dict[str, Any]:
        """供 AI 解读的每日摘要。

        已经把原始采样聚合好、算好个人基线、标好数据质量 —— **不要**把逐分钟心率
        直接喂给模型。提示词见仓库 `prompts/daily-analysis.md`。
        """
        with open_repo() as repo:
            return build_daily_payload(
                repo, date, baseline_days=baseline_days, trend_days=trend_days
            )

    @app.get("/api/ai/summary", tags=["AI"], dependencies=[Depends(require_auth)])
    def ai_summary(
        date: str | None = Query(default=None, description="YYYY-MM-DD，默认今天"),
        baseline_days: int = Query(default=30, ge=7, le=180),
        trend_days: int = Query(default=7, ge=3, le=30),
        regenerate: bool = Query(default=False, description="忽略缓存，强制重新生成"),
    ) -> dict[str, Any]:
        """调用 AI 生成当日健康解读（JSON）。

        已接入 NVIDIA NIM（OpenAI 兼容）。当天首次生成后会缓存，重复请求直接命中缓存，
        避免重复消耗 API 额度。返回结构与 `prompts/daily-analysis.md` 的参考输出一致。
        """
        ai = current_ai()
        if not ai.configured():
            raise HTTPException(
                status_code=501,
                detail="AI 未配置：请到 AI 设置页启用服务并填写访问地址、API Key 和模型",
            )
        day = date or datetime.now(tz=CN_TZ).date().isoformat()
        with open_repo() as repo:
            payload = build_daily_payload(
                repo, day, baseline_days=baseline_days, trend_days=trend_days
            )
            fingerprint = input_fingerprint(payload)
            if not regenerate:
                cached = repo.get_ai_summary(day)
                if cached and cache_matches(cached, ai, fingerprint):
                    return {**cached["summary"], "_cached": True, "_generated_at": cached["generated_at"]}

            try:
                analysis = AIClient(ai).generate_daily_analysis(payload)
            except AIError as exc:
                raise HTTPException(status_code=502, detail=f"AI 调用失败：{exc}")
            repo.upsert_ai_summary(
                day, analysis, ai.model, PROMPT_VERSION, ai.fingerprint(), fingerprint
            )
            return analysis

    @app.get("/api/ai/summaries", tags=["AI"], dependencies=[Depends(require_auth)])
    def ai_summaries(
        days: int = Query(default=7, ge=1, le=31, description="读取最近 N 天的缓存"),
        end: str | None = Query(default=None, description="结束日期 YYYY-MM-DD，默认今天"),
    ) -> dict[str, Any]:
        """读取最近 N 天的 AI 每日解读缓存（**不触发生成**，未生成的日期 summary 为 null）。"""
        configured = current_ai().configured()
        end_day = end or datetime.now(tz=CN_TZ).date().isoformat()
        end_dt = datetime.fromisoformat(end_day).date()
        with open_repo() as repo:
            items = []
            # 新的在前（今天 → 过去），前端时间轴与「默认展开最新」都依赖此顺序
            for i in range(0, days):
                day = (end_dt - timedelta(days=i)).isoformat()
                cached = repo.get_ai_summary(day) if configured else None
                if cached:
                    items.append(
                        {
                            "date": day,
                            "generated_at": cached["generated_at"],
                            "summary": cached["summary"],
                        }
                    )
                else:
                    items.append({"date": day, "summary": None, "generated_at": None})
        return {"configured": configured, "days": days, "end": end_day, "items": items}

    # ------------------------------------------------------------ 健康对话
    @app.post("/api/ai/chat", tags=["AI"], dependencies=[Depends(require_auth)])
    def ai_chat(body: ChatRequest) -> dict[str, Any]:
        """健康咨询：模型基于用户的健康数据上下文回答，并自行判断是否要记录主观陈述。

        多轮上下文由前端随请求传入（仅本次对话有效，刷新页面即作废），
        服务端不保存对话记录；健康数据作为常态上下文每次都会注入。
        """
        ai = current_ai()
        if not ai.configured():
            raise HTTPException(
                status_code=501,
                detail="AI 未配置：请到 AI 设置页启用服务并填写访问地址、API Key 和模型",
            )
        message = (body.message or "").strip()
        if not message:
            raise HTTPException(status_code=422, detail="消息不能为空")

        # 会话内多轮上下文：只取最近 16 轮、每条截断，防止上下文膨胀
        turns = [
            t for t in body.history[-16:]
            if t.role in ("user", "assistant") and (t.content or "").strip()
        ]
        history_text = "\n".join(
            f"{'用户' if t.role == 'user' else '顾问'}：{t.content.strip()[:600]}"
            for t in turns
        ) or "（本轮是本次对话的第一句）"

        today = datetime.now(tz=CN_TZ).date().isoformat()
        with open_repo() as repo:
            # 今天的解读，没有就找最近 3 天里最新的一份
            today_summary = None
            for back in range(3):
                day = (datetime.now(tz=CN_TZ).date() - timedelta(days=back)).isoformat()
                cached = repo.get_ai_summary(day)
                if cached:
                    today_summary = {"date": day, **cached["summary"]}
                    break
            recent = repo.list_ai_summaries(limit=7)
            latest_reports = repo.list_ai_reports(limit=1)
            notes = repo.get_health_notes(limit=10) if ai.include_health_notes else []

            context = {
                "today": today,
                "today_summary": (
                    json.dumps(today_summary, ensure_ascii=False)
                    if today_summary
                    else "（今日还没有生成解读）"
                ),
                "recent_summaries": (
                    "\n".join(
                        f"- {r['date']} [{r.get('summary', {}).get('status', '?')}] "
                        f"{r.get('summary', {}).get('headline', '')}"
                        for r in recent
                    )
                    or "（无）"
                ),
                "latest_report": (
                    json.dumps(
                        {
                            "title": latest_reports[0]["report"].get("title"),
                            "period": f"{latest_reports[0]['period_start']} ~ {latest_reports[0]['period_end']}",
                            "headline": latest_reports[0]["report"].get("headline"),
                        },
                        ensure_ascii=False,
                    )
                    if latest_reports
                    else "（还没有周报/月报）"
                ),
                "notes": (
                    "\n".join(f"- [{n['note_date']}] {n['content']}" for n in notes)
                    or "（暂无备注）"
                ),
                "chat_history": history_text,
            }
            base_context = CHAT_USER_TEMPLATE.format(**context)
            try:
                reply = AIClient(ai).chat_with_tools(
                    base_context,
                    message,
                    [t.model_dump() for t in turns],
                    _build_tool_executor(repo, config),
                )
            except AIError as exc:
                raise HTTPException(status_code=502, detail=f"AI 调用失败：{exc}")

            # 模型判定值得记的主观陈述 → 健康备注（对话本身不落库）
            reply_text = str(reply.get("reply", "")).strip() or "(未能生成回答，请重试)"
            recorded = False
            note_date = today
            if reply.get("should_record") and str(reply.get("record_text") or "").strip():
                note_date = str(reply.get("note_date") or today)[:10]
                repo.add_health_note(note_date, str(reply["record_text"]).strip(), "chat")
                recorded = True

            return {
                "reply": reply_text,
                "recorded": recorded,
                "note_date": note_date if recorded else None,
            }

    @app.get("/api/ai/notes", tags=["AI"], dependencies=[Depends(require_auth)])
    def ai_notes(limit: int = Query(default=20, ge=1, le=100)) -> dict[str, Any]:
        """健康备注列表（对话中被记录下来的主观陈述 + 手动添加）。"""
        with open_repo() as repo:
            return {"notes": repo.get_health_notes(limit=limit)}

    @app.post("/api/ai/notes", tags=["AI"], dependencies=[Depends(require_auth)])
    def ai_add_note(body: NoteRequest) -> dict[str, Any]:
        """手动添加一条健康备注。"""
        content = (body.content or "").strip()
        if not content:
            raise HTTPException(status_code=422, detail="内容不能为空")
        note_date = (body.note_date or datetime.now(tz=CN_TZ).date().isoformat())[:10]
        with open_repo() as repo:
            note_id = repo.add_health_note(note_date, content, "manual")
        return {"id": note_id, "note_date": note_date, "content": content}

    @app.delete("/api/ai/notes/{note_id}", tags=["AI"], dependencies=[Depends(require_auth)])
    def ai_delete_note(note_id: int) -> dict[str, Any]:
        """删除一条健康备注（对话记忆由用户自主管理）。"""
        with open_repo() as repo:
            if not repo.delete_health_note(note_id):
                raise HTTPException(status_code=404, detail="备注不存在")
        return {"deleted": note_id}

    # ------------------------------------------------------------ 周报 / 月报
    @app.get("/api/ai/reports", tags=["AI"], dependencies=[Depends(require_auth)])
    def ai_reports(
        limit: int = Query(default=10, ge=1, le=50),
        type: str | None = Query(default=None, alias="type", description="weekly / monthly"),
    ) -> dict[str, Any]:
        """周期报告列表（含完整 JSON）。"""
        with open_repo() as repo:
            return {"reports": repo.list_ai_reports(limit=limit, period_type=type)}

    @app.post("/api/ai/report", tags=["AI"], dependencies=[Depends(require_auth)])
    def ai_generate_report(body: ReportRequest) -> dict[str, Any]:
        """生成（或返回缓存的）周报/月报。周报=上一个完整自然周，月报=上个自然月。"""
        ai = current_ai()
        if not ai.configured():
            raise HTTPException(
                status_code=501,
                detail="AI 未配置：请到 AI 设置页启用服务并填写访问地址、API Key 和模型",
            )
        if body.type not in ("weekly", "monthly"):
            raise HTTPException(status_code=422, detail="type 必须是 weekly 或 monthly")

        today = datetime.now(tz=CN_TZ).date()
        start, end = resolve_period_range(body.type, today)
        with open_repo() as repo:
            payload = build_report_payload(repo, body.type, start, end)
            if not ai.include_health_notes:
                payload["health_notes"] = []
            fingerprint = input_fingerprint(payload)
            if not body.regenerate:
                cached = repo.get_ai_report(body.type, start.isoformat(), end.isoformat())
                if (
                    cached
                    and cache_matches(cached["report"], ai, fingerprint)
                ):
                    return {
                        **cached["report"],
                        "period_start": start.isoformat(),
                        "period_end": end.isoformat(),
                        "_cached": True,
                    }
            if not any(
                (payload["sleep"]["days_with_data"], payload["activity"]["days_with_data"])
            ):
                raise HTTPException(
                    status_code=422, detail=f"{start} ~ {end} 没有任何健康数据，无法生成报告"
                )
            try:
                report = AIClient(ai).generate_period_report(
                    payload, body.type, start.isoformat(), end.isoformat()
                )
            except AIError as exc:
                raise HTTPException(status_code=502, detail=f"AI 调用失败：{exc}")
            repo.upsert_ai_report(
                body.type, start.isoformat(), end.isoformat(),
                report, ai.model, PROMPT_VERSION, ai.fingerprint(), fingerprint,
            )
            return {
                **report,
                "period_start": start.isoformat(),
                "period_end": end.isoformat(),
            }

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception) -> JSONResponse:  # noqa: ARG001
        logger.exception("未处理异常: %s", type(exc).__name__)
        return JSONResponse(status_code=500, content={"detail": "内部错误"})

    return app
