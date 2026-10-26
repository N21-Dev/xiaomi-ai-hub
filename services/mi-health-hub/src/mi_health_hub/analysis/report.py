"""周期报告（周报/月报）的聚合 payload。

与 daily.py 同一设计原则：不喂原始采样，只喂聚合结论 + 环比 + 备注。
"""

from __future__ import annotations

import statistics
from datetime import date as date_cls
from datetime import datetime, timedelta, timezone
from typing import Any

from ..store.repository import Repository

CN_TZ = timezone(timedelta(hours=8))


def _avg(values: list[float]) -> float | None:
    clean = [float(v) for v in values if v is not None]
    return round(statistics.fmean(clean), 1) if clean else None


def resolve_period_range(period_type: str, today: date_cls) -> tuple[date_cls, date_cls]:
    """报告覆盖的自然周期：周报 = 上一个完整自然周（周一~周日），月报 = 上个自然月。"""
    if period_type == "monthly":
        first_of_this_month = today.replace(day=1)
        last_month_end = first_of_this_month - timedelta(days=1)
        return last_month_end.replace(day=1), last_month_end
    # 上一个完整自然周：本周一往前推 7 天到上周日
    this_monday = today - timedelta(days=today.weekday())
    last_sunday = this_monday - timedelta(days=1)
    return last_sunday - timedelta(days=6), last_sunday


def _sleep_agg(repo: Repository, start: str, end: str) -> dict[str, Any]:
    rows = repo.conn.execute(
        "SELECT date, total_min, efficiency, deep_min, rem_min, start_ts, has_stage, "
        "quality_flag FROM sleep_session WHERE date >= ? AND date <= ? "
        "AND quality_flag = 'ok' ORDER BY date",
        (start, end),
    ).fetchall()
    totals = [r["total_min"] for r in rows if r["total_min"] is not None]
    effs = [r["efficiency"] for r in rows if r["efficiency"] is not None]
    staged = [r for r in rows if r["has_stage"] == 1]
    # 就寝时间转分钟便于求平均
    bedtimes = [
        int(datetime.fromtimestamp(r["start_ts"], tz=CN_TZ).strftime("%H")) * 60
        + int(datetime.fromtimestamp(r["start_ts"], tz=CN_TZ).strftime("%M"))
        for r in rows
        if r["start_ts"]
    ]
    # 跨零点的就寝（23:xx = 23*60+xx）与凌晨就寝（0:xx）混在一起会算出错误的均值，
    # 把 0:00-12:00 之间视为「前一晚熬过来的」，+24h 归到后半夜再平均
    shifted = [m + 1440 if m < 720 else m for m in bedtimes]
    avg_bed = round(statistics.fmean(shifted) % 1440) if shifted else None
    return {
        "days_with_data": len(rows),
        "avg_total_min": _avg(totals),
        "min_total_min": int(min(totals)) if totals else None,
        "max_total_min": int(max(totals)) if totals else None,
        "avg_efficiency": _avg(effs),
        "avg_deep_min": _avg([r["deep_min"] for r in staged if r["deep_min"] is not None]),
        "avg_rem_min": _avg([r["rem_min"] for r in staged if r["rem_min"] is not None]),
        "stage_days": len(staged),
        "avg_bedtime": (
            f"{avg_bed // 60:02d}:{avg_bed % 60:02d}" if avg_bed is not None else None
        ),
        "debt_min_480": (
            int(sum(max(0, 480 - t) for t in totals)) if totals else None
        ),
        "per_day": [
            {"date": r["date"], "total_min": r["total_min"], "efficiency": r["efficiency"]}
            for r in rows
        ],
    }


def _activity_agg(repo: Repository, start: str, end: str) -> dict[str, Any]:
    rows = repo.conn.execute(
        "SELECT date, steps, distance_m, calories FROM daily_activity "
        "WHERE date >= ? AND date <= ? AND quality_flag = 'ok' ORDER BY date",
        (start, end),
    ).fetchall()
    steps = [r["steps"] for r in rows if r["steps"] is not None]
    return {
        "days_with_data": len(rows),
        "avg_steps": _avg(steps),
        "total_steps": int(sum(steps)) if steps else None,
        "max_steps": int(max(steps)) if steps else None,
        "max_steps_date": (
            max(rows, key=lambda r: r["steps"] or 0)["date"] if steps else None
        ),
        "total_distance_m": int(sum(r["distance_m"] or 0 for r in rows)),
        "total_calories": int(sum(r["calories"] or 0 for r in rows)),
    }


