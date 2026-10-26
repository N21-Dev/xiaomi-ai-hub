"""云端原始记录 → 规范化行的解析器。

约定：
- 输入是 `get_fitness_data_by_time` / `get_sport_records_by_time` 的原始 item 列表
- 输出是可直接 UPSERT 的 dict 列表
- **不改数值**，只在明显不可能时打 quality_flag（`ok` / `suspect`），保留原值以便日后重算
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from .endpoints import (
    DEFAULT_SLEEP_STAGE,
    NAP_HOUR_RANGE,
    NAP_MAX_MINUTES,
    SLEEP_STAGE_FIELDS,
    SLEEP_STAGE_MAP,
)

logger = logging.getLogger(__name__)

# 质量判定阈值（与设计文档 §5 一致）
HR_MIN, HR_MAX = 25, 220
STEPS_DAILY_MAX = 100_000
SLEEP_MIN_RANGE = (60, 16 * 60)
SPO2_RANGE = (70, 100)
STRESS_RANGE = (0, 100)


class ParseValueError(ValueError):
    """云端记录的 value 不是合法对象；不得继续按 0 值写入事实表。"""


def _tz(zone_offset: int) -> timezone:
    return timezone(timedelta(seconds=int(zone_offset or 0)))


def record_dt(item: dict[str, Any]) -> datetime:
    """记录自身的时间（含 zone_offset）。"""
    ts = int(item.get("time", 0) or 0)
    return datetime.fromtimestamp(ts, tz=_tz(int(item.get("zone_offset", 0) or 0)))


def record_ts(item: dict[str, Any]) -> int:
    return int(item.get("time", 0) or 0)


def parse_value(item: dict[str, Any]) -> dict[str, Any]:
    """把云端 `value` 解析成对象。

    合法空对象仍返回 `{}`；字段存在但 JSON 损坏/类型变化时抛错，阻止同步把既有正确值
    覆盖成 0 或残缺聚合。字段完全缺失仍按空对象处理，兼容无 payload 的事件记录。
    """
    if "value" not in item or item.get("value") is None:
        return {}
    raw = item.get("value")
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError) as exc:
            raise ParseValueError("value 不是合法 JSON 对象") from exc
        if isinstance(parsed, dict):
            return parsed
        raise ParseValueError("value JSON 顶层不是对象")
    raise ParseValueError(f"value 类型不受支持：{type(raw).__name__}")


def _num(value: Any, default: float = 0.0) -> float:
    """允许空值/0 的数值转换，转换失败返回 default。"""
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _int_or_none(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        parsed = int(float(value))
    except (TypeError, ValueError):
        return None
    return None if parsed == 0 else parsed


def _float_or_none(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return None if parsed == 0 else parsed


def _number_or_none(value: Any) -> float | None:
    """严格数值转换：转换失败与缺失都返回 None，但保留合法的 0。"""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _bool_value(value: Any) -> bool:
    """兼容云端可能出现的 bool / 0、1 / 字符串布尔值。"""
    if value in (True, 1, "1", "true", "True"):
        return True
    if value in (False, 0, "0", "false", "False", None, ""):
        return False
    return False


def _first_present(payload: dict[str, Any], *keys: str) -> Any:
    """返回第一个存在（含 0 / 空串）的字段值。

    注意不能用 `a or b`：0 是合法测量值（血氧 0 表示测量失败、压力 0 是真实低值），
    用 `or` 会把它误判成缺失而整条丢弃。
    """
    for key in keys:
        if key in payload and payload[key] is not None:
            return payload[key]
    return None


def hr_quality(bpm: int | None) -> str:
    if bpm is None:
        return "suspect"
    return "ok" if HR_MIN <= bpm <= HR_MAX else "suspect"


# ---------------------------------------------------------------- 活动
def parse_daily_activity(
    steps_records: list[dict[str, Any]],
    calories_records: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """把分钟级 steps/calories 增量按本地日期聚合为每日活动。"""
    daily: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"steps": 0, "distance_m": 0.0, "calories": 0.0, "latest_ts": 0}
    )

    # 分页边界或云端修订可能重复返回同一条增量；同一记录只能聚合一次。
    def unique(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        chosen: dict[tuple[str, str, int], dict[str, Any]] = {}
        for item in records:
            identity = (
                str(item.get("sid") or ""),
                str(item.get("key") or ""),
                record_ts(item),
            )
            previous = chosen.get(identity)
            if previous is None or int(item.get("update_time", 0) or 0) >= int(
                previous.get("update_time", 0) or 0
            ):
                chosen[identity] = item
        return list(chosen.values())

    for item in unique(steps_records):
        payload = parse_value(item)
        day = record_dt(item).strftime("%Y-%m-%d")
        bucket = daily[day]
        bucket["steps"] += int(_num(payload.get("steps")))
        bucket["distance_m"] += _num(payload.get("distance"))
        bucket["calories"] += _num(payload.get("calories"))
        bucket["latest_ts"] = max(
            bucket["latest_ts"], int(item.get("update_time", 0) or record_ts(item))
        )

    # calories 单独拉一次会给出更准的活动消耗，覆盖 steps 里带的近似值
    calorie_total: dict[str, float] = defaultdict(float)
    for item in unique(calories_records):
        payload = parse_value(item)
        day = record_dt(item).strftime("%Y-%m-%d")
        calorie_total[day] += _num(payload.get("calories"))
        daily[day]["latest_ts"] = max(
            daily[day]["latest_ts"], int(item.get("update_time", 0) or record_ts(item))
        )

    rows: list[dict[str, Any]] = []
    for day, bucket in sorted(daily.items()):
        steps = int(bucket["steps"])
        if day in calorie_total:
            bucket["calories"] = calorie_total[day]
        rows.append(
            {
                "date": day,
                "steps": steps,
                "distance_m": round(bucket["distance_m"], 1),
                "calories": round(bucket["calories"], 1),
                "quality_flag": "suspect" if steps > STEPS_DAILY_MAX or steps < 0 else "ok",
                "source": "cloud",
                "updated_at": bucket["latest_ts"],
            }
        )
    return rows


# ---------------------------------------------------------------- 心率
def parse_heart_rate(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in records:
        payload = parse_value(item)
        ts = record_ts(item)
        if not ts:
            continue
        bpm = _int_or_none(payload.get("bpm"))
        rows.append(
            {
                "ts": ts,
                "bpm": bpm,
                "type": int(_num(payload.get("type"))),
                "quality_flag": hr_quality(bpm),
            }
        )
    return rows


def parse_resting_hr(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in records:
        payload = parse_value(item)
        ts = payload.get("date_time") or record_ts(item)
        if not ts:
            continue
        ts = int(_num(ts))
        day = datetime.fromtimestamp(ts, tz=_tz(int(item.get("zone_offset", 0) or 0))).strftime(
            "%Y-%m-%d"
        )
        rows.append(
            {
                "date": day,
                "bpm": _int_or_none(payload.get("bpm")),
                "quality_flag": hr_quality(_int_or_none(payload.get("bpm"))),
                "source": "cloud",
                "updated_at": record_ts(item),
            }
        )
    return rows


# ---------------------------------------------------------------- 睡眠
def _sleep_completeness(payload: dict[str, Any], duration_min: int) -> tuple[int, int]:
    """同一更新时间下的完整性兜底：时长越长、分段越多越完整。"""
    return (duration_min, len(payload.get("items") or []))


def _dedupe_sleep_sessions(records: list[dict[str, Any]]) -> list[tuple[dict, dict]]:
    """同一晚云端会上传多条**渐进**记录：sid 相同、bedtime 相同，wake 越来越晚。

    例（实测）：bedtime 00:35 → 分别出现 duration 346（wake 06:21）和 473（wake 08:31）。
    它们不是两段睡眠，是同一段睡眠的两次快照。必须去重，否则会凭空多出一段
    「7.8 小时的小睡」。

    去重键用 (sid, bedtime)。注意 **sid 不是每次睡眠的编号**（实测多晚共用同一个 sid）。
    """
    best: dict[tuple[str, int], tuple[tuple[int, int, int], dict, dict]] = {}
    for item in records:
        payload = parse_value(item)
        bed_raw = (
            payload.get("bedtime")
            or payload.get("device_bedtime")
            or payload.get("bed_timestamp")
        )
        if not bed_raw:
            continue
        bed_ts = int(_num(bed_raw))
        if not bed_ts:
            continue

        wake_raw = (
            payload.get("wake_up_time")
            or payload.get("device_wake_up_time")
            or payload.get("out_bed_timestamp")
            or record_ts(item)
        )
        wake_ts = int(_num(wake_raw)) or bed_ts
        in_bed = int(_num(payload.get("duration"))) or max(0, (wake_ts - bed_ts) // 60)

        key = (str(item.get("sid") or ""), bed_ts)
        # 云端会回溯缩短/纠正睡眠，优先选 update_time 最新版本；只有缺少该字段时
        # 才用记录时间和完整性打破平局，不能简单偏爱“更长”的旧记录。
        score = (
            int(item.get("update_time", 0) or record_ts(item)),
            *_sleep_completeness(payload, in_bed),
        )
        prev = best.get(key)
        if prev is None or score > prev[0]:
            best[key] = (score, item, payload)

    return [(item, payload) for _, item, payload in best.values()]


def parse_sleep(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """解析睡眠记录。

    口径（已用真实数据校准）：
    - `duration` = 实际睡眠时长（分钟）；`bed_duration` = 卧床时长（秒）
    - 各分期时长优先取云端字段 `sleep_{deep,light,rem,awake}_duration`（分钟），
      每个缺失字段单独退回 `items` 分段求和（此时依赖 SLEEP_STAGE_MAP）
    - `sleep_efficiency` 是**百分数**（95 表示 95%），落库时除以 100
    - 实际睡眠 = 深睡 + 浅睡 + 快速眼动（**不含清醒**）
    - 云端**没有** `is_nap` 字段；小睡靠「卧床 ≤ 180 分钟且就寝在白天」判定
    - 归属日期 = 起床日
    """
    rows: list[dict[str, Any]] = []

    for item, payload in _dedupe_sleep_sessions(records):
        zone = int(item.get("zone_offset", 0) or 0)
        bed_ts = int(
            _num(
                payload.get("bedtime")
                or payload.get("device_bedtime")
                or payload.get("bed_timestamp")
            )
        )
        wake_raw = (
            payload.get("wake_up_time")
            or payload.get("device_wake_up_time")
            or payload.get("out_bed_timestamp")
            or record_ts(item)
        )
        wake_ts = int(_num(wake_raw)) or bed_ts
        if not bed_ts or not wake_ts:
            continue

        # 注意：云端 `duration` 是**实际睡眠**（=深+浅+REM），不是卧床时长。
        # 这里只是给「无分期数据的小睡」当兜底用，真正的卧床在后面单独算。
        reported_duration = int(_num(payload.get("duration"))) or max(
            0, (wake_ts - bed_ts) // 60
        )

        # 分期：每个字段分别选择来源。云端可能只给出部分直接字段，缺少的分期
        # 必须从 items 补齐，不能因为其中一个字段存在就把其余分期误写成 0。
        stage_minutes: dict[str, int] = {}
        for stage, field in SLEEP_STAGE_FIELDS.items():
            if field in payload and payload[field] is not None:
                stage_minutes[stage] = int(_num(payload[field]))

        segments: list[dict[str, Any]] = []
        has_stage = _bool_value(payload.get("has_stage"))
        for seg in payload.get("items") or []:
            if not isinstance(seg, dict):
                continue
            seg_start = _int_or_none(seg.get("start_time"))
            seg_end = _int_or_none(seg.get("end_time"))
            if not seg_start or not seg_end:
                continue
            minutes = max(0, (int(seg_end) - int(seg_start)) // 60)
            if minutes == 0:
                continue
            stage = SLEEP_STAGE_MAP.get(int(_num(seg.get("state"), -1)), DEFAULT_SLEEP_STAGE)
            segments.append(
                {"stage": stage, "start_ts": int(seg_start), "end_ts": int(seg_end), "minutes": minutes}
            )

        # 先对 items 求和，再只补齐云端未直接提供的分期。
        segment_minutes: dict[str, int] = defaultdict(int)
        for seg in segments:
            segment_minutes[seg["stage"]] += seg["minutes"]
        for stage in ("deep", "light", "rem", "awake"):
            if stage not in stage_minutes and stage in segment_minutes:
                stage_minutes[stage] = segment_minutes[stage]

        deep = stage_minutes.get("deep", 0)
        light = stage_minutes.get("light", 0)
        rem = stage_minutes.get("rem", 0)
        awake = stage_minutes.get("awake", 0)
        asleep_min = deep + light + rem

        # 小睡常常没有分期数据（has_stage=False，各分期字段全 0），
        # 这时用云端 duration 兜底，否则整段小睡会被当成 0 分钟丢掉。质量标为 suspect。
        if asleep_min == 0 and reported_duration and not has_stage:
            asleep_min = reported_duration

        # 云端 `duration` 是**实际睡眠**（= 深+浅+REM），不是卧床时长！
        # 已用算术验证：卧床 bed_duration 24780s=413min、实际睡眠 393min
        # → 393/413 = 95.2%，与云端 sleep_efficiency=95 吻合。
        # 所以卧床要单独算，优先级：bed_duration(秒) > 由效率反推 > 入睡到醒来
        efficiency: float | None = None
        if payload.get("sleep_efficiency") is not None:
            value = _num(payload["sleep_efficiency"])
            efficiency = round(value / 100.0, 4) if value > 1 else round(value, 4)

        bed_duration_s = _num(payload.get("bed_duration"))
        if bed_duration_s > 0:
            in_bed = int(bed_duration_s // 60)
        elif efficiency and efficiency > 0 and asleep_min:
            in_bed = int(round(asleep_min / efficiency))
        else:
            in_bed = max(0, (wake_ts - bed_ts) // 60) or asleep_min + awake

        if efficiency is None and in_bed:
            efficiency = round(asleep_min / in_bed, 4)

        bed_hour = datetime.fromtimestamp(bed_ts, tz=_tz(zone)).hour
        is_nap = bool(
            in_bed <= NAP_MAX_MINUTES and NAP_HOUR_RANGE[0] <= bed_hour < NAP_HOUR_RANGE[1]
        )

        quality = "ok"
        if not is_nap and (asleep_min < SLEEP_MIN_RANGE[0] or asleep_min > SLEEP_MIN_RANGE[1]):
            quality = "suspect"
        if is_nap and not (0 < asleep_min <= NAP_MAX_MINUTES):
            quality = "suspect"
        if in_bed < asleep_min or not (0 < (efficiency or 0) <= 1):
            quality = "suspect"
        # 无分期只表示 deep/rem 不可用；若总时长、卧床和效率自洽，主睡眠总量仍可用。
        # has_stage / has_rem 单独控制分期趋势，不再把整晚总睡眠标成 suspect。

        rows.append(
            {
                "date": datetime.fromtimestamp(wake_ts, tz=_tz(zone)).strftime("%Y-%m-%d"),
                "start_ts": bed_ts,
                "end_ts": wake_ts,
                "total_min": asleep_min,
                "in_bed_min": in_bed,
                "deep_min": deep,
                "light_min": light,
                "rem_min": rem,
                "awake_min": awake,
                "nap_min": 0,
                "efficiency": efficiency,
                "score": _int_or_none(payload.get("score") or payload.get("sleep_score")),
                "is_nap": 1 if is_nap else 0,
                "avg_hr": _int_or_none(payload.get("avg_hr")),
                "min_hr": _int_or_none(payload.get("min_hr")),
                "max_hr": _int_or_none(payload.get("max_hr")),
                "avg_spo2": _float_or_none(payload.get("avg_spo2")),
                "min_spo2": _int_or_none(payload.get("min_spo2")),
                "max_spo2": _int_or_none(payload.get("max_spo2")),
                # 睡眠呼吸率（次/分）。约 18% 的记录才有，设备确实会测
                "avg_breath": _float_or_none(payload.get("avg_breath")),
                "awake_cnt": _int_or_none(payload.get("awake_count")) or 0,
                "has_stage": 1 if has_stage else 0,
                "has_rem": 1 if _bool_value(payload.get("has_rem")) else 0,
                "alg_version": _int_or_none(payload.get("sleep_algorithm_version")),
                "quality_flag": quality,
                "stages": segments,
                "source": "cloud",
                "updated_at": int(item.get("update_time", 0) or record_ts(item)),
            }
        )

    return _merge_sleep_by_date(rows)


def _merge_sleep_by_date(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """同一归属日可能有「夜间主睡眠 + 若干小睡」，合并成一行。

    组合规则：
    - 主行 = 非小睡中实际睡眠最长的一条
    - 其余 session 不静默丢弃：白天短睡与未选中的分段睡眠都累计进 `nap_min`
    - 纯小睡日当前表结构无法表达“没有主睡眠但有小睡”，保留最长记录作为承载行，
      但 `is_nap=1` 且所有 session 均计入 nap_min，前端不会把它误标为长睡眠
    """
    by_date: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_date[row["date"]].append(row)

    merged: list[dict[str, Any]] = []
    for day, group in by_date.items():
        mains = [r for r in group if not r["is_nap"]]
        naps = [r for r in group if r["is_nap"]]
        if not mains:
            carrier = max(naps, key=lambda r: r["total_min"])
            carrier = {
                **carrier,
                "nap_min": sum(r["total_min"] for r in naps),
                "is_nap": 1,
            }
            merged.append(carrier)
            continue

        main = max(mains, key=lambda r: r["total_min"])
        extra_sessions = naps + [r for r in mains if r is not main]
        if extra_sessions:
            main = {**main, "nap_min": sum(r["total_min"] for r in extra_sessions)}
        merged.append(main)

    return sorted(merged, key=lambda r: r["date"])


# ---------------------------------------------------------------- 体征
def parse_spo2(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in records:
        payload = parse_value(item)
        ts = _first_present(payload, "time") or record_ts(item)
        value = _first_present(payload, "spo2", "value")
        if not ts or value is None:
            continue
        ts = int(_num(ts))
        parsed = _number_or_none(value)
        if parsed is None:
            continue
        spo2 = int(parsed)
        rows.append(
            {
                "ts": ts,
                "spo2": spo2,
                "quality_flag": "ok" if SPO2_RANGE[0] <= spo2 <= SPO2_RANGE[1] else "suspect",
            }
        )
    return rows


def parse_stress(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in records:
        payload = parse_value(item)
        ts = _first_present(payload, "time") or record_ts(item)
        value = _first_present(payload, "stress", "score", "value")
        if not ts or value is None:
            continue
        ts = int(_num(ts))
        parsed = _number_or_none(value)
        if parsed is None:
            continue
        stress = int(parsed)
        rows.append(
            {
                "ts": ts,
                "stress": stress,
                "quality_flag": "ok" if STRESS_RANGE[0] <= stress <= STRESS_RANGE[1] else "suspect",
            }
        )
    return rows


def parse_weight(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in records:
        payload = parse_value(item)
        day = record_dt(item).strftime("%Y-%m-%d")
        rows.append(
            {
                "date": day,
                "weight_kg": _float_or_none(payload.get("weight")),
                "bmi": _float_or_none(payload.get("bmi")),
                "body_fat_pct": _float_or_none(payload.get("body_fat_rate")),
                "muscle_kg": _float_or_none(payload.get("muscle_rate")),
                "water_pct": _float_or_none(payload.get("moisture_rate")),
                "quality_flag": "ok",
                "updated_at": record_ts(item),
            }
        )
    return rows


# ---------------------------------------------------------------- 运动
# 云端 `avg_pace` / `max_pace` 的单位**随运动类型变化**（已用 duration÷distance 实测反推）：
#   swimming                    → 秒/100 米
#   walking / running / hiking / climbing → 秒/公里
# 猜错会让配速差 10 倍，所以原始值照存、另记一个 basis 字段交给前端格式化。
PACE_BASIS_100M = "100m"
PACE_BASIS_KM = "km"
WATER_SPORTS = {
    "swimming",
    "pool_swimming",
    "open_water_swimming",
    "swim",
    "diving",
}

# 心率区间时长字段（秒）
HR_ZONE_FIELDS = {
    "zone_warmup_s": "hrm_warm_up_duration",
    "zone_fat_burn_s": "hrm_fat_burning_duration",
    "zone_aerobic_s": "hrm_aerobic_duration",
    "zone_anaerobic_s": "hrm_anaerobic_duration",
    "zone_extreme_s": "hrm_extreme_duration",
}

# 训练负荷类字段
TRAINING_FIELDS = {
    "train_effect": ("train_effect", _float_or_none),
    "anaerobic_train_effect": ("anaerobic_train_effect", _float_or_none),
    "train_load": ("train_load", _float_or_none),
    "recover_time_h": ("recover_time", _int_or_none),
    "vitality": ("vitality", _float_or_none),
    "total_cal": ("total_cal", _float_or_none),
    "min_hr": ("min_hrm", _int_or_none),
    "stroke_count": ("stroke_count", _int_or_none),
    "avg_stroke_freq": ("avg_stroke_frequency", _float_or_none),
    "max_stroke_freq": ("max_stroke_freq", _float_or_none),
    "turn_count": ("turn_count", _int_or_none),
    "avg_swolf": ("avg_swolf", _float_or_none),
    "best_swolf": ("best_swolf", _float_or_none),
}


def pace_basis_for(activity_type: str) -> str:
    return PACE_BASIS_100M if activity_type in WATER_SPORTS else PACE_BASIS_KM


def workout_extras(payload: dict[str, Any], activity_type: str = "") -> dict[str, Any]:
    """从运动记录的 payload 里抽取训练负荷 / 心率区间 / 游泳专项字段。

    单独抽成函数是为了能**离线重解析**：库里已存的 `raw_json` 就能算出这些字段，
    解析逻辑改进后不必重新去云端拉取（`mi-health reparse-workouts`）。
    """
    extras: dict[str, Any] = {"pace_basis": pace_basis_for(activity_type)}
    for column, (field, cast) in TRAINING_FIELDS.items():
        extras[column] = cast(payload.get(field))
    for column, field in HR_ZONE_FIELDS.items():
        value = _number_or_none(payload.get(field))
        extras[column] = int(value) if value is not None else None
    extras["avg_pace_s"] = _float_or_none(payload.get("avg_pace"))
    extras["max_pace_s"] = _float_or_none(payload.get("max_pace"))
    return extras


def parse_workouts(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in records:
        payload = parse_value(item)
        zone = int(item.get("zone_offset", 0) or 0)
        start_ts = payload.get("start_time") or record_ts(item)
        end_ts = payload.get("end_time")
        duration_s = int(_num(payload.get("duration")))
        if not start_ts:
            continue
        start_ts = int(_num(start_ts))
        if not end_ts:
            end_ts = start_ts + duration_s
        end_ts = int(_num(end_ts))
        if duration_s <= 0:
            duration_s = max(0, end_ts - start_ts)

        activity_type = str(
            item.get("category") or item.get("key") or payload.get("sport_type") or "workout"
        )
        workout_key = f"{item.get('sid', '')}_{item.get('key', 'workout')}_{record_ts(item)}"

        row: dict[str, Any] = {
            "id": workout_key,
            "type": activity_type,
            "start_ts": start_ts,
            "end_ts": end_ts,
            "duration_s": duration_s,
            "distance_m": _float_or_none(payload.get("distance")),
            # calories 是活动消耗，total_cal 含基础代谢，二者不能互相兜底。
            "calories": _float_or_none(payload.get("calories")),
            "avg_hr": _int_or_none(payload.get("avg_hrm")),
            "max_hr": _int_or_none(payload.get("max_hrm")),
            "steps": _int_or_none(_first_present(payload, "steps", "total_steps")),
            "quality_flag": "suspect" if duration_s <= 0 else "ok",
            "raw_json": json.dumps(payload, ensure_ascii=False),
            "source": "cloud",
            "updated_at": record_ts(item),
            **workout_extras(payload, activity_type),
        }
        rows.append(row)
    return rows


def parse_abnormal_hr(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in records:
        payload = parse_value(item)
        start_ts = payload.get("start_time") or record_ts(item)
        if not start_ts:
            continue
        start_ts = int(_num(start_ts))
        end_ts = int(_num(payload.get("end_time") or start_ts))
        day = datetime.fromtimestamp(start_ts, tz=_tz(int(item.get("zone_offset", 0) or 0))).strftime(
            "%Y-%m-%d"
        )
        rows.append(
            {
                "date": day,
                "ts": start_ts,
                "kind": "abnormal_heart_beat",
                "severity": "warn",
                "message": "云端记录到异常心跳事件",
                "payload_json": json.dumps(
                    {"start_ts": start_ts, "end_ts": end_ts, "duration_s": end_ts - start_ts},
                    ensure_ascii=False,
                ),
            }
        )
    return rows


# ---------------------------------------------------------------- 调度表
PARSERS = {
    "steps": None,       # 与 calories 合并处理
    "calories": None,
    "heart_rate": parse_heart_rate,
    "resting_heart_rate": parse_resting_hr,
    "sleep": parse_sleep,
    "spo2": parse_spo2,
    "stress": parse_stress,
    "weight": parse_weight,
    "abnormal_heart_beat": parse_abnormal_hr,
}
