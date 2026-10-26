"""命令行入口。

子命令：
  init-db   建库（幂等）
  login     校验凭据并保存被轮换的 passToken
  probe     探测各数据 key 在当前账号下的记录数与时间跨度（只读，不写库）
  sentinel  只跑哨兵探测
  sync      执行同步（T1 增量 / T2 修正 / T3 全量回填）
  status    同步状态与表统计
  backup    备份数据库
  serve     启动 API + Web 看板
  export    导出 CSV / JSON
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .auth.store import CredentialError, CredentialStore, Credentials
from .analysis.report import build_report_payload, resolve_period_range
from .cloud.client import MiAuthError, MiHealthClient, MiRateLimitError, RequestBudget
from .cloud.endpoints import KEY_SPECS, SYNC_KEYS
from .config import Config, ConfigError, load_config
from .ai import AIClient, AIError, PROMPT_VERSION
from .logging_setup import setup_logging
from .store.repository import Repository, connect, init_db, now_ts
from .sync.engine import SyncEngine, build_engine

CN_TZ = timezone(timedelta(hours=8))


# ---------------------------------------------------------------- 公共
def _resolve_config(args: argparse.Namespace) -> Config:
    """加载配置；环境变量没给凭据时回退到凭据文件。"""
    config = load_config(getattr(args, "config", None))

    if not config.user_id or not config.pass_token:
        store = CredentialStore()
        creds = store.load()
        if creds:
            config.user_id = creds.user_id
            config.pass_token = creds.pass_token
            if not config.region:
                config.region = creds.region

    if not config.user_id or not config.pass_token:
        raise ConfigError(
            "缺少小米账号凭据。\n"
            "方式一：设置环境变量 MI_USER_ID / MI_PASS_TOKEN\n"
            "方式二（推荐）：打开健康看板的「账号」窗口完成小米网页登录，\n"
            "        服务会自动验证并写入 /var/lib/mi-health-hub/health-credentials.json（权限 600）。\n"
            "        文件与小爱音箱凭据分离，登录轮换不会互相覆盖。"
        )
    return config


def _open_repo(config: Config) -> Repository:
    config.paths.ensure()
    conn = connect(config.paths.db)
    init_db(conn)
    return Repository(conn)


def _make_client(config: Config) -> MiHealthClient:
    return MiHealthClient(
        user_id=config.user_id,
        pass_token=config.pass_token,
        region=config.region,
        interval_ms=config.sync.request_interval_ms,
        timeout_s=config.sync.http_timeout_s,
        retries=config.sync.request_retries,
        max_pages=config.sync.max_pages,
        budget=RequestBudget(
            limit=config.sync.max_requests_per_day,
            storage_path=config.paths.db.with_name("request-budget.sqlite3"),
            account=f"{config.region}:{config.user_id}",
        ),
    )


# ---------------------------------------------------------------- 命令
def cmd_init_db(args: argparse.Namespace) -> int:
    config = load_config(getattr(args, "config", None))
    repo = _open_repo(config)
    print(f"数据库就绪: {config.paths.db}")
    print(f"  WAL 模式: {repo.conn.execute('PRAGMA journal_mode').fetchone()[0]}")
    print(f"  完整性:   {repo.integrity_check()}")
    return 0


def cmd_login(args: argparse.Namespace) -> int:
    config = _resolve_config(args)
    client = _make_client(config)
    try:
        client.login()
    finally:
        pass
    print("登录成功")
    print(f"  userId:   {client.user_id[:3]}***{client.user_id[-3:] if len(client.user_id) > 6 else ''}")
    print(f"  region:   {config.region}")
    print(f"  base_url: {client.base_url}")

    changed = client.pass_token != config.pass_token
    if changed:
        CredentialStore().save(
            Credentials(user_id=client.user_id, pass_token=client.pass_token, region=config.region)
        )
        print("  passToken 已被服务端轮换，新值已写回凭据文件")
    else:
        print("  passToken 未变化")
    client.close()
    return 0


def cmd_probe(args: argparse.Namespace) -> int:
    config = _resolve_config(args)
    keys = args.keys.split(",") if args.keys else list(SYNC_KEYS)
    end = args.end or datetime.now(tz=CN_TZ).date().isoformat()
    if args.start:
        start = args.start
    elif args.days:
        start = (datetime.fromisoformat(end) - timedelta(days=args.days)).isoformat()
    else:
        start = (datetime.fromisoformat(end) - timedelta(days=7)).isoformat()

    client = _make_client(config)
    try:
        client.login()
    except MiAuthError as exc:
        print(f"[FAIL] 认证失败: {exc}", file=sys.stderr)
        return 2

    print(f"探测区间 {start} ~ {end}（region={config.region}）\n")
    print(f"{'key':<22}{'记录数':>8}  {'最早':<20}{'最新':<20}说明")
    print("-" * 96)
    try:
        for key in keys:
            spec = KEY_SPECS.get(key)
            note = spec.description if spec else "(未在 KEY_SPECS 中登记)"
            try:
                records = client.fetch_key(key, start, end)
                stamps = sorted(int(item.get("time", 0) or 0) for item in records)
                first = _fmt_ts(stamps[0]) if stamps else "-"
                last = _fmt_ts(stamps[-1]) if stamps else "-"
                print(f"{key:<22}{len(records):>8}  {first:<20}{last:<20}{note}")
            except MiAuthError:
                raise
            except MiRateLimitError as exc:
                print(f"{key:<22}{'-':>8}  {'':<20}{'':<20}限流: {exc}")
                break
            except Exception as exc:  # noqa: BLE001 - 单个 key 失败不影响其他
                print(f"{key:<22}{'ERR':>8}  {'':<20}{'':<20}{type(exc).__name__}: {exc}")

        try:
            sports = client.fetch_sport_records(start, end)
            print(f"{'sport_records':<22}{len(sports):>8}  {'':<20}{'':<20}运动记录")
        except Exception as exc:  # noqa: BLE001
            print(f"{'sport_records':<22}{'ERR':>8}  {'':<20}{'':<20}{type(exc).__name__}: {exc}")
    finally:
        client.close()

    print(f"\n本次请求数: {client.budget.count}")
    return 0


def _fmt_ts(ts: int) -> str:
    if not ts:
        return "-"
    return datetime.fromtimestamp(ts, tz=CN_TZ).strftime("%Y-%m-%d %H:%M:%S")


def cmd_sentinel(args: argparse.Namespace) -> int:
    config = _resolve_config(args)
    repo = _open_repo(config)
    engine = build_engine(config, repo)
    try:
        result = engine.run_with_sentinel()
    except MiAuthError as exc:
        print(f"[FAIL] 认证失效，请重新获取 userId/passToken: {exc}", file=sys.stderr)
        return 2
    except MiRateLimitError as exc:
        print(f"[STOP] {exc}", file=sys.stderr)
        return 3
    _print_result(result)
    return 0


def cmd_sync(args: argparse.Namespace) -> int:
    config = _resolve_config(args)
    repo = _open_repo(config)
    engine = build_engine(config, repo)

    if args.all:
        print(">>> 全量回填，按 chunk 分块串行拉取（耗时较长，请勿中断）")
    only_keys = [k.strip() for k in args.keys.split(",")] if args.keys else None
    if only_keys:
        print(f">>> 只同步这些 key: {only_keys}")
    try:
        result = engine.run(args.tier, start_date=args.start, end_date=args.end, keys=only_keys)
    except MiAuthError as exc:
        print(f"[FAIL] 认证失效: {exc}", file=sys.stderr)
        return 2
    except MiRateLimitError as exc:
        print(f"[STOP] {exc}", file=sys.stderr)
        return 3

    _print_result(result)
    if args.backup:
        path = engine.backup_database()
        print(f"数据库备份: {path or '失败'}")
    engine.client.close()
    return 0 if not result.failed_keys else 1


def _print_result(result: object) -> None:
    from .sync.engine import SyncResult

    assert isinstance(result, SyncResult)
    print(f"\n档位 {result.tier} → {result.status}")
    if result.note:
        print(f"  说明: {result.note}")
    print(f"  成功 key: {', '.join(result.ok_keys) or '-'}")
    if result.failed_keys:
        for key, err in result.failed_keys.items():
            print(f"  失败 {key}: {err}")
    print(f"  写入行数: {result.rows_written}")


def cmd_status(args: argparse.Namespace) -> int:
    config = load_config(getattr(args, "config", None))
    config.paths.ensure()
    conn = connect(config.paths.db)
    init_db(conn)
    repo = Repository(conn)

    print(f"数据库: {config.paths.db}")
    print(f"完整性: {repo.integrity_check()}\n")

    print("表统计")
    for table, count in repo.table_counts().items():
        print(f"  {table:<22}{count:>10}")

    print("\n同步游标")
    states = repo.all_sync_states()
    if not states:
        print("  （尚未同步）")
    for state in states:
        cursor = _fmt_ts(state["cursor_ts"]) if state["cursor_ts"] else "-"
        last = _fmt_ts(state["last_success_at"]) if state["last_success_at"] else "-"
        fail = state["consecutive_fail"]
        flag = f"  连续失败 {fail} 次" if fail else ""
        print(f"  {state['key_name']:<22}游标 {cursor:<20} 上次成功 {last}{flag}")
        if state["last_error"]:
            print(f"    最近错误: {state['last_error'][:120]}")

    print("\n最近同步运行")
    for run in repo.recent_sync_runs(limit=10):
        finished = _fmt_ts(run["finished_at"]) if run["finished_at"] else "进行中"
        print(
            f"  #{run['id']} {run['tier']:<3} {run['status']:<8} "
            f"开始 {_fmt_ts(run['started_at'])} 结束 {finished} 请求 {run['request_count']}"
        )
    return 0


def cmd_backup(args: argparse.Namespace) -> int:
    config = _resolve_config(args)
    repo = _open_repo(config)
    engine = build_engine(config, repo)
    path = engine.backup_database()
    if not path:
        print("备份失败，详见日志", file=sys.stderr)
        return 1
    print(f"备份完成: {path}")
    print(f"完整性: {repo.integrity_check()}")
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    config = load_config(getattr(args, "config", None))
    repo = _open_repo(config)
    config.paths.output.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(tz=CN_TZ).strftime("%Y%m%d-%H%M%S")

    exported: list[Path] = []
    for table in args.tables.split(","):
        rows = repo.conn.execute(f"SELECT * FROM {table}").fetchall()  # noqa: S608
        if not rows:
            continue
        target = config.paths.output / f"{table}-{stamp}.csv"
        with target.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(rows[0].keys())
            writer.writerows([tuple(row) for row in rows])
        exported.append(target)

    if not exported:
        print("没有可导出的数据")
        return 1
    for path in exported:
        print(f"已导出: {path}")
    return 0


def cmd_prune_archive(args: argparse.Namespace) -> int:
    """清理失配的原始归档（默认只报告，加 --apply 才真删）。"""
    from .sync.raw_archive import RawArchive, prune_stale

    config = load_config(getattr(args, "config", None))
    repo = _open_repo(config)
    archive = RawArchive(config.paths.raw)

    stats = prune_stale(repo, archive, dry_run=not args.apply)
    mode = "已删除" if args.apply else "仅检查（加 --apply 才会真删）"
    print(f"归档检查完成 [{mode}]")
    print(f"  检查条目:   {stats['checked']}")
    print(f"  哈希失配:   {stats['stale']}")
    print(f"  文件丢失:   {stats['missing']}")
    if args.apply:
        print(f"  已删文件:   {stats['removed_files']}")
    return 0


def cmd_reparse_workouts(args: argparse.Namespace) -> int:
    """用库里已存的 raw_json 重算运动记录字段，**不访问云端**。

    解析逻辑改进后（比如开始提取划水次数、训练负荷）用它补齐历史数据，
    不必重新拉一遍 —— 这正是当初把原始响应存下来的意义。
    """
    import json as _json

    from .cloud import parsers

    config = load_config(getattr(args, "config", None))
    repo = _open_repo(config)

    rows = repo.conn.execute("SELECT id, type, raw_json FROM workout").fetchall()
    if not rows:
        print("库里没有运动记录")
        return 1

    updated = 0
    skipped = 0
    for row in rows:
        try:
            payload = _json.loads(row["raw_json"] or "{}")
        except ValueError:
            skipped += 1
            continue
        if not isinstance(payload, dict) or not payload:
            skipped += 1
            continue

        extras = parsers.workout_extras(payload, row["type"] or "")
        assignments = ", ".join(f"{column} = :{column}" for column in extras)
        repo.conn.execute(
            f"UPDATE workout SET {assignments} WHERE id = :id",  # noqa: S608
            {**extras, "id": row["id"]},
        )
        updated += 1

    print(f"重解析完成：更新 {updated} 条，跳过 {skipped} 条（raw_json 缺失或损坏）")
    print("未访问云端，全部来自本地归档。")
    return 0


def cmd_daily(args: argparse.Namespace) -> int:
    """输出供 AI 解读的每日摘要（可直接作为提示词里的 payload）。"""
    from .analysis.daily import build_daily_payload

    config = load_config(getattr(args, "config", None))
    repo = _open_repo(config)
    payload = build_daily_payload(repo, args.date)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from .api.app import create_app

    config = _resolve_config(args)
    app = create_app(config)
    uvicorn.run(
        app,
        host=config.server.api_host,
        port=config.server.api_port,
        log_config=None,
        access_log=False,
    )
    return 0


# ---------------------------------------------------------------- AI
def cmd_ai_config(args: argparse.Namespace) -> int:
    """展示 AI 配置状态（不输出密钥明文）。"""
    config = load_config(getattr(args, "config", None))
    ai = config.ai
    key = ai.api_key
    masked = (key[:6] + "…" + key[-4:]) if len(key) > 10 else ("已设置" if key else "（空）")
    print("AI 每日分析配置")
    print(f"  启用:       {ai.enabled}")
    print(f"  Base URL:   {ai.base_url}")
    print(f"  API Key:    {masked}")
    print(f"  模型:       {ai.model}")
    print(f"  max_tokens: {ai.max_tokens}   temperature: {ai.temperature}")
    print(f"  超时:       {ai.timeout_s}s   同步后自动生成: {ai.auto_after_sync}")
    print(f"  提示词版本: {PROMPT_VERSION}")
    print(f"  就绪:       {ai.configured()}")
    return 0


def cmd_ai_summary(args: argparse.Namespace) -> int:
    """调用 AI 生成某日（或最近 N 天）健康解读。结果缓存到库里。

    --days 1 时打印完整 JSON；--days > 1 时批量生成并紧凑输出（每天一行）。
    批量模式遇限流（429）会自动退避重试，天与天之间间隔 --delay 秒避免触发限流。
    """
    from .analysis.daily import build_daily_payload

    config = load_config(getattr(args, "config", None))
    if not config.ai.configured():
        print(
            "[FAIL] AI 未配置：需设置 MI_AI_ENABLED=true、MI_AI_API_KEY、MI_AI_MODEL",
            file=sys.stderr,
        )
        return 2

    repo = _open_repo(config)
    client = AIClient(config.ai)

    if args.date:
        days = [args.date]
    else:
        today = datetime.now(tz=CN_TZ).date()
        n = max(1, args.days)
        days = [(today - timedelta(days=i)).isoformat() for i in range(n - 1, -1, -1)]

    def generate(day: str):
        payload = build_daily_payload(
            repo, day, baseline_days=args.baseline_days, trend_days=args.trend_days
        )
        last_err: AIError | None = None
        for attempt in range(args.retries):
            try:
                return client.generate_daily_analysis(payload), None
            except AIError as exc:
                last_err = exc
                if "429" in str(exc):
                    wait = 10 * (attempt + 1)
                    print(f"  [限流] {day} 第 {attempt + 1} 次重试前等待 {wait}s…", file=sys.stderr)
                    time.sleep(wait)
                else:
                    break
        return None, last_err

    if len(days) == 1:
        analysis, err = generate(days[0])
        if err:
            print(f"[FAIL] AI 调用失败：{err}", file=sys.stderr)
            return 2
        repo.upsert_ai_summary(
            days[0], analysis, config.ai.model, PROMPT_VERSION, config.ai.fingerprint()
        )
        print(json.dumps(analysis, ensure_ascii=False, indent=2))
        return 0

    print(f"批量生成 {len(days)} 天（间隔 {args.delay}s，429/5xx 自动退避；失败的天会多轮重跑）")
    pending = list(days)
    ok_days: dict[str, dict] = {}
    for pass_no in range(1, max(1, args.passes) + 1):
        if not pending:
            break
        if pass_no > 1:
            print(f"第 {pass_no} 轮重跑 {len(pending)} 天（等待 15s）")
            time.sleep(15)
        still: list[str] = []
        for idx, day in enumerate(pending):
            analysis, err = generate(day)
            if err:
                print(f"  {day}: ❌ {err}")
                still.append(day)
            else:
                repo.upsert_ai_summary(
                    day, analysis, config.ai.model, PROMPT_VERSION, config.ai.fingerprint()
                )
                ok_days[day] = analysis
                print(
                    f"  {day}: {analysis.get('status', '?'):<7} "
                    f"{analysis.get('headline', '')}"
                )
            if idx < len(pending) - 1 and args.delay:
                time.sleep(args.delay)
        pending = still
    print(f"完成：成功 {len(ok_days)} / {len(days)}")
    return 0 if not pending else 2


def cmd_ai_report(args: argparse.Namespace) -> int:
    """生成周报/月报（默认读缓存；--refresh 强制重新生成）。"""
    config = load_config(getattr(args, "config", None))
    if not config.ai.configured():
        print(
            "[FAIL] AI 未配置：需设置 MI_AI_ENABLED=true、MI_AI_API_KEY、MI_AI_MODEL",
            file=sys.stderr,
        )
        return 2

    repo = _open_repo(config)
    client = AIClient(config.ai)
    today = datetime.now(tz=CN_TZ).date()
    start, end = resolve_period_range(args.type, today)
    label = "周报" if args.type == "weekly" else "月报"

    if not args.refresh:
        cached = repo.get_ai_report(args.type, start.isoformat(), end.isoformat())
        if (
            cached
            and cached["report"].get("model") == config.ai.model
            and cached["report"].get("prompt_version") == PROMPT_VERSION
            and cached["report"].get("config_fingerprint") == config.ai.fingerprint()
        ):
            print(json.dumps(cached["report"], ensure_ascii=False, indent=2))
            return 0

    payload = build_report_payload(repo, args.type, start, end)
    if not config.ai.include_health_notes:
        payload["health_notes"] = []
    print(f"生成{label}（{start} ~ {end}）…", file=sys.stderr)
    try:
        report = client.generate_period_report(
            payload, args.type, start.isoformat(), end.isoformat()
        )
    except AIError as exc:
        print(f"[FAIL] AI 调用失败：{exc}", file=sys.stderr)
        return 2
    repo.upsert_ai_report(
        args.type, start.isoformat(), end.isoformat(),
        report, config.ai.model, PROMPT_VERSION, config.ai.fingerprint(),
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


# ---------------------------------------------------------------- 参数
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mi-health",
        description="小米手表健康数据自托管分析平台",
    )
    parser.add_argument("--config", help="配置文件路径（默认 /etc/mi-health-hub/config.yaml）")
    parser.add_argument("--log-level", default="INFO")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db", help="建库（幂等）").set_defaults(func=cmd_init_db)
    sub.add_parser("login", help="校验凭据并保存轮换后的 passToken").set_defaults(func=cmd_login)

    probe = sub.add_parser("probe", help="探测各 key 的记录数（只读）")
    probe.add_argument("--start", help="起始日期 YYYY-MM-DD")
    probe.add_argument("--end", help="结束日期 YYYY-MM-DD")
    probe.add_argument("--days", type=int, default=7, help="相对今天的天数窗口（默认 7）")
    probe.add_argument("--keys", help="逗号分隔的 key 列表，默认全部")
    probe.set_defaults(func=cmd_probe)

    sub.add_parser("sentinel", help="只跑哨兵探测，有新数据才触发增量").set_defaults(
        func=cmd_sentinel
    )

    sync = sub.add_parser("sync", help="执行同步")
    sync.add_argument("--tier", choices=["T1", "T2", "T3"], default="T1")
    sync.add_argument("--start", help="起始日期（T3 回填用）")
    sync.add_argument("--end", help="结束日期")
    sync.add_argument("--keys", help="只同步指定 key（逗号分隔），如 sleep,spo2")
    sync.add_argument("--all", action="store_true", help="标记为全量回填（仅提示用）")
    sync.add_argument("--backup", action="store_true", help="同步后立即备份数据库")
    sync.set_defaults(func=cmd_sync)

    sub.add_parser("status", help="同步状态与表统计").set_defaults(func=cmd_status)
    sub.add_parser("backup", help="备份数据库").set_defaults(func=cmd_backup)
    sub.add_parser("serve", help="启动 API + Web 看板").set_defaults(func=cmd_serve)

    prune = sub.add_parser("prune-archive", help="清理失配的原始归档（默认只检查）")
    prune.add_argument("--apply", action="store_true", help="真正执行删除")
    prune.set_defaults(func=cmd_prune_archive)

    sub.add_parser(
        "reparse-workouts",
        help="用本地 raw_json 重算运动记录字段（不访问云端）",
    ).set_defaults(func=cmd_reparse_workouts)

    daily = sub.add_parser("daily", help="输出供 AI 解读的每日摘要 JSON")
    daily.add_argument("--date", help="YYYY-MM-DD，默认今天")
    daily.set_defaults(func=cmd_daily)

    sub.add_parser("ai-config", help="展示 AI 配置状态（不泄露密钥）").set_defaults(
        func=cmd_ai_config
    )

    ai_summary = sub.add_parser("ai-summary", help="调用 AI 生成某日（或最近 N 天）健康解读（JSON）")
    ai_summary.add_argument("--date", help="YYYY-MM-DD，指定单日；不指定则按 --days 取最近 N 天")
    ai_summary.add_argument("--days", type=int, default=1, help="不指定 --date 时，生成最近 N 天")
    ai_summary.add_argument("--delay", type=float, default=3, help="批量模式下天与天的间隔秒数")
    ai_summary.add_argument("--retries", type=int, default=3, help="单日遇限流(429)最大重试次数")
    ai_summary.add_argument(
        "--passes", type=int, default=5,
        help="批量模式整体重跑轮数上限（失败的天进入下一轮，直到全部成功）",
    )
    ai_summary.add_argument("--baseline-days", type=int, default=30, help="基线窗口天数")
    ai_summary.add_argument("--trend-days", type=int, default=7, help="趋势窗口天数")
    ai_summary.set_defaults(func=cmd_ai_summary)

    ai_report = sub.add_parser("ai-report", help="生成健康周报/月报（JSON，缓存到库里）")
    ai_report.add_argument(
        "--type", choices=["weekly", "monthly"], default="weekly",
        help="weekly=上一个完整自然周；monthly=上个自然月",
    )
    ai_report.add_argument("--refresh", action="store_true", help="忽略缓存，强制重新生成")
    ai_report.set_defaults(func=cmd_ai_report)

    export = sub.add_parser("export", help="导出 CSV")
    export.add_argument(
        "--tables",
        default="daily_activity,sleep_session,resting_hr,workout",
        help="逗号分隔的表名",
    )
    export.set_defaults(func=cmd_export)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    setup_logging(getattr(args, "log_level", "INFO"))
    try:
        return int(args.func(args))
    except CredentialError as exc:
        print(f"[FAIL] {exc}", file=sys.stderr)
        return 2
    except ConfigError as exc:
        print(f"[FAIL] {exc}", file=sys.stderr)
        return 2
    except MiAuthError as exc:
        print(f"[FAIL] 认证失效: {exc}", file=sys.stderr)
        return 2
    except MiRateLimitError as exc:
        print(f"[STOP] {exc}", file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        print("\n已中断", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