def _rhr_agg(repo: Repository, start: str, end: str) -> dict[str, Any]:
    rows = repo.conn.execute(
        "SELECT date, bpm FROM resting_hr WHERE date >= ? AND date <= ? "
        "ORDER BY date",
        (start, end),
    ).fetchall()
    bpms = [r["bpm"] for r in rows if r["bpm"] is not None]
    half = len(bpms) // 2
    trend: float | None = None
    if half >= 2:
        first, second = _avg(bpms[:half]), _avg(bpms[half:])
        if first is not None and second is not None:
            trend = round(second - first, 1)
    return {
        "days_with_data": len(rows),
        "avg_bpm": _avg(bpms),
        "min_bpm": int(min(bpms)) if bpms else None,
        "max_bpm": int(max(bpms)) if bpms else None,
        "second_half_vs_first_half": trend,
    }


def _workouts_agg(repo: Repository, start: str, end: str) -> dict[str, Any]:
    rows = repo.conn.execute(
        "SELECT type, date(start_ts, 'unixepoch', '+8 hours') AS d, duration_s, "
        "distance_m, calories, avg_hr, max_hr, train_load, train_effect, recover_time_h "
        "FROM workout WHERE date(start_ts, 'unixepoch', '+8 hours') >= ? "
        "AND date(start_ts, 'unixepoch', '+8 hours') <= ? ORDER BY start_ts",
        (start, end),
    ).fetchall()
    return {
        "count": len(rows),
        "total_duration_min": int(sum(r["duration_s"] or 0 for r in rows) // 60),
        "total_distance_m": int(sum(r["distance_m"] or 0 for r in rows)),
        "total_train_load": round(sum(r["train_load"] or 0 for r in rows), 1),
        "items": [
            {
                "date": r["d"],
                "type": r["type"],
                "duration_min": int((r["duration_s"] or 0) // 60),
                "distance_m": r["distance_m"],
                "avg_hr": r["avg_hr"],
                "max_hr": r["max_hr"],
                "train_load": r["train_load"],
                "train_effect": r["train_effect"],
                "recover_time_h": r["recover_time_h"],
            }
            for r in rows
        ],
    }


def _vitals_agg(repo: Repository, start: str, end: str) -> dict[str, Any]:
    spo2 = repo.conn.execute(
        "SELECT MIN(spo2) AS lo, AVG(spo2) AS avg FROM spo2_sample "
        "WHERE date(ts, 'unixepoch', '+8 hours') >= ? "
        "AND date(ts, 'unixepoch', '+8 hours') <= ? AND quality_flag = 'ok'",
        (start, end),
    ).fetchone()
    stress = repo.conn.execute(
        "SELECT AVG(stress) AS avg FROM stress_sample "
        "WHERE date(ts, 'unixepoch', '+8 hours') >= ? "
        "AND date(ts, 'unixepoch', '+8 hours') <= ? AND quality_flag = 'ok'",
        (start, end),
    ).fetchone()
    return {
        "spo2_min": spo2["lo"] if spo2 else None,
        "spo2_avg": round(spo2["avg"], 1) if spo2 and spo2["avg"] else None,
        "stress_avg": round(stress["avg"], 1) if stress and stress["avg"] else None,
    }


def build_report_payload(
    repo: Repository, period_type: str, start: date_cls, end: date_cls
) -> dict[str, Any]:
    """聚合一个周期的数据，交给模型写周报/月报。"""
    start_s, end_s = start.isoformat(), end.isoformat()
    span_days = (end - start).days + 1

    # 环比：上一个等长周期
    prev_end = start - timedelta(days=1)
    prev_start = prev_end - timedelta(days=span_days - 1)

    prev_sleep = _sleep_agg(repo, prev_start.isoformat(), prev_end.isoformat())
    prev_activity = _activity_agg(repo, prev_start.isoformat(), prev_end.isoformat())
    prev_rhr = _rhr_agg(repo, prev_start.isoformat(), prev_end.isoformat())
    prev_workouts = _workouts_agg(repo, prev_start.isoformat(), prev_end.isoformat())

    notes = repo.get_health_notes_between(start_s, end_s)
    generated_at = datetime.now(tz=CN_TZ).isoformat(timespec="seconds")

    return {
        "period_type": period_type,
        "start": start_s,
        "end": end_s,
        "span_days": span_days,
        "generated_at": generated_at,
        "sleep": _sleep_agg(repo, start_s, end_s),
        "activity": _activity_agg(repo, start_s, end_s),
        "resting_hr": _rhr_agg(repo, start_s, end_s),
        "workouts": _workouts_agg(repo, start_s, end_s),
        "vitals": _vitals_agg(repo, start_s, end_s),
        "health_notes": [{"date": n["note_date"], "content": n["content"]} for n in notes],
        "previous_period": {
            "start": prev_start.isoformat(),
            "end": prev_end.isoformat(),
            "sleep_avg_total_min": prev_sleep["avg_total_min"],
            "sleep_debt_min": prev_sleep["debt_min_480"],
            "avg_steps": prev_activity["avg_steps"],
            "resting_hr_avg_bpm": prev_rhr["avg_bpm"],
            "workout_count": prev_workouts["count"],
            "workout_total_train_load": prev_workouts["total_train_load"],
        },
    }
