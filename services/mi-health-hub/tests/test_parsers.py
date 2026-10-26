"""解析器测试。用合成的云端响应形状驱动，覆盖边界与质量标记。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from mi_health_hub.cloud import parsers

CN = timezone(timedelta(hours=8))


def ts(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> int:
    return int(datetime(year, month, day, hour, minute, tzinfo=CN).timestamp())


def item(epoch: int, value: dict | str, zone_offset: int = 28800, **extra) -> dict:
    payload = value if isinstance(value, str) else json.dumps(value)
    base = {"time": epoch, "zone_offset": zone_offset, "value": payload}
    base.update(extra)
    return base


# ---------------------------------------------------------------- 活动
def test_daily_activity_aggregates_intraday_records_by_local_date():
    records = [
        item(ts(2026, 9, 15, 8, 0), {"steps": 100, "distance": 70, "calories": 4}),
        item(ts(2026, 9, 15, 9, 0), {"steps": 250, "distance": 180, "calories": 9}),
        item(ts(2026, 9, 16, 8, 0), {"steps": 50, "distance": 30, "calories": 2}),
    ]
    rows = parsers.parse_daily_activity(records, [])
    assert [row["date"] for row in rows] == ["2026-09-15", "2026-09-16"]
    assert rows[0]["steps"] == 350
    assert rows[0]["distance_m"] == 250.0
    assert rows[0]["quality_flag"] == "ok"


def test_calories_records_override_steps_embedded_calories():
    steps_records = [item(ts(2026, 9, 15, 8), {"steps": 100, "distance": 70, "calories": 4})]
    calorie_records = [
        item(ts(2026, 9, 15, 8), {"calories": 11}),
        item(ts(2026, 9, 15, 9), {"calories": 13}),
    ]
    rows = parsers.parse_daily_activity(steps_records, calorie_records)
    assert rows[0]["calories"] == 24.0


def test_absurd_step_count_is_flagged_not_dropped():
    records = [item(ts(2026, 9, 15, 8), {"steps": 250_000})]
    rows = parsers.parse_daily_activity(records, [])
    assert rows[0]["steps"] == 250_000
    assert rows[0]["quality_flag"] == "suspect"


def test_parse_value_accepts_objects_but_rejects_corrupt_payloads():
    assert parsers.parse_value({"value": {"a": 1}}) == {"a": 1}
    assert parsers.parse_value({"value": '{"a": 1}'}) == {"a": 1}
    assert parsers.parse_value({}) == {}
    with pytest.raises(parsers.ParseValueError, match="合法 JSON"):
        parsers.parse_value({"value": "not json"})
    with pytest.raises(parsers.ParseValueError, match="顶层不是对象"):
        parsers.parse_value({"value": "[1, 2]"})


# ---------------------------------------------------------------- 心率
def test_heart_rate_quality_flags_out_of_range():
    records = [
        item(ts(2026, 9, 15, 8), {"bpm": 62, "type": 0}),
        item(ts(2026, 9, 15, 9), {"bpm": 300, "type": 0}),
        item(ts(2026, 9, 15, 10), {"bpm": 0, "type": 1}),
    ]
    rows = parsers.parse_heart_rate(records)
    assert [row["quality_flag"] for row in rows] == ["ok", "suspect", "suspect"]
    assert rows[1]["bpm"] == 300  # 原值保留，不做删除或截断


def test_resting_hr_maps_to_local_date():
    records = [item(ts(2026, 9, 15, 6, 30), {"bpm": 54})]
    rows = parsers.parse_resting_hr(records)
    assert rows[0]["date"] == "2026-09-15"
    assert rows[0]["bpm"] == 54
    assert rows[0]["quality_flag"] == "ok"


# ---------------------------------------------------------------- 睡眠
# 真实字段形态（来自实际云端响应，已用于校准口径）：
#   duration=393（卧床，分钟）、sleep_efficiency=95（百分数）、
#   sleep_deep_duration / sleep_light_duration / sleep_rem_duration / sleep_awake_duration
#   items 里 state: 2=深睡 3=浅睡 4=快速眼动 5=清醒
#   **没有 is_nap 字段**；同一晚会有多条渐进记录（同 sid + 同 bedtime）
NIGHT_BED = ts(2026, 10, 31, 0, 8)
NIGHT_WAKE = ts(2026, 10, 31, 6, 46)


def night_payload(**overrides) -> dict:
    payload = {
        "bedtime": NIGHT_BED,
        "device_bedtime": NIGHT_BED,
        "wake_up_time": NIGHT_WAKE,
        "duration": 393,
        "bed_duration": 24780,          # 秒，不要直接用
        "sleep_efficiency": 95,         # 百分数
        "sleep_deep_duration": 107,
        "sleep_light_duration": 203,
        "sleep_rem_duration": 83,
        "sleep_awake_duration": 5,
        "awake_count": 3,
        "has_stage": True,
        "has_rem": True,
        "avg_hr": 56,
        "min_hr": 47,
        "max_hr": 105,
        "avg_spo2": 96,
        "items": [
            {"state": 3, "start_time": NIGHT_BED, "end_time": NIGHT_BED + 203 * 60},
            {"state": 2, "start_time": NIGHT_BED + 203 * 60, "end_time": NIGHT_BED + 310 * 60},
            {"state": 4, "start_time": NIGHT_BED + 310 * 60, "end_time": NIGHT_BED + 393 * 60},
            {"state": 5, "start_time": NIGHT_BED + 393 * 60, "end_time": NIGHT_BED + 398 * 60},
        ],
    }
    payload.update(overrides)
    return payload


def sleep_item(payload: dict, epoch: int = NIGHT_WAKE, sid: str = "2072878787") -> dict:
    return item(epoch, payload, sid=sid)


def nap_payload() -> dict:
    bed = ts(2026, 10, 31, 13, 30)
    return {
        "bedtime": bed,
        "wake_up_time": ts(2026, 10, 31, 13, 50),
        "duration": 20,
        "sleep_efficiency": 95,
        "sleep_deep_duration": 0,
        "sleep_light_duration": 0,
        "sleep_rem_duration": 0,
        "sleep_awake_duration": 0,
        "awake_count": 0,
        "has_stage": False,
        "items": [],
    }


def test_sleep_belongs_to_wake_up_date():
    rows = parsers.parse_sleep([sleep_item(night_payload())])
    assert len(rows) == 1
    assert rows[0]["date"] == "2026-10-31"


def test_sleep_uses_cloud_stage_durations():
    """优先用云端给的分期时长（与 items 求和吻合）。"""
    row = parsers.parse_sleep([sleep_item(night_payload())])[0]
    assert row["deep_min"] == 107
    assert row["light_min"] == 203
    assert row["rem_min"] == 83
    assert row["awake_min"] == 5
    assert row["total_min"] == 393          # 深+浅+REM，不含清醒
    assert row["efficiency"] == 0.95        # 百分数已转成比例
    assert row["avg_hr"] == 56 and row["min_hr"] == 47 and row["avg_spo2"] == 96.0
    assert row["awake_cnt"] == 3
    assert row["has_stage"] == 1


def test_in_bed_comes_from_bed_duration_not_from_duration_field():
    """回归：云端 `duration` 是**实际睡眠**（=深+浅+REM），不是卧床时长。

    实测反推：卧床 bed_duration 24780s = 413min，实际睡眠 393min
    → 393/413 = 95.2%，与云端 sleep_efficiency=95 吻合。
    早期代码把 duration 当卧床存入 in_bed_min，导致 in_bed == total、效率恒为 100%。
    """
    row = parsers.parse_sleep([sleep_item(night_payload())])[0]
    assert row["in_bed_min"] == 413        # 24780 秒 ÷ 60
    assert row["total_min"] == 393
    assert row["in_bed_min"] > row["total_min"]
    assert abs(row["total_min"] / row["in_bed_min"] - row["efficiency"]) < 0.01


def test_in_bed_derived_from_efficiency_when_bed_duration_missing():
    """没有 bed_duration 时，用「实际睡眠 ÷ 效率」反推卧床时长。"""
    payload = night_payload()
    payload.pop("bed_duration")
    payload["sleep_efficiency"] = 90
    row = parsers.parse_sleep([sleep_item(payload)])[0]
    assert row["in_bed_min"] == int(round(393 / 0.90))  # 437
    assert row["efficiency"] == 0.9


def test_sleep_extra_fields_are_extracted():
    """min/max_spo2、睡眠呼吸率、has_rem、算法版本都要存下来。"""
    payload = night_payload(avg_breath=15.2, min_spo2=90, max_spo2=99, has_rem=True,
                            sleep_algorithm_version=5)
    row = parsers.parse_sleep([sleep_item(payload)])[0]
    assert row["avg_breath"] == 15.2
    assert row["min_spo2"] == 90
    assert row["max_spo2"] == 99
    assert row["has_rem"] == 1
    assert row["alg_version"] == 5


def test_partial_cloud_stage_fields_are_filled_from_segments():
    payload = night_payload()
    payload.pop("sleep_deep_duration")
    payload.pop("sleep_rem_duration")
    row = parsers.parse_sleep([sleep_item(payload)])[0]
    assert row["deep_min"] == 107
    assert row["light_min"] == 203
    assert row["rem_min"] == 83
    assert row["total_min"] == 393


def test_sleep_stage_mapping_matches_cloud_when_cloud_fields_absent():
    """回归：state 映射错了会把深睡算成浅睡、REM 算成清醒。

    这里刻意去掉云端分期字段，强制走 items 求和路径，验证映射表。
    """
    payload = night_payload()
    for field in (
        "sleep_deep_duration",
        "sleep_light_duration",
        "sleep_rem_duration",
        "sleep_awake_duration",
    ):
        payload.pop(field)

    row = parsers.parse_sleep([sleep_item(payload)])[0]
    # 期望值与上面「云端字段」用例完全相同，说明映射与云端口径一致
    assert row["deep_min"] == 107, "state 2 应为深睡"
    assert row["light_min"] == 203, "state 3 应为浅睡"
    assert row["rem_min"] == 83, "state 4 应为快速眼动"
    assert row["awake_min"] == 5, "state 5 应为清醒"


def test_newer_sleep_revision_wins_even_when_it_is_shorter():
    older = sleep_item(night_payload(), epoch=NIGHT_WAKE)
    older["update_time"] = NIGHT_WAKE + 60
    corrected = night_payload(
        wake_up_time=ts(2026, 10, 31, 6, 0),
        duration=345,
        bed_duration=22200,
        sleep_deep_duration=90,
        sleep_light_duration=190,
        sleep_rem_duration=65,
        sleep_awake_duration=25,
    )
    newer = sleep_item(corrected, epoch=ts(2026, 10, 31, 6, 0))
    newer["update_time"] = NIGHT_WAKE + 600

    row = parsers.parse_sleep([older, newer])[0]
    assert row["total_min"] == 345
    assert row["updated_at"] == NIGHT_WAKE + 600


def test_progressive_records_for_same_night_are_deduplicated():
    """回归：同一晚多次上传（同 bedtime，wake 越来越晚）不是多段睡眠。

    不去重的话会凭空多出一段「7.8 小时的小睡」。
    """
    early = night_payload(wake_up_time=ts(2026, 10, 31, 4, 6), duration=234,
                          sleep_deep_duration=60, sleep_light_duration=150,
                          sleep_rem_duration=24, sleep_awake_duration=0)
    final = night_payload()  # 更完整
    rows = parsers.parse_sleep(
        [sleep_item(early, epoch=ts(2026, 10, 31, 4, 6)),
         sleep_item(final, epoch=NIGHT_WAKE)]
    )
    assert len(rows) == 1, "同一晚被当成了多段睡眠"
    assert rows[0]["total_min"] == 393   # 保留最完整的那条
    assert rows[0]["nap_min"] == 0
    assert rows[0]["is_nap"] == 0


def test_daytime_short_session_is_treated_as_nap():
    """云端没有 is_nap 字段，小睡靠「卧床短 + 白天」判定。"""
    rows = parsers.parse_sleep([sleep_item(night_payload()), sleep_item(nap_payload())])
    assert len(rows) == 1
    assert rows[0]["nap_min"] == 20        # 小睡单独累加
    assert rows[0]["total_min"] == 393     # 未混入主睡眠
    assert rows[0]["deep_min"] == 107


def test_night_long_session_is_never_a_nap():
    rows = parsers.parse_sleep(
        [sleep_item(night_payload(bedtime=ts(2026, 10, 31, 13, 0),
                                  device_bedtime=ts(2026, 10, 31, 13, 0),
                                  wake_up_time=ts(2026, 10, 31, 21, 0),
                                  duration=480))]
    )
    assert rows[0]["is_nap"] == 0, "卧床 480 分钟不该被判成小睡"


def test_stageless_nap_keeps_duration_without_becoming_bad_data():
    rows = parsers.parse_sleep([sleep_item(nap_payload())])
    assert rows[0]["total_min"] == 20      # 各分期全 0，退回云端实际睡眠时长
    assert rows[0]["nap_min"] == 20
    assert rows[0]["is_nap"] == 1
    assert rows[0]["has_stage"] == 0
    assert rows[0]["quality_flag"] == "ok"


def test_sleep_without_stage_keeps_valid_total_but_marks_stage_unavailable():
    payload = {
        "bedtime": ts(2026, 10, 30, 23, 0),
        "wake_up_time": ts(2026, 10, 31, 7, 0),
        "duration": 480,
    }
    row = parsers.parse_sleep([sleep_item(payload, epoch=ts(2026, 10, 31, 7, 0))])[0]
    assert row["quality_flag"] == "ok"
    assert row["total_min"] == 480
    assert row["has_stage"] == 0
    assert row["deep_min"] == 0 and row["rem_min"] == 0


def test_cloud_says_no_stage_data_keeps_total_but_not_stage_trends():
    """老数据 has_stage=False 时总睡眠仍可用，分期由 has_stage 单独判为不可用。"""
    payload = night_payload(has_stage=False)
    for field in (
        "sleep_deep_duration",
        "sleep_light_duration",
        "sleep_rem_duration",
        "sleep_awake_duration",
    ):
        payload.pop(field)
    payload["items"] = [
        {"state": 3, "start_time": NIGHT_BED, "end_time": NIGHT_BED + 300 * 60},
        {"state": 5, "start_time": NIGHT_BED + 300 * 60, "end_time": NIGHT_BED + 330 * 60},
    ]
    row = parsers.parse_sleep([sleep_item(payload)])[0]
    assert row["has_stage"] == 0
    assert row["deep_min"] == 0
    assert row["quality_flag"] == "ok"
    assert row["total_min"] == 300  # 只有浅睡计入实际睡眠
    assert row["awake_min"] == 30


def test_too_short_sleep_is_flagged():
    payload = {
        "bedtime": ts(2026, 10, 31, 2, 0),
        "wake_up_time": ts(2026, 10, 31, 2, 20),
        "duration": 20,
        "has_stage": False,
    }
    rows = parsers.parse_sleep([sleep_item(payload, epoch=ts(2026, 10, 31, 2, 20))])
    assert rows[0]["quality_flag"] == "suspect"


# ---------------------------------------------------------------- 体征
def test_spo2_outside_physiological_range_is_flagged():
    rows = parsers.parse_spo2(
        [
            item(ts(2026, 9, 15, 8), {"time": ts(2026, 9, 15, 8), "spo2": 97}),
            item(ts(2026, 9, 15, 9), {"time": ts(2026, 9, 15, 9), "spo2": 0}),
            item(ts(2026, 9, 15, 10), {"time": ts(2026, 9, 15, 10), "spo2": 105}),
        ]
    )
    assert [row["quality_flag"] for row in rows] == ["ok", "suspect", "suspect"]


def test_stress_parsed_from_various_field_names_and_keeps_zero():
    rows = parsers.parse_stress(
        [
            item(ts(2026, 9, 15, 8), {"time": ts(2026, 9, 15, 8), "stress": 45}),
            item(ts(2026, 9, 15, 9), {"time": ts(2026, 9, 15, 9), "score": 30}),
            item(ts(2026, 9, 15, 10), {"time": ts(2026, 9, 15, 10), "stress": 0}),
            item(ts(2026, 9, 15, 11), {"time": ts(2026, 9, 15, 11), "value": 101}),
        ]
    )
    assert [row["stress"] for row in rows] == [45, 30, 0, 101]
    assert [row["quality_flag"] for row in rows] == ["ok", "ok", "ok", "suspect"]


def test_weight_optional_fields_become_none_not_zero():
    rows = parsers.parse_weight([item(ts(2026, 9, 15, 7), {"weight": 70.5, "bmi": 24.2, "body_fat_rate": 0})])
    assert rows[0]["weight_kg"] == 70.5
    assert rows[0]["body_fat_pct"] is None  # 0 视为缺失，避免污染统计


# ---------------------------------------------------------------- 运动
def test_workout_parsed_with_duration_fallback():
    start = ts(2026, 9, 15, 19)
    records = [
        item(
            start,
            {"start_time": start, "end_time": start + 1800, "distance": 5000, "avg_hrm": 142, "max_hrm": 168},
            key="outdoor_running",
            category="outdoor_running",
        )
    ]
    rows = parsers.parse_workouts(records)
    assert rows[0]["duration_s"] == 1800
    assert rows[0]["avg_hr"] == 142
    assert rows[0]["type"] == "outdoor_running"
    assert json.loads(rows[0]["raw_json"])["distance"] == 5000


def test_pace_unit_differs_between_swimming_and_running():
    """回归：配速单位随类型变化，猜错会差 10 倍。

    实测反推：swimming 是秒/100 米，walking/running 等是秒/公里。
    """
    start = ts(2026, 9, 19, 10, 45)
    swim = parsers.parse_workouts([
        item(start, {"start_time": start, "duration": 3234, "distance": 650, "avg_pace": 497},
             key="pool_swimming", category="swimming")
    ])[0]
    run = parsers.parse_workouts([
        item(start, {"start_time": start, "duration": 472, "distance": 774, "avg_pace": 609},
             key="outdoor_running", category="running")
    ])[0]

    assert swim["pace_basis"] == "100m"
    assert run["pace_basis"] == "km"
    # 原始值照存不换算，交给前端按 basis 格式化
    assert swim["avg_pace_s"] == 497.0
    assert run["avg_pace_s"] == 609.0


def test_swim_specific_fields_are_extracted():
    """游泳特有的字段（划水、SWOLF、转身、心率区间）以前被整批丢掉。"""
    start = ts(2026, 9, 19, 10, 45)
    payload = {
        "start_time": start,
        "end_time": start + 3234,
        "duration": 3234,
        "distance": 650,
        "calories": 203,
        "total_cal": 222,
        "avg_hrm": 121,
        "max_hrm": 163,
        "min_hrm": 88,
        "avg_pace": 497,
        "max_pace": 124,
        "stroke_count": 254,
        "avg_stroke_frequency": 4,
        "max_stroke_freq": 22,
        "turn_count": 13,
        "avg_swolf": 260,
        "best_swolf": 85,
        "train_effect": 2.2,
        "anaerobic_train_effect": 1.2,
        "train_load": 28,
        "recover_time": 12,
        "vitality": 22,
        "hrm_warm_up_duration": 1518,
        "hrm_fat_burning_duration": 924,
        "hrm_aerobic_duration": 340,
        "hrm_anaerobic_duration": 0,
        "hrm_extreme_duration": 0,
    }
    row = parsers.parse_workouts([
        item(start, payload, key="pool_swimming", category="swimming")
    ])[0]

    assert row["stroke_count"] == 254
    assert row["turn_count"] == 13
    assert row["avg_swolf"] == 260.0
    assert row["best_swolf"] == 85.0
    assert row["min_hr"] == 88
    assert row["total_cal"] == 222.0
    assert row["train_effect"] == 2.2
    assert row["anaerobic_train_effect"] == 1.2
    assert row["train_load"] == 28.0
    assert row["recover_time_h"] == 12
    assert row["zone_warmup_s"] == 1518
    assert row["zone_fat_burn_s"] == 924
    assert row["zone_aerobic_s"] == 340
    # 0 是合法值（该区间没有时间），不能被当成缺失
    assert row["zone_anaerobic_s"] is None or row["zone_anaerobic_s"] == 0


def test_workout_extras_can_be_recomputed_from_stored_payload():
    """离线重解析：只靠 payload 就能算出扩展字段，不必重新拉云端。"""
    payload = {"duration": 1800, "distance": 5000, "avg_pace": 360, "train_load": 40,
               "hrm_warm_up_duration": 300, "stroke_count": None}
    extras = parsers.workout_extras(payload, "running")
    assert extras["pace_basis"] == "km"
    assert extras["avg_pace_s"] == 360.0
    assert extras["train_load"] == 40.0
    assert extras["zone_warmup_s"] == 300
    assert extras["stroke_count"] is None


def test_abnormal_hr_becomes_anomaly_event():
    start = ts(2026, 9, 15, 3)
    rows = parsers.parse_abnormal_hr([item(start, {"start_time": start, "end_time": start + 120})])
    assert rows[0]["kind"] == "abnormal_heart_beat"
    assert rows[0]["date"] == "2026-09-15"
    assert rows[0]["severity"] == "warn"
