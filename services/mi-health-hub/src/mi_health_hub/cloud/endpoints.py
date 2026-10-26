"""接口与数据 key 的集中定义。

小米改版时**只需要改这个文件**。原始响应已全量归档，改完解析器即可对历史重放。
"""

from __future__ import annotations

from dataclasses import dataclass

# ---------------------------------------------------------------- 登录
ACCOUNT_HOST = "https://account.xiaomi.com"
LOGIN_PATH = "/pass/serviceLogin"
LOGIN_SID = "miothealth"

# ---------------------------------------------------------------- 健康数据
CN_HOST = "https://hlth.io.mi.com"

PATH_FITNESS_BY_TIME = "/app/v1/data/get_fitness_data_by_time"
PATH_LATEST_FITNESS = "/app/v1/data/get_latest_fitness_data"
PATH_SPORT_RECORDS_BY_TIME = "/app/v1/data/get_sport_records_by_time"

# 已知区域。cn 用裸域名，其余用 {region}.hlth.io.mi.com
KNOWN_REGIONS = ("cn", "ru", "de", "i2", "sg", "us")


def host_for_region(region: str) -> str:
    region = (region or "cn").strip().lower()
    if region in ("", "cn"):
        return CN_HOST
    return f"https://{region}.hlth.io.mi.com"


# ---------------------------------------------------------------- 数据 key 规格
@dataclass(frozen=True)
class KeySpec:
    key: str
    kind: str
    table: str
    description: str


# kind 取值：
#   intraday_accum  云端按分钟/小时给的增量记录，需按日累加
#   sample          逐点采样，直接落采样表
#   daily           每天一条
#   session         一次完整的睡眠记录
#   event           事件型记录
KEY_SPECS: dict[str, KeySpec] = {
    "steps": KeySpec("steps", "intraday_accum", "daily_activity", "步数明细"),
    "calories": KeySpec("calories", "intraday_accum", "daily_activity", "卡路里明细"),
    "heart_rate": KeySpec("heart_rate", "sample", "heart_rate_sample", "心率采样"),
    "resting_heart_rate": KeySpec("resting_heart_rate", "daily", "resting_hr", "静息心率"),
    "sleep": KeySpec("sleep", "session", "sleep_session", "睡眠记录"),
    "spo2": KeySpec("spo2", "sample", "spo2_sample", "血氧采样"),
    "stress": KeySpec("stress", "sample", "stress_sample", "压力采样"),
    "weight": KeySpec("weight", "daily", "weight_record", "体重/身体成分"),
    "abnormal_heart_beat": KeySpec("abnormal_heart_beat", "event", "anomaly_event", "异常心跳"),
}

# 实际同步的 key 顺序。steps 放最前，因为哨兵探测也用它。
SYNC_KEYS: tuple[str, ...] = (
    "steps",
    "calories",
    "heart_rate",
    "resting_heart_rate",
    "sleep",
    "spo2",
    "stress",
    "weight",
    "abnormal_heart_beat",
)

# 设备可能不支持 / 账号可能为空的 key，用于 probe 时提示而不是报错
OPTIONAL_KEYS: tuple[str, ...] = (
    "spo2",
    "stress",
    "weight",
    "abnormal_heart_beat",
    # 红米 Watch 6 无 HRV 硬件，以下 key 接口存在但永远无数据，仅用于验证
    "hrv",
    "hrv_rmssd",
    "hrv_sdnn",
)

# ---------------------------------------------------------------- 睡眠分期映射
# ⚠️ 已用真实数据校准（2026-09-19，对照云端返回的 sleep_*_duration 与 items 分段求和）：
#      state 2 → 深睡，3 → 浅睡，4 → 快速眼动，5 → 清醒
#    三组样本的 items 求和与云端字段完全吻合，例：
#      items {3:203, 2:107, 4:83, 5:5}
#      cloud  light=203,  deep=107,  rem=83,  awake=5
#    之前的映射（1:deep, 2:light, 4:awake, 5:rem）是错的，会把深睡算成浅睡、
#    快速眼动算成清醒，导致 deep/rem 恒为 0、awake 虚高。
SLEEP_STAGE_MAP: dict[int, str] = {
    2: "deep",
    3: "light",
    4: "rem",
    5: "awake",
}
DEFAULT_SLEEP_STAGE = "unknown"

# 云端为每个分期直接给出的时长字段（单位：分钟）。
# 优先用这些字段而不是自行累加 items —— 不受映射表影响，也不怕分段缺失。
SLEEP_STAGE_FIELDS: dict[str, str] = {
    "deep": "sleep_deep_duration",
    "light": "sleep_light_duration",
    "rem": "sleep_rem_duration",
    "awake": "sleep_awake_duration",
}

STAGE_CN: dict[str, str] = {
    "deep": "深睡",
    "light": "浅睡",
    "rem": "快速眼动",
    "awake": "清醒",
    "unknown": "未知",
}

# 判定小睡：卧床时长不超过该值（分钟），且就寝时间落在白天时段
NAP_MAX_MINUTES = 180
NAP_HOUR_RANGE = (9, 21)

# ---------------------------------------------------------------- 业务错误码
AUTH_ERROR_CODES = {401, 403, -6, -10001}
AUTH_ERROR_MARKERS = (
    "authentication failed",
    "invalid credential",
    "invalid pass token",
    "invalid passtoken",
    "login required",
    "not logged in",
    "session expired",
    "unauthorized",
)


def is_auth_error(code: object, message: str) -> bool:
    try:
        if int(code) in AUTH_ERROR_CODES:  # type: ignore[arg-type]
            return True
    except (TypeError, ValueError):
        pass
    lowered = (message or "").casefold()
    return any(marker in lowered for marker in AUTH_ERROR_MARKERS)
