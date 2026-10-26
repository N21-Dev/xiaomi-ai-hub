"""周期聚合：日报 / 周报 / 月报的指标计算与环比。

指标口径（重要，改之前先读）：
- 卡路里 days_activity.calories = 活动消耗（不含基础代谢），逐日汇总/求日均
- 步数 直接取 daily_activity.steps
- 活动时间 / 高强度运动时间：云端未提供 active_min（预留字段全空），
  改为**由心率采样推算**——按分钟取该分钟最大 bpm，然后
  `bpm >= 100` 的分钟数记为活动时间，`bpm >= 140` 的分钟数记为高强度时间。
  静息心率约 50，故 100 约等于「中等强度以上」，140 约等于「高强度」。
- 睡眠时间 = sleep_session.total_min（主睡眠，不含小睡）；小睡单独给 nap_min
- 心率序列：折线图用，日报给当天分钟级曲线，周/月报给逐日平均心率
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

from ..store.repository import Repository

CN_TZ = timezone(timedelta(hours=8))  # 业务日统一按北京时间

ACTIVE_BPM = 100   # 活动时间阈值
VIGOROUS_BPM = 140  # 高强度阈值


@dataclass(frozen=True)
class Period:
    kind: str          # daily / weekly / monthly
    start: date
    end: date

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1

    @property
    def previous(self) -> "Period":
        if self.kind == "monthly":
            prev_end = self.start - timedelta(days=1)
            prev_start = prev_end.replace(day=1)
            return Period(self.kind, prev_start, prev_end)
        span = timedelta(days=self.days)
        return Period(self.kind, self.start - span, self.start - timedelta(days=1))

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "start": self.start.isoformat(),
                "end": self.end.isoformat(), "days": self.days}


def resolve_period(kind: str, ref: date | None = None) -> Period:
    """把「某一天」解析成它所属的日/周/月区间（周一为一周之始）。"""
    ref = ref or date.today()
    if kind == "daily":
        return Period("daily", ref, ref)
    if kind == "weekly":
        start = ref - timedelta(days=ref.weekday())
        return Period("weekly", start, start + timedelta(days=6))
    if kind == "monthly":
        start = ref.replace(day=1)
        nxt = (start + timedelta(days=32)).replace(day=1)
        return Period("monthly", start, nxt - timedelta(days=1))
    raise ValueError(f"未知周期类型 {kind}")


def _daily_hr_agg(repo: Repository, start: date, end: date) -> dict[str, dict[str, Any]]:
    """按分钟取最大心率后，逐日汇总活动/高强度分钟数与心率统计。"""
    rows = repo.conn.execute(
        """
        WITH per_min AS (
          SELECT CAST(ts / 60 AS INTEGER) AS minute, MAX(bpm) AS bpm
          FROM heart_rate_sample
          WHERE date(ts, 'unixepoch', '+8 hours') BETWEEN ? AND ?
            AND quality_flag = 'ok' AND bpm IS NOT NULL
          GROUP BY minute
        )
        SELECT date(minute * 60, 'unixepoch', '+8 hours') AS d,
               SUM(bpm >= ?) AS active_min,
               SUM(bpm >= ?) AS vigorous_min,
               ROUND(AVG(bpm), 1) AS hr_avg,
               MAX(bpm) AS hr_max,
               MIN(bpm) AS hr_min,
               COUNT(*) AS samples
        FROM per_min GROUP BY d ORDER BY d
        """,
        (start.isoformat(), end.isoformat(), ACTIVE_BPM, VIGOROUS_BPM),
    ).fetchall()
    return {r["d"]: dict(r) for r in rows}


def _activity_by_day(repo: Repository, start: date, end: date) -> dict[str, dict[str, Any]]:
    rows = repo.conn.execute(
        "SELECT date, steps, calories, distance_m FROM daily_activity "
        "WHERE date BETWEEN ? AND ? AND quality_flag = 'ok' ORDER BY date",
        (start.isoformat(), end.isoformat()),
    ).fetchall()
    return {r["date"]: dict(r) for r in rows}


def _sleep_by_day(repo: Repository, start: date, end: date) -> dict[str, dict[str, Any]]:
    rows = repo.conn.execute(
        "SELECT date, "
        "CASE WHEN is_nap = 0 THEN total_min END AS total_min, nap_min, "
        "CASE WHEN is_nap = 0 THEN efficiency END AS efficiency, "
        "CASE WHEN is_nap = 0 AND has_stage = 1 THEN deep_min END AS deep_min, "
        "CASE WHEN is_nap = 0 AND has_stage = 1 AND has_rem = 1 THEN rem_min END AS rem_min, "
        "CASE WHEN is_nap = 0 AND has_stage = 1 THEN light_min END AS light_min, "
        "CASE WHEN is_nap = 0 AND has_stage = 1 THEN awake_min END AS awake_min, "
        "CASE WHEN is_nap = 0 THEN has_stage ELSE 0 END AS has_stage, "
        "CASE WHEN is_nap = 0 THEN has_rem ELSE 0 END AS has_rem "
        "FROM sleep_session WHERE date BETWEEN ? AND ? AND quality_flag = 'ok' ORDER BY date",
        (start.isoformat(), end.isoformat()),
    ).fetchall()
    return {r["date"]: dict(r) for r in rows}


def _rhr_by_day(repo: Repository, start: date, end: date) -> dict[str, int]:
    rows = repo.conn.execute(
        "SELECT date, bpm FROM resting_hr WHERE date BETWEEN ? AND ? "
        "AND quality_flag = 'ok' ORDER BY date",
        (start.isoformat(), end.isoformat()),
    ).fetchall()
    return {r["date"]: r["bpm"] for r in rows if r["bpm"] is not None}


def _workouts_in(repo: Repository, start: date, end: date) -> list[dict[str, Any]]:
    rows = repo.conn.execute(
        "SELECT id, type, start_ts, duration_s, distance_m, calories, avg_hr, max_hr, train_load "
        "FROM workout WHERE date(start_ts, 'unixepoch', '+8 hours') BETWEEN ? AND ? "
        "AND quality_flag = 'ok' ORDER BY start_ts DESC",
        (start.isoformat(), end.isoformat()),
    ).fetchall()
    return [dict(r) for r in rows]


def _avg(values: list[float]) -> float | None:
    clean = [v for v in values if v is not None]
    return round(sum(clean) / len(clean), 1) if clean else None


def _sum(values: list[float]) -> float | None:
    clean = [v for v in values if v is not None]
    return round(sum(clean), 1) if clean else None


def _as_int(value: float | None) -> int | None:
    return int(value) if value is not None else None


def _series_for(repo: Repository, p: Period) -> dict[str, Any]:
    """一个周期的逐日序列（图表用）。"""
    days = [p.start + timedelta(days=i) for i in range(p.days)]
    act = _activity_by_day(repo, p.start, p.end)
    sleep = _sleep_by_day(repo, p.start, p.end)
    hr = _daily_hr_agg(repo, p.start, p.end)
    rhr = _rhr_by_day(repo, p.start, p.end)
    return {
        "labels": [d.strftime("%m-%d") for d in days],
        "dates": [d.isoformat() for d in days],
        "steps": [act.get(d.isoformat(), {}).get("steps") for d in days],
        "calories": [act.get(d.isoformat(), {}).get("calories") for d in days],
        "active_min": [hr.get(d.isoformat(), {}).get("active_min") for d in days],
        "vigorous_min": [hr.get(d.isoformat(), {}).get("vigorous_min") for d in days],
        "sleep_min": [sleep.get(d.isoformat(), {}).get("total_min") for d in days],
        "sleep_deep": [sleep.get(d.isoformat(), {}).get("deep_min") for d in days],
        "sleep_light": [sleep.get(d.isoformat(), {}).get("light_min") for d in days],
        "sleep_rem": [sleep.get(d.isoformat(), {}).get("rem_min") for d in days],
        "nap_min": [sleep.get(d.isoformat(), {}).get("nap_min") for d in days],
        "rhr": [rhr.get(d.isoformat()) for d in days],
        "hr_avg": [hr.get(d.isoformat(), {}).get("hr_avg") for d in days],
    }


def build_period_stats(
    repo: Repository,
    kind: str,
    ref: date | None = None,
    *,
    as_of: date | None = None,
) -> dict[str, Any]:
    """构建周期统计。

    `as_of` 仅用于固定“今天”的业务日期，便于测试进行中周期；线上默认取北京时间今天。
    """
    period = resolve_period(kind, ref)
    prev = period.previous

    def metrics(p: Period) -> dict[str, Any]:
        act = _activity_by_day(repo, p.start, p.end)
        sleep = _sleep_by_day(repo, p.start, p.end)
        hr = _daily_hr_agg(repo, p.start, p.end)
        rhr = _rhr_by_day(repo, p.start, p.end)
        workouts = _workouts_in(repo, p.start, p.end)

        days = [p.start + timedelta(days=i) for i in range(p.days)]
        steps_vals = [act.get(d.isoformat(), {}).get("steps") for d in days]
        cal_vals = [act.get(d.isoformat(), {}).get("calories") for d in days]
        active_vals = [hr.get(d.isoformat(), {}).get("active_min") for d in days]
        vig_vals = [hr.get(d.isoformat(), {}).get("vigorous_min") for d in days]
        sleep_vals = [sleep.get(d.isoformat(), {}).get("total_min") for d in days]
        rhr_vals = [rhr.get(d.isoformat()) for d in days]

        # 有数据的天数（步数或睡眠任一有值）
        covered = sum(
            1 for d in days
            if act.get(d.isoformat(), {}).get("steps") is not None
            or sleep.get(d.isoformat(), {}).get("total_min") is not None
        )

        return {
            "days": p.days,
            "days_with_data": covered,
            # 卡路里
            "calories_total": _sum(cal_vals),
            "calories_avg": _avg(cal_vals),
            "activity_days": sum(v is not None for v in steps_vals),
            # 步数
            "steps_total": _as_int(_sum(steps_vals)),
            "steps_avg": _avg(steps_vals),
            # 心率阈值分钟：仅代表有正常心率采样的分钟，不等同全天运动时长
            "active_min_total": _as_int(_sum(active_vals)),
            "active_min_avg": _avg(active_vals),
            "heart_rate_days": sum(v is not None for v in active_vals),
            "heart_rate_observed_min": _as_int(
                _sum([hr.get(d.isoformat(), {}).get("samples") for d in days])
            ),
            # 高心率分钟
            "vigorous_min_total": _as_int(_sum(vig_vals)),
            "vigorous_min_avg": _avg(vig_vals),
            # 睡眠
            "sleep_min_total": _as_int(_sum(sleep_vals)),
            "sleep_min_avg": _avg(sleep_vals),
            "sleep_days": sum(v is not None for v in sleep_vals),
            "nap_min_total": _as_int(
                _sum([sleep.get(d.isoformat(), {}).get("nap_min") for d in days])
            ),
            "sleep_efficiency_avg": _avg([sleep.get(d.isoformat(), {}).get("efficiency") for d in days]),
            "deep_min_total": _as_int(
                _sum([sleep.get(d.isoformat(), {}).get("deep_min") for d in days])
            ),
            "rem_min_total": _as_int(
                _sum([sleep.get(d.isoformat(), {}).get("rem_min") for d in days])
            ),
            "sleep_stage_days": sum(
                sleep.get(d.isoformat(), {}).get("has_stage") == 1 for d in days
            ),
            "sleep_rem_days": sum(
                sleep.get(d.isoformat(), {}).get("has_rem") == 1 for d in days
            ),
            # 心率
            "rhr_avg": _avg(rhr_vals),
            "hr_avg": _avg([hr.get(d.isoformat(), {}).get("hr_avg") for d in days]),
            "hr_max": max((hr.get(d.isoformat(), {}).get("hr_max") or 0) for d in days) or None,
            # 运动
            "workout_count": len(workouts),
            "workout_min_total": (
                round(sum(w["duration_s"] or 0 for w in workouts) / 60) if workouts else None
            ),
            "workout_distance_m": _sum([w["distance_m"] for w in workouts]),
            "workout_calories": _sum([w["calories"] for w in workouts]),
            "active_days": len(
                {
                    datetime.fromtimestamp(w["start_ts"], tz=CN_TZ).date()
                    for w in workouts
                }
            ),
        }

    today = as_of or datetime.now(tz=CN_TZ).date()
    effective_end = min(period.end, today) if period.start <= today <= period.end else period.end
    is_partial = effective_end < period.end
    elapsed_days = (effective_end - period.start).days + 1 if is_partial else period.days

    current_period = Period(period.kind, period.start, effective_end) if is_partial else period
    if is_partial:
        previous_period = Period(
            prev.kind,
            prev.start,
            min(prev.start + timedelta(days=elapsed_days - 1), prev.end),
        )
    else:
        previous_period = prev

    current = metrics(current_period)
    previous = metrics(previous_period)

    # 返回自然周期长度，同时给出实际比较进度；当前未结束周期只和上一期等长天数比较。
    current["days"] = period.days
    current["elapsed_days"] = elapsed_days
    current["is_partial"] = is_partial
    previous["days"] = prev.days
    previous["elapsed_days"] = previous_period.days

    # 逐日序列（本期 + 上期，前端对比用；避免前端逐日发请求）
    series = _series_for(repo, period)
    # 进行中的周/月只返回上一期相同进度的对比序列，避免图中后半段完整上期数据
    # 与本期尚未到来的日期并排，视觉上继续造成不公平比较。
    series_prev = _series_for(repo, previous_period)

    result: dict[str, Any] = {
        **period.as_dict(),
        "current": current,
        "previous": previous,
        "previous_range": {
            **prev.as_dict(),
            "comparison_end": previous_period.end.isoformat(),
        },
        "as_of": effective_end.isoformat(),
        "is_partial": is_partial,
        "elapsed_days": elapsed_days,
        "series": series,
        "series_prev": series_prev,
    }

    # 周 / 月报独有：每晚的睡眠分期分段，供前端画「每晚时间轴」。
    # 过滤口径与日览 /api/summary 的 sleep_stages 一致（丢弃零长度与时间倒置的脏段），
    # 只按请求区间返回，不引入新的路由。
    if kind in {"weekly", "monthly"}:
        rows = repo.conn.execute(
            "SELECT date, stage, start_ts, end_ts, minutes FROM sleep_stage "
            "WHERE date BETWEEN ? AND ? AND end_ts > start_ts ORDER BY date, start_ts",
            (period.start.isoformat(), period.end.isoformat()),
        ).fetchall()
        sleep_stages: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            sleep_stages.setdefault(row["date"], []).append(
                {
                    "stage": row["stage"],
                    "start_ts": row["start_ts"],
                    "end_ts": row["end_ts"],
                    "minutes": row["minutes"],
                }
            )
        result["sleep_stages"] = sleep_stages

    # 日报独有：当天分钟级心率曲线
    if kind == "daily":
        rows = repo.conn.execute(
            """
            WITH per_min AS (
              SELECT CAST(ts / 60 AS INTEGER) AS minute, MAX(bpm) AS bpm
              FROM heart_rate_sample
              WHERE date(ts, 'unixepoch', '+8 hours') = ?
                AND quality_flag = 'ok' AND bpm IS NOT NULL
              GROUP BY minute
            )
            SELECT
              CAST(strftime('%H', minute * 60, 'unixepoch', '+8 hours') AS INTEGER) * 60
              + CAST(strftime('%M', minute * 60, 'unixepoch', '+8 hours') AS INTEGER)
              AS minute_of_day,
              bpm
            FROM per_min ORDER BY minute
            """,
            (period.start.isoformat(),),
        ).fetchall()
        # 固定为北京时间 00:00-23:59 的 1440 个分钟位。未采样分钟保留 null，
        # 让前端显示完整全天时间轴，而不是把首尾采样误画成连续的全天数据。
        bpm_by_minute = {
            r["minute_of_day"]: r["bpm"]
            for r in rows
            if 0 <= r["minute_of_day"] < 1440
        }
        result["hr_curve"] = {
            "labels": [f"{minute // 60:02d}:{minute % 60:02d}" for minute in range(1440)],
            "bpm": [bpm_by_minute.get(minute) for minute in range(1440)],
            "sample_count": len(bpm_by_minute),
            "range": ["00:00", "23:59"],
        }
        result["workouts"] = _workouts_in(repo, period.start, period.end)

    return result
