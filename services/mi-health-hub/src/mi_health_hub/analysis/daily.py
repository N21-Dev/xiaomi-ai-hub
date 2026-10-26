"""分析层：构建「供 AI 解读」的每日摘要。

**不要**把逐分钟心率之类的原始数据直接喂给 LLM：一天 1400 条、全量 36 万行，
既贵又失真。这里先把数据聚合成几十行结构化摘要，算好个人基线、
标好数据质量、跑一遍规则引擎，模型只需要「读结论」。

设计原则：
- 基线用**中位数**（对异常值稳健），并**排除 target 当天**（避免自比自）
- 统计前排除 `quality_flag != 'ok'`；深睡/REM 额外要求 `has_stage = 1`
- 缺失与 0 严格区分：缺数据时字段为 null，并在 data_quality 里说明
"""

from __future__ import annotations

import statistics
from datetime import date as date_cls
from datetime import datetime, timedelta, timezone
from typing import Any

from ..store.repository import Repository

CN_TZ = timezone(timedelta(hours=8))

# 分析用到的睡眠字段。deep/rem 需要 has_stage=1 才可信
STAGE_METRICS = ("deep_min", "rem_min")


def _local_hhmm(ts: int | None) -> str | None:
    if not ts:
        return None
    return datetime.fromtimestamp(int(ts), tz=CN_TZ).strftime("%H:%M")


def _format_pace(seconds: float | None, basis: str | None) -> str | None:
    """配速格式化。单位由 pace_basis 决定（游泳是 100 米，其余是公里）。"""
    if seconds is None:
        return None
    total = int(round(float(seconds)))
    text = f"{total // 60}:{total % 60:02d}"
    suffix = "100米" if basis == "100m" else "公里"
    return f"{text}/{suffix}"


def _seconds_to_min(value: Any) -> float | None:
    return None if value is None else round(float(value) / 60.0, 1)


def _median(values: list[float]) -> float | None:
    clean = [float(v) for v in values if v is not None]
    return round(statistics.median(clean), 3) if clean else None


def _day_range(end: date_cls, days: int) -> tuple[str, str]:
    """返回 [start, end) 的日期字符串区间（含 start，不含 end）。"""
    return (end - timedelta(days=days)).isoformat(), end.isoformat()


# ---------------------------------------------------------------- 基线
def _sleep_baseline(repo: Repository, end_day: date_cls, days: int) -> dict[str, Any]:
    start, end = _day_range(end_day, days)
    rows = repo.conn.execute(
        "SELECT date, total_min, efficiency, deep_min, rem_min, has_stage, has_rem "
        "FROM sleep_session WHERE date >= ? AND date < ? "
        "AND quality_flag = 'ok' AND is_nap = 0",
        (start, end),
    ).fetchall()
    total = [r["total_min"] for r in rows if r["total_min"] is not None]
    eff = [r["efficiency"] for r in rows if r["efficiency"] is not None]
    # 深睡要求有完整分期；REM 还要设备明确识别出 REM，0 不能代表“没有 REM”。
    staged = [r for r in rows if r["has_stage"] == 1]
    rem_ready = [r for r in staged if r["has_rem"] == 1]
    return {
        "sample_days": len(rows),
        "stage_sample_days": len(staged),
        "rem_sample_days": len(rem_ready),
        "sleep_total_min": _median(total),
        "sleep_efficiency": _median(eff),
        "sleep_deep_min": _median([r["deep_min"] for r in staged]),
        "sleep_rem_min": _median([r["rem_min"] for r in rem_ready]),
        "sleep_debt_7d_min": _sleep_debt(repo, end_day, target_min=480),
    }


def _sleep_debt(repo: Repository, end_day: date_cls, target_min: int = 480) -> int | None:
    """近 7 日睡眠债：每天与目标时长的差额累加（只算不足的天）。"""
    start = (end_day - timedelta(days=7)).isoformat()
    rows = repo.conn.execute(
        "SELECT total_min FROM sleep_session WHERE date >= ? AND date < ? "
        "AND quality_flag = 'ok' AND is_nap = 0 AND total_min IS NOT NULL",
        (start, end_day.isoformat()),
    ).fetchall()
    if not rows:
        return None
    return int(sum(max(0, target_min - r["total_min"]) for r in rows))


