-- mi-health-hub 数据库结构
-- 原则：
--   1. 事实表只存云端原样数据 + quality_flag，不做加工（分析层产出另存 metrics_daily）
--   2. 指标表可以整体 DROP 后重算，事实表不行
--   3. 所有写入走 UPSERT（小米会回溯修正历史数据）

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;
PRAGMA synchronous = NORMAL;

-- ============================================================ 元数据
CREATE TABLE IF NOT EXISTS schema_version (
  version    INTEGER PRIMARY KEY,
  applied_at INTEGER NOT NULL,
  note       TEXT
);

-- 原始响应归档索引（内容在 raw/ 目录，这里是索引，便于按需重放）
CREATE TABLE IF NOT EXISTS raw_response (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  key_name     TEXT NOT NULL,
  range_start  INTEGER NOT NULL,
  range_end    INTEGER NOT NULL,
  fetched_at   INTEGER NOT NULL,
  file_path    TEXT NOT NULL,
  sha256       TEXT NOT NULL,
  record_count INTEGER NOT NULL DEFAULT 0,
  UNIQUE (key_name, range_start, range_end, sha256)
);
CREATE INDEX IF NOT EXISTS idx_raw_key_range ON raw_response (key_name, range_start, range_end);

-- 每个 key 的同步游标
CREATE TABLE IF NOT EXISTS sync_state (
  key_name         TEXT PRIMARY KEY,
  cursor_ts        INTEGER,
  last_success_at  INTEGER,
  last_attempt_at  INTEGER,
  last_error       TEXT,
  consecutive_fail INTEGER NOT NULL DEFAULT 0
);

-- 每次同步运行留痕，排障用
CREATE TABLE IF NOT EXISTS sync_run (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  tier          TEXT NOT NULL,
  started_at    INTEGER NOT NULL,
  finished_at   INTEGER,
  ok_keys       TEXT,
  failed_keys   TEXT,
  request_count INTEGER NOT NULL DEFAULT 0,
  status        TEXT NOT NULL DEFAULT 'running',
  note          TEXT
);
CREATE INDEX IF NOT EXISTS idx_sync_run_started ON sync_run (started_at DESC);

-- ============================================================ 事实表
CREATE TABLE IF NOT EXISTS daily_activity (
  date         TEXT PRIMARY KEY,
  steps        INTEGER,
  distance_m   REAL,
  calories     REAL,
  active_min   INTEGER,          -- 预留：中高强度活动分钟
  standing_cnt INTEGER,          -- 预留：有效站立次数
  quality_flag TEXT NOT NULL DEFAULT 'ok',
  source       TEXT NOT NULL DEFAULT 'cloud',
  updated_at   INTEGER
);

CREATE TABLE IF NOT EXISTS heart_rate_sample (
  ts           INTEGER NOT NULL,
  bpm          INTEGER,
  type         INTEGER NOT NULL DEFAULT 0,   -- 0 自动 / 1 手动 / 2 运动
  quality_flag TEXT NOT NULL DEFAULT 'ok',
  PRIMARY KEY (ts, type)
);
CREATE INDEX IF NOT EXISTS idx_hr_ts ON heart_rate_sample (ts);

CREATE TABLE IF NOT EXISTS resting_hr (
  date         TEXT PRIMARY KEY,
  bpm          INTEGER,
  quality_flag TEXT NOT NULL DEFAULT 'ok',
  source       TEXT NOT NULL DEFAULT 'cloud',
  updated_at   INTEGER
);

CREATE TABLE IF NOT EXISTS sleep_session (
  date         TEXT PRIMARY KEY,   -- 归属日期 = 起床日
  start_ts     INTEGER NOT NULL,
  end_ts       INTEGER NOT NULL,
  total_min    INTEGER,            -- 实际睡眠 = 深睡 + 浅睡 + REM（不含清醒）
  in_bed_min   INTEGER,            -- 卧床时长（云端 duration 字段，单位分钟）
  deep_min     INTEGER,
  light_min    INTEGER,
  rem_min      INTEGER,
  awake_min    INTEGER,
  nap_min      INTEGER NOT NULL DEFAULT 0,  -- 当日小睡合计，不混入主睡眠
  efficiency   REAL,               -- 比例（云端给的是百分数，已除以 100）
  score        INTEGER,
  is_nap       INTEGER NOT NULL DEFAULT 0,
  avg_hr       INTEGER,            -- 睡眠期间平均心率
  min_hr       INTEGER,
  max_hr       INTEGER,
  avg_spo2     REAL,
  min_spo2     INTEGER,            -- 睡眠期间最低血氧（约 70% 的记录才有）
  max_spo2     INTEGER,
  avg_breath   REAL,               -- 睡眠呼吸率 次/分（约 18% 的记录才有）
  awake_cnt    INTEGER NOT NULL DEFAULT 0,  -- 夜间觉醒次数
  has_stage    INTEGER NOT NULL DEFAULT 0,  -- 云端是否提供了分期数据
  has_rem      INTEGER NOT NULL DEFAULT 0,  -- 云端是否识别出 REM
  alg_version  INTEGER,            -- 云端睡眠算法版本
  quality_flag TEXT NOT NULL DEFAULT 'ok',
  source       TEXT NOT NULL DEFAULT 'cloud',
  updated_at   INTEGER
);
CREATE INDEX IF NOT EXISTS idx_sleep_date ON sleep_session (date);