def _simple_baseline(
    repo: Repository, table: str, column: str, start: str, end: str, *, extra: str = ""
) -> tuple[float | None, int]:
    sql = (
        f"SELECT {column} AS v FROM {table} WHERE date >= ? AND date < ? "
        f"AND quality_flag = 'ok' AND {column} IS NOT NULL {extra}"
    )
    rows = repo.conn.execute(sql, (start, end)).fetchall()  # noqa: S608
    values = [r["v"] for r in rows]
    return _median(values), len(values)


def _training_load(repo: Repository, start: str, end: str) -> list[tuple[str, float]]:
    rows = repo.conn.execute(
        "SELECT date(start_ts, 'unixepoch', '+8 hours') AS d, "
        "SUM(COALESCE(train_load, 0)) AS load FROM workout "
        "WHERE date(start_ts, 'unixepoch', '+8 hours') >= ? "
        "AND date(start_ts, 'unixepoch', '+8 hours') < ? "
        "AND quality_flag = 'ok' GROUP BY d ORDER BY d",
        (start, end),
    ).fetchall()
    return [(r["d"], float(r["load"] or 0)) for r in rows]


# ---------------------------------------------------------------- 规则引擎
def _rule_hits(
    today: dict[str, Any],
    baseline: dict[str, Any],
    trend: dict[str, Any],
    delta: dict[str, Any],
) -> list[dict[str, str]]:
    hits: list[dict[str, str]] = []

    # 静息心率：连续多天高于基线
    rhr_series = [v for v in trend.get("resting_hr_bpm", []) if v is not None]
    base_rhr = baseline.get("resting_hr_bpm")
    if base_rhr and len(rhr_series) >= 3:
        tail = rhr_series[-3:]
        elevated = [v for v in tail if v - base_rhr >= 3]
        if len(elevated) >= 2:
            hits.append(
                {
                    "kind": "rhr_elevated",
                    "severity": "warn",
                    "message": (
                        f"静息心率最近 3 天有 {len(elevated)} 天高于 30 日基线 "
                        f"{base_rhr:.0f}bpm（当前 {rhr_series[-1]:.0f}）"
                    ),
                }
            )

    # 睡眠债
    debt = baseline.get("sleep_debt_7d_min")
    if debt is not None and debt >= 300:
        hits.append(
            {
                "kind": "sleep_debt",
                "severity": "warn" if debt >= 600 else "info",
                "message": f"近 7 日累计睡眠债 {debt} 分钟（目标每天 8 小时）",
            }
        )

    # 负荷比 ACWR：近 7 日总负荷 ÷ 近 28 日日均
    loads = trend.get("training_load", [])
    load_28 = trend.get("_load_28d_total")
    if loads and load_28:
        recent_7 = sum(loads)
        weekly_avg = load_28 / 4.0
        if weekly_avg > 0:
            ratio = recent_7 / weekly_avg
            if ratio >= 1.5:
                hits.append(
                    {
                        "kind": "load_spike",
                        "severity": "warn",
                        "message": f"本周训练负荷是近 4 周周均的 {ratio:.1f} 倍，属于负荷激增区间",
                    }
                )
            elif ratio <= 0.5 and recent_7 > 0:
                hits.append(
                    {
                        "kind": "load_drop",
                        "severity": "info",
                        "message": f"本周训练负荷只有近 4 周周均的 {ratio:.1f} 倍",
                    }
                )

    # 步数骤降
    steps_delta = delta.get("steps")
    if steps_delta is not None and steps_delta <= -5000:
        hits.append(
            {
                "kind": "steps_drop",
                "severity": "info",
                "message": f"今日步数比基线少 {abs(int(steps_delta))} 步",
            }
        )

    # 睡眠时长明显不足
    sleep_delta = delta.get("sleep_total_min")
    if sleep_delta is not None and sleep_delta <= -90:
        hits.append(
            {
                "kind": "short_sleep",
                "severity": "warn",
                "message": f"昨夜睡眠比基线少 {abs(int(sleep_delta))} 分钟",
            }
        )

    # 建议恢复时间未过
    for workout in today.get("workouts") or []:
        if workout.get("recover_time_h"):
            hours = workout["recover_time_h"]
            hits.append(
                {
                    "kind": "recovery_pending",
                    "severity": "info",
                    "message": (
                        f"今天的 {workout['type']} 建议恢复 {hours} 小时，"
                        "下次高强度训练前先留出这段时间"
                    ),
                }
            )

    return hits