CREATE TABLE IF NOT EXISTS sleep_stage (
  date     TEXT NOT NULL,
  stage    TEXT NOT NULL,          -- deep / light / rem / awake
  start_ts INTEGER NOT NULL,
  end_ts   INTEGER NOT NULL,
  minutes  INTEGER NOT NULL,
  PRIMARY KEY (date, start_ts, stage)
);
CREATE INDEX IF NOT EXISTS idx_sleep_stage_date ON sleep_stage (date);

CREATE TABLE IF NOT EXISTS spo2_sample (
  ts           INTEGER PRIMARY KEY,
  spo2         INTEGER,
  quality_flag TEXT NOT NULL DEFAULT 'ok'
);

CREATE TABLE IF NOT EXISTS stress_sample (
  ts           INTEGER PRIMARY KEY,
  stress       INTEGER,
  quality_flag TEXT NOT NULL DEFAULT 'ok'
);

CREATE TABLE IF NOT EXISTS weight_record (
  date         TEXT PRIMARY KEY,
  weight_kg    REAL,
  bmi          REAL,
  body_fat_pct REAL,
  muscle_kg    REAL,
  water_pct    REAL,
  quality_flag TEXT NOT NULL DEFAULT 'ok',
  updated_at   INTEGER
);

CREATE TABLE IF NOT EXISTS workout (
  id             TEXT PRIMARY KEY,
  type           TEXT,
  type_cn        TEXT,
  start_ts       INTEGER NOT NULL,
  end_ts         INTEGER NOT NULL,
  duration_s     INTEGER,
  distance_m     REAL,
  calories       REAL,
  total_cal      REAL,             -- 总消耗（含基础代谢），与 calories（活动消耗）不同
  avg_hr         INTEGER,
  max_hr         INTEGER,
  min_hr         INTEGER,
  -- 配速：单位随运动类型变化，用 pace_basis 标明（swimming 是 100m，其余是 km）
  avg_pace_s     REAL,
  max_pace_s     REAL,
  pace_basis     TEXT,
  steps          INTEGER,
  -- 训练负荷（云端已算好）
  train_effect   REAL,
  anaerobic_train_effect REAL,
  train_load     REAL,
  recover_time_h INTEGER,          -- 建议恢复时长（小时）
  vitality       REAL,
  -- 心率区间时长（秒）
  zone_warmup_s     INTEGER,
  zone_fat_burn_s   INTEGER,
  zone_aerobic_s    INTEGER,
  zone_anaerobic_s  INTEGER,
  zone_extreme_s    INTEGER,
  -- 游泳专项
  stroke_count      INTEGER,       -- 划水次数
  avg_stroke_freq   REAL,
  max_stroke_freq   REAL,
  turn_count        INTEGER,       -- 转身次数
  avg_swolf         REAL,          -- SWOLF 均值（游泳效率）
  best_swolf        REAL,
  quality_flag   TEXT NOT NULL DEFAULT 'ok',
  raw_json       TEXT,
  source         TEXT NOT NULL DEFAULT 'cloud',
  updated_at     INTEGER
);
CREATE INDEX IF NOT EXISTS idx_workout_start ON workout (start_ts DESC);
CREATE INDEX IF NOT EXISTS idx_workout_type ON workout (type);

-- 数据覆盖度：前端「哪天缺数据」提示的依据
CREATE TABLE IF NOT EXISTS data_coverage (
  date         TEXT NOT NULL,
  key_name     TEXT NOT NULL,
  sample_count INTEGER NOT NULL DEFAULT 0,
  first_ts     INTEGER,
  last_ts      INTEGER,
  updated_at   INTEGER,
  PRIMARY KEY (date, key_name)
);

-- ============================================================ 分析层产出（可整体重算）
CREATE TABLE IF NOT EXISTS metrics_daily (
  date              TEXT PRIMARY KEY,
  sleep_score       REAL,
  sleep_total_min   INTEGER,
  sleep_efficiency  REAL,
  sleep_regularity  REAL,
  sleep_debt_min    INTEGER,
  rhr_bpm           INTEGER,
  rhr_baseline_bpm  REAL,
  rhr_delta_bpm     REAL,
  steps             INTEGER,
  training_load     REAL,
  recovery_index    REAL,
  spo2_min          INTEGER,
  stress_avg        REAL,
  flags_json        TEXT,
  computed_at       INTEGER
);

CREATE TABLE IF NOT EXISTS anomaly_event (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  date         TEXT NOT NULL,
  ts           INTEGER,
  kind         TEXT NOT NULL,
  severity     TEXT NOT NULL DEFAULT 'info',
  message      TEXT,
  payload_json TEXT,
  detected_at  INTEGER,
  UNIQUE (date, kind, ts)
);
CREATE INDEX IF NOT EXISTS idx_anomaly_date ON anomaly_event (date DESC);

CREATE TABLE IF NOT EXISTS report (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  period_type  TEXT NOT NULL,      -- daily / weekly / monthly
  period_start TEXT NOT NULL,
  period_end   TEXT NOT NULL,
  generated_at INTEGER,
  path         TEXT,
  summary_json TEXT,
  UNIQUE (period_type, period_start, period_end)
);

-- AI 每日解读缓存：避免每次刷新都重打模型（一天只生成一次）
CREATE TABLE IF NOT EXISTS ai_daily_summary (
  date           TEXT PRIMARY KEY,    -- 归属日期
  summary_json   TEXT NOT NULL,       -- 模型返回的 JSON 文本
  model          TEXT NOT NULL,       -- 生成时用的模型
  prompt_version TEXT NOT NULL,       -- 生成时用的提示词版本
  config_fingerprint TEXT,            -- 协议/地址/模型/生成偏好的配置指纹（不含密钥）
  input_fingerprint TEXT,             -- 分析输入指纹（不含 payload 构建时间）
  generated_at   INTEGER NOT NULL
);

INSERT OR IGNORE INTO schema_version (version, applied_at, note)
VALUES (1, CAST(strftime('%s', 'now') AS INTEGER), 'initial schema');

INSERT OR IGNORE INTO schema_version (version, applied_at, note)
VALUES (2, CAST(strftime('%s', 'now') AS INTEGER), 'sleep_session: 增加 avg_hr/min_hr/max_hr/avg_spo2/awake_cnt/has_stage');

INSERT OR IGNORE INTO schema_version (version, applied_at, note)
VALUES (3, CAST(strftime('%s', 'now') AS INTEGER), 'workout: 增加配速/训练负荷/心率区间/游泳专项字段');

INSERT OR IGNORE INTO schema_version (version, applied_at, note)
VALUES (4, CAST(strftime('%s', 'now') AS INTEGER),
        'sleep_session: 增加 min/max_spo2、avg_breath、has_rem、alg_version；修正 in_bed_min 语义（原为实际睡眠）');

INSERT OR IGNORE INTO schema_version (version, applied_at, note)
VALUES (5, CAST(strftime('%s', 'now') AS INTEGER),
        '新增 ai_daily_summary 表：缓存 AI 每日解读，避免重复调用模型');

-- AI 对话历史：健康咨询窗口的完整往来记录（user / assistant）
CREATE TABLE IF NOT EXISTS ai_chat (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  role       TEXT NOT NULL,           -- user / assistant
  content    TEXT NOT NULL,
  created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ai_chat_created ON ai_chat (created_at);

-- 健康备注：对话里模型判定值得长期记住的主观陈述（症状/状态/生活事件），
-- 供后续日报分析与对话引用
CREATE TABLE IF NOT EXISTS health_note (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  note_date  TEXT NOT NULL,           -- 归属日期 YYYY-MM-DD
  content    TEXT NOT NULL,
  source     TEXT NOT NULL DEFAULT 'chat',  -- chat / manual
  created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_health_note_date ON health_note (note_date);

INSERT OR IGNORE INTO schema_version (version, applied_at, note)
VALUES (6, CAST(strftime('%s', 'now') AS INTEGER),
        '新增 ai_chat（对话历史）与 health_note（健康备注）表；周报/月报复用 report 表');

INSERT OR IGNORE INTO schema_version (version, applied_at, note)
VALUES (7, CAST(strftime('%s', 'now') AS INTEGER),
        'ai_daily_summary 增加 AI 配置指纹，切换协议、地址、模型或偏好后不复用旧缓存');

INSERT OR IGNORE INTO schema_version (version, applied_at, note)
VALUES (8, CAST(strftime('%s', 'now') AS INTEGER),
        'ai_daily_summary 增加分析输入指纹，数据修正或分析范围变化后不复用旧缓存');