# ---------------------------------------------------------------- 主入口
def build_daily_payload(
    repo: Repository,
    target: str | None = None,
    *,
    baseline_days: int = 30,
    trend_days: int = 7,
) -> dict[str, Any]:
    day = date_cls.fromisoformat(target) if target else datetime.now(tz=CN_TZ).date()
    day_str = day.isoformat()

    # ---- 今日 ----
    sleep_row = repo.conn.execute(
        "SELECT * FROM sleep_session WHERE date = ? AND quality_flag = 'ok' AND is_nap = 0",
        (day_str,),
    ).fetchone()
    activity_row = repo.conn.execute(
        "SELECT * FROM daily_activity WHERE date = ? AND quality_flag = 'ok'", (day_str,)
    ).fetchone()
    rhr_row = repo.conn.execute(
        "SELECT * FROM resting_hr WHERE date = ? AND quality_flag = 'ok'", (day_str,)
    ).fetchone()
    workout_rows = repo.conn.execute(
        "SELECT * FROM workout WHERE date(start_ts, 'unixepoch', '+8 hours') = ? "
        "AND quality_flag = 'ok' ORDER BY start_ts",
        (day_str,),
    ).fetchall()

    # 正常心率采样数 → 判断当天是否佩戴；可疑极值不算有效佩戴证据。
    hr_count = repo.conn.execute(
        "SELECT COUNT(*) FROM heart_rate_sample "
        "WHERE date(ts, 'unixepoch', '+8 hours') = ? AND quality_flag = 'ok'",
        (day_str,),
    ).fetchone()[0]

    spo2_row = repo.conn.execute(
        "SELECT MIN(spo2) AS lo FROM spo2_sample "
        "WHERE date(ts, 'unixepoch', '+8 hours') = ? AND quality_flag = 'ok'",
        (day_str,),
    ).fetchone()
    stress_row = repo.conn.execute(
        "SELECT AVG(stress) AS avg FROM stress_sample "
        "WHERE date(ts, 'unixepoch', '+8 hours') = ? AND quality_flag = 'ok'",
        (day_str,),
    ).fetchone()

    sleep = None
    if sleep_row:
        sleep = {
            "date": sleep_row["date"],
            "total_min": sleep_row["total_min"],
            "in_bed_min": sleep_row["in_bed_min"],
            "efficiency": sleep_row["efficiency"],
            "deep_min": sleep_row["deep_min"],
            "light_min": sleep_row["light_min"],
            "rem_min": sleep_row["rem_min"],
            "awake_min": sleep_row["awake_min"],
            "nap_min": sleep_row["nap_min"],
            "bedtime": _local_hhmm(sleep_row["start_ts"]),
            "wake_time": _local_hhmm(sleep_row["end_ts"]),
            "avg_hr": sleep_row["avg_hr"],
            "min_hr": sleep_row["min_hr"],
            "max_hr": sleep_row["max_hr"],
            "avg_spo2": sleep_row["avg_spo2"],
            "min_spo2": sleep_row["min_spo2"],
            "max_spo2": sleep_row["max_spo2"],
            "avg_breath": sleep_row["avg_breath"],   # 睡眠呼吸率，仅约 18% 的记录有
            "awake_cnt": sleep_row["awake_cnt"],
            "has_stage": sleep_row["has_stage"],
            "has_rem": sleep_row["has_rem"],
            "quality_flag": sleep_row["quality_flag"],
        }

    workouts: list[dict[str, Any]] = []
    for row in workout_rows:
        workouts.append(
            {
                "type": row["type"],
                "start": _local_hhmm(row["start_ts"]),
                "duration_s": row["duration_s"],
                "duration_display": f"{row['duration_s'] // 60}分{row['duration_s'] % 60}秒",
                "distance_m": row["distance_m"],
                "calories": row["calories"],
                "avg_hr": row["avg_hr"],
                "max_hr": row["max_hr"],
                "train_effect": row["train_effect"],
                "anaerobic_train_effect": row["anaerobic_train_effect"],
                "train_load": row["train_load"],
                "recover_time_h": row["recover_time_h"],
                "pace": _format_pace(row["avg_pace_s"], row["pace_basis"]),
                "best_pace": _format_pace(row["max_pace_s"], row["pace_basis"]),
                "stroke_count": row["stroke_count"],
                "turn_count": row["turn_count"],
                "avg_swolf": row["avg_swolf"],
                "zones": {
                    "warmup_min": _seconds_to_min(row["zone_warmup_s"]),
                    "fat_burn_min": _seconds_to_min(row["zone_fat_burn_s"]),
                    "aerobic_min": _seconds_to_min(row["zone_aerobic_s"]),
                    "anaerobic_min": _seconds_to_min(row["zone_anaerobic_s"]),
                    "extreme_min": _seconds_to_min(row["zone_extreme_s"]),
                },
            }
        )

    today = {
        "sleep": sleep,
        "resting_hr": {"bpm": rhr_row["bpm"]} if rhr_row else None,
        "activity": (
            {
                "steps": activity_row["steps"],
                "distance_m": activity_row["distance_m"],
                "calories": activity_row["calories"],
            }
            if activity_row
            else None
        ),
        "vitals": {
            "spo2_min": spo2_row["lo"] if spo2_row else None,
            "stress_avg": round(stress_row["avg"], 1) if stress_row and stress_row["avg"] else None,
        },
        "workouts": workouts,
    }

    # ---- 基线（排除 target 当天）----
    base_start, base_end = _day_range(day, baseline_days)
    rhr_base, rhr_days = _simple_baseline(repo, "resting_hr", "bpm", base_start, base_end)
    steps_base, steps_days = _simple_baseline(
        repo, "daily_activity", "steps", base_start, base_end
    )
    baseline = {
        "window_days": baseline_days,
        "sample_days": rhr_days,
        "resting_hr_bpm": rhr_base,
        "steps": steps_base,
        "steps_sample_days": steps_days,
        **_sleep_baseline(repo, day, baseline_days),
    }

    # ---- 偏移量 ----
    delta: dict[str, Any] = {}
    if rhr_row and rhr_base:
        delta["resting_hr_bpm"] = round(rhr_row["bpm"] - rhr_base, 1)
    if sleep and baseline.get("sleep_total_min"):
        delta["sleep_total_min"] = int(sleep["total_min"] - baseline["sleep_total_min"])
    if sleep and baseline.get("sleep_efficiency"):
        delta["sleep_efficiency"] = round(sleep["efficiency"] - baseline["sleep_efficiency"], 4)
    if activity_row and steps_base:
        delta["steps"] = int(activity_row["steps"] - steps_base)

    # ---- 近 N 日趋势 ----
    trend_start, trend_end = _day_range(day + timedelta(days=1), trend_days)
    rhr_series = [
        r["bpm"]
        for r in repo.conn.execute(
            "SELECT bpm FROM resting_hr WHERE date >= ? AND date < ? "
            "AND quality_flag = 'ok' ORDER BY date",
            (trend_start, trend_end),
        )
    ]
    sleep_series = [
        r["total_min"]
        for r in repo.conn.execute(
            "SELECT total_min FROM sleep_session WHERE date >= ? AND date < ? "
            "AND quality_flag = 'ok' AND is_nap = 0 ORDER BY date",
            (trend_start, trend_end),
        )
    ]
    steps_series = [
        r["steps"]
        for r in repo.conn.execute(
            "SELECT steps FROM daily_activity WHERE date >= ? AND date < ? "
            "AND quality_flag = 'ok' ORDER BY date",
            (trend_start, trend_end),
        )
    ]
    bedtime_series = [
        _local_hhmm(r["start_ts"])
        for r in repo.conn.execute(
            "SELECT start_ts FROM sleep_session WHERE date >= ? AND date < ? "
            "AND quality_flag = 'ok' AND is_nap = 0 ORDER BY date",
            (trend_start, trend_end),
        )
    ]
    load_map = dict(_training_load(repo, trend_start, trend_end))
    load_series = [
        load_map.get((day + timedelta(days=1) - timedelta(days=trend_days - i)).isoformat(), 0.0)
        for i in range(trend_days)
    ]
    load_28_map = dict(_training_load(repo, base_start, base_end))
    load_28_total = round(sum(load_28_map.values()), 1)

    trend = {
        "window_days": trend_days,
        "resting_hr_bpm": rhr_series,
        "sleep_total_min": sleep_series,
        "steps": steps_series,
        "training_load": load_series,
        "bedtime_local": bedtime_series,
        "_load_28d_total": load_28_total,
    }

    # ---- 数据质量 ----
    missing: list[str] = []
    for name, present in (
        ("sleep", sleep is not None),
        ("activity", activity_row is not None),
        ("resting_hr", rhr_row is not None),
        ("workouts", bool(workouts)),
        ("spo2", bool(spo2_row and spo2_row["lo"] is not None)),
        ("stress", bool(stress_row and stress_row["avg"] is not None)),
    ):
        if not present:
            missing.append(name)

    notes: list[str] = []
    worn = hr_count >= 100 or bool(steps_series)
    if not worn:
        notes.append("当天心率采样极少，可能没有佩戴手表")
    stage_available = bool(sleep and sleep["has_stage"] == 1)
    rem_available = bool(stage_available and sleep and sleep["has_rem"] == 1)
    if sleep and not stage_available:
        notes.append("当晚没有睡眠分期数据，深睡/REM 不可用（不代表为 0）")
    elif sleep and not rem_available:
        notes.append("当晚没有可靠 REM 数据，REM 不可用（不代表为 0）")
    if sleep and sleep["quality_flag"] != "ok":
        notes.append(f"睡眠记录质量标记为 {sleep['quality_flag']}，解读需谨慎")

    # 需要模型跳过的项
    insufficient: list[str] = ["hrv"]  # 设备硬件不支持，永远没有
    if not stage_available or baseline.get("stage_sample_days", 0) < 5:
        insufficient.append("deep_sleep_trend")
    if not rem_available or baseline.get("rem_sample_days", 0) < 5:
        insufficient.append("rem_trend")
    if not workouts:
        insufficient.append("workout_analysis")
    if (baseline.get("sample_days") or 0) < 10:
        insufficient.append("baseline_comparison")

    payload = {
        "date": day_str,
        "generated_at": datetime.now(tz=CN_TZ).isoformat(timespec="seconds"),
        "data_quality": {
            "worn": worn,
            "has_sleep": sleep is not None,
            "sleep_stage_available": stage_available,
            "sleep_rem_available": rem_available,
            "heart_rate_samples": hr_count,
            "notes": notes,
            "missing": missing,
        },
        "today": today,
        "baseline_30d": baseline,
        "delta": delta,
        "trend_7d": trend,
        "insufficient_data": insufficient,
    }
    payload["rule_hits"] = _rule_hits(today, baseline, trend, delta)
    # 内部用的辅助字段不进 payload
    payload["trend_7d"].pop("_load_28d_total", None)
    return payload
