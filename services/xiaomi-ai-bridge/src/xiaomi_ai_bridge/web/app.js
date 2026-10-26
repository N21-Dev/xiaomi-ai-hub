const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];
const panelCsrfValue = document.querySelector('meta[name="panel-csrf"]')?.content || "";
const panelCsrfToken = panelCsrfValue.startsWith("__") ? "" : panelCsrfValue;
let panelRedirecting = false;

function requirePanelLogin() {
  if (panelRedirecting) return;
  panelRedirecting = true;
  hubStream?.close();
  window.location.replace(`/login?next=${encodeURIComponent("/" + window.location.hash)}`);
}

let currentStatus = null;
let currentConfig = null;
let currentHome = null;
let currentHub = null;
let currentHubSelection = "";
let toastTimer = null;
let loginPollTimer = null;
let loginTarget = "micoapi";
let hubStream = null;
let hubStreamRetryTimer = null;
let hubStreamRetrySeconds = 0;
let hubHistoryRequest = 0;
let refreshPromise = null;
let hubFilterKind = "all";
let hubFilterSearch = "";
let hubFilterRoom = "all";
let hubFilterPurpose = "all";
let hubHistory = [];
let hubHistoryHours = 24;
let hubHistoryKey = "";
let hubHistoryResource = "";
let hubHistoryLoading = false;
let hubHistoryError = "";
let hubHistoryQuery = {};
let hubHistoryScope = "";
let hubHistoryOlderLoaded = false;
let hubHistoryLatestStamp = "";
let hubHistoryRevision = null;
let hubHistoryNotice = "";
let hubStorage = null;
let hubStorageLoadedAt = 0;
let hubStoragePromise = null;
let hubStorageError = "";
let hubStreamConnected = false;
let hubSnapshotFailed = false;
let hubSamplePromise = null;
let homeSelectedKey = "";
let homeCategory = "all";
let homeDetailSignature = "";
const homePending = new Set();
const homeMessages = new Map();
const homeDrafts = new Map();
const homeCapabilitySections = new Map();
const homeExtensions = new Map();
const homeExtensionLoads = new Map();
const homeAcCommands = new Map();
let homeExtensionSignature = "";
let currentHomeLayout = null;
let homeLayoutPromise = null;
let homeRoomFilter = "all";
const hubFieldFilters = new Map();
const hubContentGroups = new Map();
let currentHealthSource = null;
let healthSelectedDate = "";
let healthDays = 7;
let healthLoading = false;
let healthStarted = false;
let healthRequestSequence = 0;
const healthSnapshots = new Map();

const hubKinds = { sensor: "传感器", agent: "智能助理", service: "服务", system: "系统", device: "家用设备" };
const stateLabels = {
  on: "电源", mode: "运行模式", "fan-level": "风速档位", status: "运行状态",
  current_task: "当前任务", last_task: "最近任务", model: "模型", task_count: "任务数量", "air-quality": "空气质量等级",
  total_tasks: "任务总数", running_tasks: "运行中任务", summary: "状态摘要",
};
const valueLabels = { favorite: "最爱模式", auto: "自动模式", sleep: "睡眠模式", silent: "静音模式", free: "自由调节", manual: "手动模式", running: "运行中", idle: "空闲", offline: "离线", online: "在线", completed: "已完成", error: "异常", paused: "已暂停" };
const metricDefinitions = [
  { key: "temperature", aliases: ["temperature_c", "temperature"], label: "温度", unit: "℃", tone: "warm" },
  { key: "humidity", aliases: ["humidity_percent", "relative-humidity", "humidity"], label: "湿度", unit: "%", tone: "blue" },
  { key: "pm25", aliases: ["pm2.5-density", "pm25", "pm2_5", "pm25_ug_m3"], label: "PM2.5", unit: "μg/m³", tone: "mint" },
  { key: "pm10", aliases: ["pm10-density", "pm10", "pm10_ug_m3"], label: "PM10", unit: "μg/m³", tone: "mint" },
  { key: "co2", aliases: ["co2-density", "co2", "co2_ppm"], label: "二氧化碳", unit: "ppm", tone: "mint" },
  { key: "aqi", aliases: ["air-quality-index", "aqi"], label: "空气质量指数", unit: "", tone: "mint" },
  { key: "filter", aliases: ["filter-life-level", "filter_life_percent"], label: "滤芯余量", unit: "%", tone: "blue" },
  { key: "brightness", aliases: ["brightness"], label: "亮度", unit: "%", tone: "warm" },
  { key: "color", aliases: ["color-temperature"], label: "色温", unit: "K", tone: "warm" },
  { key: "target_temperature", aliases: ["target-temperature"], label: "目标温度", unit: "℃", tone: "warm" },
  { key: "battery", aliases: ["battery_percent", "battery-level"], label: "电量", unit: "%", tone: "blue" },
  { key: "progress", aliases: ["progress_percent"], label: "任务进度", unit: "%", tone: "blue" },
];
const environmentKeys = new Set(["temperature", "humidity", "pm25", "pm10", "co2", "aqi"]);

function metricValue(state, metric, fields) {
  if (fields) {
    const field = findMetricField(fields, metric);
    const status = field?.read_status?.status || field?.status;
    if (field?.sensitive || status && status !== "ok") return null;
  }
  if (metric.path) {
    const raw = stateFieldValue(state, metric);
    return typeof raw === "number" && Number.isFinite(raw) ? raw : null;
  }
  for (const alias of metric.aliases) {
    const raw = state?.[alias];
    if (typeof raw !== "number" && !(typeof raw === "string" && raw.trim() !== "")) continue;
    const value = Number(raw);
    if (Number.isFinite(value)) return value;
  }
  return null;
}

function findMetricField(fields, metric) {
  return (fields || []).find((item) => metric.path ? JSON.stringify(item.path || [item.key]) === JSON.stringify(metric.path) : item.key === metric.key || metric.aliases.includes(item.key));
}

function historyMetricPoints(events, metric) {
  const points = new Map();
  events.forEach((event) => {
    const hasMetadata = Array.isArray(event.fields) && event.fields.length > 0;
    const field = findMetricField(event.fields, metric);
    if (hasMetadata && !field || field?.cached || field?.read_status?.cached || field?.source_sample_cached || field?.read_status?.source_sample_cached) return;
    const diagnostic = field?.read_status || field;
    const status = diagnostic?.status;
    const failed = status && status !== "ok";
    const timestamp = failed ? diagnostic.attempted_at || event.observed_at || event.received_at : hasMetadata ? diagnostic?.observed_at || field?.observed_at : event.observed_at || event.received_at;
    if (!timestamp) return;
    const at = new Date(timestamp).getTime();
    if (!Number.isFinite(at)) return;
    const value = failed ? null : metricValue(event.state, metric, event.fields);
    points.set(at, { at, value });
  });
  return [...points.values()].sort((a, b) => a.at - b.at);
}

function stateFieldValue(state, field) {
  const path = field.path || [field.key];
  return path.reduce((value, part) => value != null && typeof value === "object" && Object.prototype.hasOwnProperty.call(value, part) ? value[part] : undefined, state);
}

function fieldType(field, state) {
  if (field.type && !["unknown", "null"].includes(field.type)) return field.type;
  if (/^(u?int\d*|float|double|number)$/.test(field.format || "")) return "number";
  if (["bool", "boolean"].includes(field.format)) return "boolean";
  const value = stateFieldValue(state, field);
  return Array.isArray(value) ? "array" : value === null ? "null" : value === undefined ? "unknown" : typeof value;
}

function fieldReadStatus(field, state, resource) {
  const diagnostic = field.read_status || field;
  let status = field.sensitive ? "sensitive" : diagnostic.status;
  if (status !== "sensitive" && ["parameter", "event"].includes(field.role)) status = "not_readable";
  if (!status) status = field.readable === false ? "not_readable" : stateFieldValue(state, field) != null ? "ok" : "unknown";
  if (status === "ok" && resource && resourceStale(resource)) status = resource.online === false ? "offline" : "stale";
  const labels = { ok: "读取正常", unavailable: "读取失败", unsupported: "接口不支持", offline: "设备离线", not_readable: field.notifiable ? "仅事件或动作" : "不可直接读取", sensitive: "敏感信息", stale: "读数已过期", unknown: "尚未读取" };
  const label = status === "not_readable" && field.role === "parameter" ? "动作输入参数" : status === "not_readable" && field.role === "event" ? "事件或动作返回" : labels[status] || "等待确认";
  return { ...diagnostic, status, label };
}

function fieldSourceLabel(source) {
  return ({ "miot.cloud": "米家属性服务", "miio.cloud": "米家云服务", "xiaomi.device_list": "小米设备目录", "xiaomi.router.local": "路由器本地 · 只读", "mqtt": "MQTT 设备上报", "http": "HTTP 数据上报", "http.ingest": "HTTP 数据上报" })[source] || source || "来源未标注";
}

function metricPresentation(field, value) {
  const unit = field.unit || "";
  if (typeof value === "number" && Number.isFinite(value) && value >= 0 && ["B", "B/s", "bit/s", "bps"].includes(unit)) {
    const units = unit === "B" ? ["B", "KB", "MB", "GB"] : unit === "B/s" ? ["B/s", "KB/s", "MB/s", "GB/s"] : ["bps", "Kbps", "Mbps", "Gbps"];
    const scale = value > 0 ? Math.min(units.length - 1, Math.max(0, Math.floor(Math.log10(value) / 3))) : 0;
    return { value: displayNumber(value / (1000 ** scale)), unit: units[scale] };
  }
  if (typeof value === "number" && /(?:^|\.)uptime-seconds$|wan-uptime-seconds$/.test(field.key || "") && value >= 0) {
    const minutes = Math.floor(value / 60);
    return { value: minutes >= 1440 ? `${Math.floor(minutes / 1440)}天 ${Math.floor(minutes % 1440 / 60)}小时` : minutes >= 60 ? `${Math.floor(minutes / 60)}小时 ${minutes % 60}分` : `${Math.floor(value)}秒`, unit: "" };
  }
  return { value: displayNumber(value), unit };
}

const fieldRoleLabels = { measurement: "实时指标", counter: "数量与计数", state: "运行状态", setting: "配置参数", parameter: "操作参数", event: "事件数据", identity: "设备资料", metadata: "记录信息", collection: "结构化数据" };

function identityField(field) {
  if (field.role === "identity") return true;
  const identity = [field.path?.length > 1 ? field.path.at(-1) : field.key, field.semantic].join("/").toLowerCase();
  return /serial|(?:^|[\s._:/-])(?:id|did|uid|uuid|imei|imsi|mac|model|firmware|hardware|version)(?:$|[\s._:/-])/.test(identity);
}

function fieldRole(field, state) {
  if (fieldRoleLabels[field.role]) return field.role;
  if (identityField(field)) return "identity";
  const presentation = fieldPresentation(field, state);
  return { measurements: "measurement", usage: "counter", states: "state", settings: "setting", records: "collection", profile: "metadata" }[presentation.group] || "metadata";
}

const hubGroupLabels = { measurements: "环境与测量", usage: "用量与进度", states: "运行状态", settings: "可调设置", records: "事件与记录", profile: "设备档案" };

function fieldPresentation(field, state) {
  if (hubGroupLabels[field.presentation?.group] && field.presentation.kind) return field.presentation;
  const type = fieldType(field, state);
  const name = [field.key, field.semantic, field.label].join(" ").toLowerCase();
  const result = (group, kind, label = "") => ({ group, kind, label });
  if (identityField(field)) return result("profile", "identifier", "标识信息");
  if (/timestamp|(?:^|[\s._:/-])(?:date|updated|created|observed|received|attempted)(?:$|[\s._:/-])/.test(name)) return result("profile", "timestamp", "时间");
  if (field.role === "parameter") return result("settings", "text", "动作输入参数");
  if (field.role === "event") return result("records", "event", "事件记录");
  if (["object", "array"].includes(type)) return result("records", "collection", "记录集合");
  if (field.writable || field.role === "setting") return result("settings", type === "enum" ? "enum" : type === "boolean" ? "boolean" : "text", "当前设置");
  if (/countdown|remaining[-_ ]?time|倒计时/.test(name)) return result("states", "countdown", "倒计时");
  if (/uptime|duration|运行时长|在线时长/.test(name)) return result("states", "duration", "运行时长");
  if (type === "boolean" || type === "enum" || field.options?.length || field.role === "state" || type === "string" && (stateLabels[field.key] || /(?:^|[._-])(?:status|playback|mode|on)$/.test(field.key || "") || valueLabels[String(stateFieldValue(state, field)).toLowerCase()])) return result("states", type === "boolean" ? "boolean" : type === "enum" || field.options?.length ? "enum" : "text", "运行状态");
  if (field.role === "counter" || /count|次数|数量|总数/.test(name)) {
    const cumulative = /cumulative|累计|total[-_ ]?(?:bytes|traffic|energy)|total_tasks/.test(name);
    const total = /total|known[-_ ]?device|总数|总量/.test(name);
    return result("usage", cumulative ? "cumulative_count" : total ? "total_count" : "current_count", cumulative ? "累计用量" : total ? "数量总计" : "当前数量");
  }
  if (["%", "percentage", "percent"].includes(field.unit) && /humidity|battery|filter[-_ ]?life|progress|utilization|usage|load|湿度|电量|余量|进度|占用|使用率/.test(name)) return result(/humidity|湿度/.test(name) ? "measurements" : "usage", "percentage", "百分比");
  if (type === "number" && (field.role === "measurement" || field.unit && !["none", "unknown"].includes(field.unit) || metricDefinitions.some((metric) => metric.aliases.includes(field.key)))) return result("measurements", "number", "实测数值");
  return result("profile", "text", "资料信息");
}

function typedDuration(value, unit) {
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0) return null;
  const scale = { s: 1, sec: 1, second: 1, seconds: 1, 秒: 1, min: 60, minute: 60, minutes: 60, 分钟: 60, 分: 60, h: 3600, hour: 3600, hours: 3600, 小时: 3600, d: 86400, day: 86400, days: 86400, 天: 86400 }[String(unit || "").toLowerCase()];
  if (!scale) return null;
  let seconds = value * scale;
  const parts = [];
  [[86400, "天"], [3600, "小时"], [60, "分"]].forEach(([size, label]) => {
    const count = Math.floor(seconds / size);
    if (count) parts.push(`${count}${label}`);
    seconds %= size;
  });
  if (seconds || !parts.length) parts.push(`${displayNumber(seconds)}秒`);
  return parts.join(" ");
}

function typedTimestamp(value) {
  if (typeof value === "string") {
    if (/^\d{4}-\d{2}-\d{2}$/.test(value)) return value;
    if (!/^\d{10,13}$/.test(value)) {
      const parsed = new Date(value);
      return Number.isFinite(parsed.getTime()) ? new Intl.DateTimeFormat("zh-CN", { year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false }).format(parsed) : value;
    }
  }
  if (typeof value === "number" && Number.isFinite(value)) {
    const digits = String(Math.trunc(Math.abs(value))).length;
    if (digits !== 10 && digits !== 13) return String(value);
  }
  const numeric = typeof value === "number" ? value : Number(value);
  const date = new Date(numeric * (Math.abs(numeric) < 1e11 ? 1000 : 1));
  return Number.isFinite(date.getTime()) ? new Intl.DateTimeFormat("zh-CN", { year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false }).format(date) : String(value);
}

function typedFieldValue(field, resource) {
  const raw = stateFieldValue(resource.state, field);
  if (raw == null || fieldReadStatus(field, resource.state, resource).status === "sensitive") return displayPropertyValue(field, resource.state);
  const presentation = fieldPresentation(field, resource.state);
  if (presentation.kind === "identifier") return typeof presentation.text === "string" ? presentation.text : String(raw);
  if (["duration", "countdown"].includes(presentation.kind)) return typedDuration(raw, field.unit) || displayPropertyValue(field, resource.state);
  if (presentation.kind === "timestamp") return typedTimestamp(raw);
  if (presentation.group === "profile" && typeof raw === "number") return String(raw);
  if (typeof raw === "string" && /^[\s]*[\[{]/.test(raw)) return "结构化文本 · 已收起原始内容";
  const value = displayPropertyValue(field, resource.state);
  return typeof value === "string" && value.length > 280 ? `${value.slice(0, 280)}…` : value;
}

function normalHubFields(resource) {
  const fields = hubResourceFields(resource).filter((field) => fieldReadStatus(field, resource.state, resource).status !== "sensitive" && field.role !== "parameter" && !(field.role === "event" && stateFieldValue(resource.state, field) == null));
  const descendant = (child, parent) => {
    const a = child.path || [child.key], b = parent.path || [parent.key];
    return a.length > b.length && b.every((part, index) => String(part) === String(a[index]));
  };
  const records = fields.filter((field) => fieldPresentation(field, resource.state).kind === "collection");
  const containers = records.filter((parent) => Array.isArray(stateFieldValue(resource.state, parent)) || !fields.some((child) => descendant(child, parent) && ["measurements", "usage", "states", "settings"].includes(fieldPresentation(child, resource.state).group)));
  return fields.filter((field) => !records.includes(field) || containers.includes(field)).filter((field) => !containers.some((parent) => parent !== field && descendant(field, parent)));
}

function chartableField(field, state) {
  if (identityField(field) || ["parameter", "event"].includes(field.role) || field.sensitive || field.readable === false || fieldReadStatus(field, state).status === "sensitive") return false;
  if (Object.prototype.hasOwnProperty.call(field, "chartable")) return field.chartable === true;
  if (["identity", "metadata", "collection", "state", "setting", "event"].includes(field.role)) return false;
  if (/timestamp|(?:^|[._:/-])(?:time|date|updated|created|observed|received|attempted)(?:$|[._:/-])/.test([field.key, field.semantic].join("/").toLowerCase())) return false;
  return field.type !== "enum" && ["measurements", "usage"].includes(fieldPresentation(field, state).group);
}

function displayNumber(value) {
  return new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 2 }).format(value);
}

function displayStateValue(key, value) {
  if (key === "on") {
    if ([true, 1, "on", "true", "开启"].includes(value)) return "开启";
    if ([false, 0, "off", "false", "关闭"].includes(value)) return "关闭";
  }
  if (value == null) return "暂无读数";
  if (typeof value === "boolean") return value ? "开启" : "关闭";
  if (Array.isArray(value)) return value.length ? value.map((item) => typeof item === "object" ? "结构化数据" : displayStateValue(key, item)).join("、") : "暂无项目";
  if (typeof value === "object") return `${Object.keys(value).length} 项数据`;
  return valueLabels[String(value).toLowerCase()] || String(value);
}

function displayPropertyValue(property, state) {
  const diagnostic = fieldReadStatus(property, state);
  if (diagnostic.status === "sensitive") return "敏感信息已隐藏";
  const value = stateFieldValue(state, property);
  if (value == null && property.role === "parameter") return "执行时填写";
  if (value == null && property.role === "event") return "由事件或动作返回";
  if (value == null) return ({ unavailable: "本次未读到", unsupported: "接口不支持读取", offline: "设备离线", not_readable: property.notifiable ? "等待事件或动作返回" : "设备未提供读取", stale: "等待新读数", unknown: "尚未取得读数" })[diagnostic.status] || "暂无读数";
  if (property.presentation?.kind === "identifier" && typeof property.presentation.text === "string") return property.presentation.text;
  if (property.presentation?.kind === "timestamp") return typedTimestamp(value);
  if (identityField(property) && ["number", "string"].includes(typeof value)) return String(value);
  const option = (property.options || []).find((item) => String(item.value) === String(value));
  if (option) return option.label || String(option.value);
  if (typeof value === "number") {
    const presentation = metricPresentation(property, value);
    return `${presentation.value}${presentation.unit ? ` ${presentation.unit}` : ""}`;
  }
  if (typeof value === "string" && /^[\s]*[\[{]/.test(value)) return "结构化文本 · 已收起原始内容";
  return displayStateValue(property.key, value);
}

function stateSummary(state, properties = []) {
  const metrics = metricDefinitions.flatMap((metric) => {
    const value = metricValue(state, metric);
    return value === null ? [] : [`${metric.label} ${displayNumber(value)}${metric.unit}`];
  });
  const details = Object.entries(state || {}).filter(([key, value]) => stateLabels[key] && value != null && typeof value !== "object")
    .map(([key, value]) => `${stateLabels[key]} ${displayStateValue(key, value)}`);
  const known = new Set([...metricDefinitions.flatMap((metric) => metric.aliases), ...Object.keys(stateLabels)]);
  const extra = Object.entries(state || {}).filter(([key]) => !known.has(key)).map(([key, value]) => {
    const property = properties.find((item) => item.key === key) || { key, label: key };
    return `${property.label || key} ${displayPropertyValue(property, { [key]: value })}`;
  });
  return [...metrics, ...details, ...extra].slice(0, 10).join(" · ") || "已收到更新，暂无可展示的读数";
}

function resourceStale(resource) {
  if (resource.infrared_ac) return !resource.enabled || resource.cloud_online === false;
  return Boolean(resource.stale || resource.online === false || (resource.expires_at && new Date(resource.expires_at).getTime() <= Date.now()));
}

function appendMetricCards(container, state, { compact = false, includeDetails = true, properties = [] } = {}) {
  const available = metricDefinitions.filter((metric) => metricValue(state, metric) !== null);
  available.forEach((metric) => {
    const card = document.createElement("div");
    card.className = `metric-card ${compact ? "compact" : ""}`;
    card.dataset.tone = metric.tone;
    const label = document.createElement("span");
    label.className = "metric-label";
    label.textContent = metric.label;
    const value = document.createElement("strong");
    value.className = "metric-value";
    value.textContent = displayNumber(metricValue(state, metric));
    const unit = document.createElement("small");
    unit.className = "metric-unit";
    unit.textContent = metric.unit;
    value.append(unit);
    card.append(label, value);
    const property = properties.find((item) => metric.aliases.includes(item.key));
    if (property) appendFieldReadNote(card, property, state);
    container.append(card);
  });
  if (includeDetails) {
    Object.entries(state || {}).filter(([key, value]) => stateLabels[key] && value != null && typeof value !== "object").forEach(([key, value]) => {
      const card = document.createElement("div");
      card.className = "metric-card text-metric";
      const label = document.createElement("span");
      label.className = "metric-label";
      label.textContent = stateLabels[key];
      const display = document.createElement("strong");
      display.textContent = displayStateValue(key, value);
      card.append(label, display);
      const property = properties.find((item) => item.key === key);
      if (property) appendFieldReadNote(card, property, state);
      container.append(card);
    });
    const known = new Set([...metricDefinitions.flatMap((metric) => metric.aliases), ...Object.keys(stateLabels)]);
    Object.entries(state || {}).filter(([key]) => !known.has(key)).forEach(([key, value]) => {
      const property = properties.find((item) => item.key === key) || { key, label: key };
      const card = homeNode("div", "metric-card text-metric");
      card.append(homeNode("span", "metric-label", property.label || key), homeNode("strong", "", displayPropertyValue(property, { [key]: value })));
      appendFieldReadNote(card, property, state);
      container.append(card);
    });
  }
  if (!container.children.length) {
    const empty = document.createElement("p");
    empty.className = "hub-history-empty";
    empty.textContent = "暂无可展示的读数";
    container.append(empty);
  }
}

function appendFieldReadNote(card, field, state) {
  const diagnostic = fieldReadStatus(field, state);
  if (!["ok", "sensitive"].includes(diagnostic.status)) {
    card.append(homeNode("small", "metric-read-note", `${stateFieldValue(state, field) != null ? "上次读数 · " : ""}${diagnostic.label}`));
    card.classList.add("has-stale-field");
  }
  if (diagnostic.source_updated_at && diagnostic.status !== "sensitive") {
    const age = Number.isFinite(Number(diagnostic.source_age_seconds)) && Number(diagnostic.source_age_seconds) >= 60
      ? ` · 已过去 ${formatDuration(Number(diagnostic.source_age_seconds))}` : "";
    card.append(homeNode("small", "metric-source-note", `云端属性更新 ${formatTime(diagnostic.source_updated_at)}${age} · 实时性未核实`));
  }
}

function formatDuration(seconds) {
  if (seconds >= 86400) return `${Math.floor(seconds / 86400)}天`;
  if (seconds >= 3600) return `${Math.floor(seconds / 3600)}小时`;
  return `${Math.floor(seconds / 60)}分钟`;
}

async function request(path, options = {}) {
  const { timeoutMs = 15000, ...fetchOptions } = options;
  const externalSignal = options.signal;
  const controller = externalSignal ? null : new AbortController();
  const timeout = controller ? setTimeout(() => controller.abort(), timeoutMs) : null;
  try {
    const response = await fetch(path, {
      cache: "no-store",
      ...fetchOptions,
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", ...(options.headers || {}), ...(panelCsrfToken && options.method && options.method !== "GET" ? { "X-Panel-CSRF": panelCsrfToken } : {}) },
      signal: externalSignal || controller.signal,
    });
    const payload = await response.json().catch(() => ({}));
    if (response.status === 401 && payload.code === "panel_auth_required") requirePanelLogin();
    if (!response.ok || payload.ok === false) {
      const error = new Error(payload.message || payload.error || `请求失败 (${response.status})`);
      error.payload = payload;
      error.status = response.status;
      throw error;
    }
    return payload;
  } catch (error) {
    if (error?.name === "AbortError") throw new Error("请求超时，请稍后重试");
    if (error instanceof TypeError) throw new Error("无法连接到中枢服务");
    throw error;
  } finally {
    if (timeout) clearTimeout(timeout);
  }
}

function text(selector, value) {
  const node = $(selector);
  if (node) node.textContent = value ?? "—";
}

function setPill(selector, label, tone) {
  const node = $(selector);
  if (!node) return;
  node.textContent = label;
  node.classList.remove("ok", "bad");
  if (tone) node.classList.add(tone);
}

function setFieldValue(selector, value) {
  const node = $(selector);
  if (node && document.activeElement !== node && node.dataset.dirty !== "true") node.value = value ?? "";
}

function clearDirtyFields(form) {
  form.querySelectorAll("[data-dirty]").forEach((node) => delete node.dataset.dirty);
}

function formatTime(value) {
  if (!value) return "暂无";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "暂无";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit",
    hour12: false,
  }).format(date);
}

function showToast(message, bad = false) {
  const node = $("#toast");
  node.textContent = message;
  node.classList.toggle("bad", bad);
  node.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => node.classList.remove("show"), 3200);
}

function markPipeline(status) {
  $$(".pipe-node").forEach((node) => node.classList.remove("active", "blocked"));
  $("#pipe-bridge").classList.add("active");
  if (status.xiaomi_connected) {
    $("#pipe-speaker").classList.add("active");
    $("#pipe-cloud").classList.add("active");
  } else {
    $("#pipe-cloud").classList.add("blocked");
  }
  if (status.ai_configured) $("#pipe-ai").classList.add("active");
  else $("#pipe-ai").classList.add("blocked");
  if (status.device_selected) $("#pipe-tts").classList.add("active");
}

function renderStatus(status) {
  currentStatus = status;
  const healthy = Boolean(status.started_at);
  const audioLabels = {
    suppressing: "正在压制小爱原生回答",
    generating: "模型正在生成回答",
    handoff: "正在切换播报通道",
    speaking: "AI 正在播报",
  };
  text("#service-value", healthy ? "运行中" : "异常");
  text("#service-note", `启动 ${formatTime(status.started_at)}`);
  text("#xiaomi-value", status.xiaomi_connected ? "已连接" : "未连接");
  text("#xiaomi-note", status.xiaomi_connected ? "micoapi 会话有效" : "等待账号认证");
  text("#device-value", status.device_selected ? status.device_hardware || "已选择" : "未选择");
  text("#device-note", status.device_selected ? (audioLabels[status.audio_state] || "对话轮询已启用") : "等待云端设备列表");
  text("#ai-value", status.ai_configured ? "可用" : "未配置");
  text("#ai-note", currentConfig?.ai_model || "OpenAI 兼容接口");
  text("#continuous-value", status.continuous_mode ? "开启" : "关闭");
  text("#last-answer", formatTime(status.last_processed_at));
  text("#last-ai-latency", status.last_ai_latency_ms == null ? "暂无" : `${status.last_ai_latency_ms} ms`);
  text("#cancelled-turns", String(status.cancelled_turns || 0));
  text("#native-pauses", String(status.native_pauses || 0));
  text("#chat-ai-latency", status.last_ai_latency_ms == null ? "暂无" : `${status.last_ai_latency_ms} ms`);
  text("#diag-audio-state", audioLabels[status.audio_state] || (status.active_turn ? "处理中" : "空闲"));
  text("#diag-ai-latency", status.last_ai_latency_ms == null ? "暂无" : `${status.last_ai_latency_ms} ms`);
  text("#diag-turn-latency", status.last_turn_latency_ms == null ? "暂无" : `${status.last_turn_latency_ms} ms`);
  text("#diag-cancelled", String(status.cancelled_turns || 0));
  text("#diag-pauses", String(status.native_pauses || 0));
  text("#last-poll", status.last_poll_at ? `最近轮询 ${formatTime(status.last_poll_at)}` : "尚未轮询");

  const dot = $("#live-dot");
  dot.className = status.ready ? "ok" : status.last_error ? "bad" : "";
  text("#live-label", status.ready ? "链路就绪" : status.last_error ? "等待处理" : "服务运行中");
  const alert = $("#error-alert");
  alert.hidden = !status.last_error;
  text("#error-text", status.last_error || "");

  setPill("#ai-status-pill", status.ai_configured ? "配置有效" : "未配置", status.ai_configured ? "ok" : "bad");
  setPill("#tts-status-pill", status.device_selected ? "设备可用" : "等待设备", status.device_selected ? "ok" : "bad");
  $("#test-tts").disabled = !status.device_selected;
  markPipeline(status);
}

function renderOverviewSnapshot(hub) {
  const resources = (hub?.resources || []).filter((resource) => !resource.catalog_only);
  const fresh = resources.filter((resource) => resource.online !== false && !resource.stale);
  const stale = resources.filter((resource) => resource.stale || resource.online === false);
  const environment = resources.filter((resource) => resource.kind === "sensor" || /环境|空气|温度|湿度|净化器|传感器/.test(resource.name || ""));
  const timestamps = resources
    .map((resource) => resource.observed_at || resource.received_at)
    .map((value) => value ? new Date(value) : null)
    .filter((value) => value && !Number.isNaN(value.getTime()))
    .sort((a, b) => b - a);
  const latest = timestamps[0];
  text("#overview-online-count", String(fresh.length));
  text("#overview-stale-count", String(stale.length));
  text("#overview-environment-count", String(environment.length));
  text("#overview-last-sample", latest ? formatTime(latest.toISOString()) : "暂无");
  text("#overview-data-note", resources.length ? `${resources.length} 个数据源 · 只显示有效摘要` : "尚未发现数据源");
  text("#overview-attention-note", stale.length ? `${stale.length} 个数据源需要刷新或检查` : "没有待处理项");
  $("#overview-stale-count")?.classList.toggle("has-attention", Boolean(stale.length));
}

function renderConfig(config) {
  currentConfig = config;
  text("#summary-user", config.user_id_masked);
  text("#summary-pass", config.pass_token_set ? "已保存" : "未设置");
  text("#summary-cuser", config.c_user_id_set ? "已保存" : "未设置");
  text("#summary-service-token", config.service_token_set ? "已保存" : "未设置");
  text("#summary-hardware", config.mi_hardware || "自动选择");
  text("#credential-state", `${config.user_id_masked} · ${config.pass_token_set ? "凭据已保存" : "缺少凭据"}`);
  text("#service-ai-url", config.ai_base_url || "未配置");
  text("#service-ai-model", config.ai_model || "未配置");
  text("#service-ai-key", config.ai_api_key_set ? "已保存" : "未设置");
  setFieldValue("#mi-hardware", config.mi_hardware || "");
  setFieldValue("#mi-did", config.mi_did || "");
  setFieldValue("#mi-device-name", config.mi_device_name || "");
  setFieldValue("#behavior-keywords", (config.trigger_keywords || []).join(","));
  setFieldValue("#behavior-answer-limit", config.answer_max_chars || 240);
  setFieldValue("#behavior-timeout", config.continuous_timeout_seconds || 30);
  setFieldValue("#behavior-mute", config.mute_xiaoai === false ? "false" : "true");
  // The speaker's read-aloud cap no longer applies to console text; this row
  // now mirrors the selected conversation mode instead.
  text("#chat-answer-limit", chatContext ? "长对话 · 记住前几轮" : "短对话 · 每轮独立");
  if (currentStatus) renderStatus(currentStatus);
}

const actionLabels = {
  get_state: "读取状态",
  turn_on: "开启",
  turn_off: "关闭",
  set_brightness: "设置亮度",
  set_color_temperature: "设置色温",
  set_temperature: "设置温度",
  set_mode: "设置模式",
  set_speed: "设置风速",
};

function renderHome(home) {
  const previousDevices = new Map((currentHome?.devices || []).map((device) => [device.key, device]));
  // A slower snapshot must not overwrite a newer control readback or SSE update.
  (home.devices || []).forEach((device) => {
    if (device.extensions) acceptHomeExtension(device.key, device.extensions);
    const saved = previousDevices.get(device.key);
    if (saved?.observed_at && new Date(saved.observed_at) > new Date(device.observed_at || 0)) {
      ["state", "observed_at", "stale", "online"].forEach((field) => { device[field] = saved[field]; });
    }
  });
  currentHome = home;
  syncInfraredDeviceEntries();
  syncHomeResources();
  const authLabels = {
    idle: ["等待发现", ""],
    connected: ["已连接", "ok"],
    needs_login: ["需要授权", "bad"],
    error: ["连接异常", "bad"],
  };
  const [authText, authTone] = authLabels[home.auth_state] || [home.auth_state, "bad"];
  setPill("#home-auth-pill", authText, authTone);
  text("#home-safe-categories", [...new Set((home.devices || []).map((device) => device.category_label).filter(Boolean))].join("、") || "等待发现");
  text("#home-call-limit", `${home.max_calls_per_turn || 3} 次`);
  text("#home-discovered-at", formatTime(home.last_discovered_at));
  text("#home-device-count", `${(currentHome.devices || []).length} 台`);
  renderHomeCategories();
  renderHomeCards();
  renderHomeDetail();
  if (!currentHomeLayout && !homeLayoutPromise) loadHomeLayout();

  const list = $("#home-management-list");
  if (!list.contains(document.activeElement) && !list.querySelector('[data-dirty="true"]')) {
  list.replaceChildren();
  (home.devices || []).filter((device) => !device.infrared_ac).forEach((device) => {
    const row = document.createElement("div");
    row.className = "device-row";

    const identity = document.createElement("div");
    identity.className = "device-identity";
    const name = document.createElement("strong");
    name.textContent = device.name;
    const detail = document.createElement("span");
    detail.textContent = `${device.category_label} · ${device.model}`;
    const actions = document.createElement("small");
    actions.textContent = device.properties ? `${device.properties.length} 项属性 · ${(device.capabilities_actions || []).length} 个动作 · ${(device.capabilities_events || []).length} 类事件` : (device.actions || []).map((item) => actionLabels[item] || item).join(" / ");
    identity.append(name, detail, actions);

    const aliasLabel = document.createElement("label");
    aliasLabel.className = "device-alias";
    const aliasTitle = document.createElement("span");
    aliasTitle.textContent = "模型别名";
    const aliasInput = document.createElement("input");
    aliasInput.value = device.alias;
    aliasInput.maxLength = 32;
    aliasInput.addEventListener("input", () => { aliasInput.dataset.dirty = "true"; });
    aliasLabel.append(aliasTitle, aliasInput);

    const toggleLabel = document.createElement("label");
    toggleLabel.className = "toggle-control";
    const toggle = document.createElement("input");
    toggle.type = "checkbox";
    toggle.checked = Boolean(device.enabled);
    const toggleText = document.createElement("span");
    toggleText.textContent = device.enabled ? "已授权" : "未授权";
    toggle.addEventListener("change", () => { toggleText.textContent = toggle.checked ? "已授权" : "未授权"; toggle.dataset.dirty = "true"; });
    toggleLabel.append(toggle, toggleText);

    const save = document.createElement("button");
    save.className = "secondary-button";
    save.type = "button";
    save.textContent = "保存";
    save.addEventListener("click", async () => {
      await withBusy(save, async () => {
        await request(`/api/home/devices/${encodeURIComponent(device.key)}`, {
          method: "PUT",
          body: JSON.stringify({ alias: aliasInput.value, enabled: toggle.checked }),
        });
        clearDirtyFields(row);
        save.blur();
        showToast("设备权限已保存");
        await refreshAll(true);
      }).catch((error) => showToast(error.message, true));
    });

    row.append(identity, aliasLabel, toggleLabel, save);
    list.append(row);
  });
  }
  renderInfraredManagementEntries();

  const select = $("#home-test-device");
  const previous = select.value;
  select.replaceChildren();
  (home.devices || []).filter((item) => item.enabled && !item.infrared_ac).forEach((device) => {
    const option = document.createElement("option");
    option.value = device.alias;
    option.textContent = `${device.alias} · ${device.category_label}`;
    option.dataset.actions = JSON.stringify(["get_state"]);
    select.append(option);
  });
  if ([...select.options].some((option) => option.value === previous)) select.value = previous;
  updateHomeActionOptions();
  $("#home-test-run").disabled = select.options.length === 0;

  const audit = $("#home-audit-list");
  audit.replaceChildren();
  $("#home-audit-empty").hidden = Boolean(home.audit?.length);
  (home.audit || []).forEach((entry) => {
    const row = document.createElement("div");
    row.className = "audit-row";
    const state = document.createElement("i");
    state.className = entry.ok ? "ok" : "bad";
    const summary = document.createElement("span");
    const device = (home.devices || []).find((item) => item.alias === entry.device || item.name === entry.device);
    const capability = [...(device?.extra_controls || []), ...(device?.capabilities_actions || [])].find((item) => item.action === entry.action);
    summary.textContent = `${entry.device} · ${capability?.label || actionLabels[entry.action] || "设备操作"}`;
    const detail = document.createElement("small");
    detail.textContent = entry.detail;
    const at = document.createElement("time");
    at.textContent = formatTime(entry.at);
    row.append(state, summary, detail, at);
    audit.append(row);
  });
}

function homeNode(tag, className = "", content = "") {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (content) node.textContent = content;
  return node;
}

function homeDeviceIcon(category) {
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", "0 0 100 100");
  svg.setAttribute("fill", "none");
  svg.setAttribute("stroke", "currentColor");
  svg.setAttribute("stroke-width", "3");
  svg.setAttribute("stroke-linecap", "round");
  svg.setAttribute("stroke-linejoin", "round");
  svg.setAttribute("aria-hidden", "true");
  const specialIcons = {
    speaker: ["M32 16h36v68H32zM42 31h16M42 48h16M42 61h16M42 72h16"],
    television: ["M12 21h76v49H12zM36 83h28M43 70v13m14-13v13"],
    tv: ["M12 21h76v49H12zM36 83h28M43 70v13m14-13v13"],
    "tv-box": ["M19 45h62v28H19zM30 61h4M49 26l8-10m-6 19 17-10"],
    router: ["M19 53h62v25H19zM30 65h2m10 0h2M27 53V22m46 31V22M42 34q8-8 16 0"],
    watch: ["M36 32h28v37H36zM40 32V13h20v19M40 69v18h20V69M50 41v11h8"],
    "infrared-remote-control": ["M35 19h30v66H35zM45 31h10M43 44h3m8 0h3M43 56h3m8 0h3M43 68h3m8 0h3"],
    "ir-remote-control": ["M35 19h30v66H35zM45 31h10M43 44h3m8 0h3M43 56h3m8 0h3M43 68h3m8 0h3"],
    "remote-control": ["M35 19h30v66H35zM45 31h10M43 44h3m8 0h3M43 56h3m8 0h3M43 68h3m8 0h3"],
  };
  const paths = specialIcons[category] || (category === "light"
    ? ["M32 81h37M50 79V48L73 27M38 42l-9-12 22-14 11 13-24 13Z", "M36 45 23 57m20-8-2 15m-12-24-15 4"]
    : category === "air-purifier"
      ? ["M30 22q20-10 40 0v61q-20 9-40 0V22Z", "M30 22q20 10 40 0M43 36h14M42 51h16M42 60h16M42 69h16", "M40 9V4m10 5V2m10 7V4"]
      : category === "fan"
        ? ["M50 43c-25-35-41 8-9 10-17 29 25 42 24 7 31-4 18-44-8-19Z", "M50 68v16M36 87h28"]
        : category === "air-conditioner"
          ? ["M17 22h66v31H17zM25 43h50M68 32h7M35 64v17m15-17v23m15-23v17"]
          : ["M30 20h40v65H30zM42 40v10m16-10v10M45 66h10"]);
  paths.forEach((d) => {
    const path = document.createElementNS(ns, "path");
    path.setAttribute("d", d);
    svg.append(path);
  });
  return svg;
}

function homePower(device) {
  if (device.infrared_ac) return null;
  const raw = device.state?.on;
  if ([true, 1, "on", "true", "开启"].includes(raw)) return true;
  if ([false, 0, "off", "false", "关闭"].includes(raw)) return false;
  return null;
}

function homeResourceId(device) {
  return device.resource_id || `home.${device.category}.${device.key}`;
}

const routerPrimaryKeys = ["local.cpu-percent", "local.memory-percent", "local.wan-download-bytes-per-second", "local.wan-upload-bytes-per-second", "local.connected-device-count"];

function routerLocalResource(device) {
  const local = device?.local_source;
  if (local?.kind !== "router") return null;
  return { resource_id: homeResourceId(device), name: device.alias || device.name, state: local.state || {}, fields: local.fields || [], source: "xiaomi.router.local", observed_at: local.observed_at, online: local.available !== false, stale: local.stale === true || local.available === false };
}

function routerSummaryFields(resource, includeMissing = false) {
  const fields = resource.fields || [];
  return routerPrimaryKeys.flatMap((key) => {
    const field = fields.find((item) => item.key === key);
    return field && (includeMissing || stateFieldValue(resource.state, field) != null) ? [field] : [];
  });
}

function frequentResourceFields(resource, limit = 3) {
  const router = routerSummaryFields(resource);
  if (router.length) return router.slice(0, limit);
  return normalHubFields(resource).filter((field) => ["measurements", "usage"].includes(fieldPresentation(field, resource.state).group) && fieldPriority(field, resource.state) !== "detail" && stateFieldValue(resource.state, field) != null).slice(0, limit);
}

function homeDeviceFields(device) {
  const resource = currentHub?.resources?.find((item) => item.resource_id === homeResourceId(device));
  return resource?.fields?.length ? resource.fields : device.properties || [];
}

function deviceForResource(resource) {
  return currentHome?.devices?.find((device) => homeResourceId(device) === resource.resource_id);
}

function syncHomeResources() {
  (currentHub?.resources || []).forEach((resource) => {
    const device = deviceForResource(resource);
    if (!device) return;
    const at = resource.observed_at || resource.received_at;
    if (device.observed_at && new Date(at) < new Date(device.observed_at)) return;
    Object.assign(device, { state: resource.state, observed_at: at, stale: resourceStale(resource), online: resource.online });
  });
}

function homeUnavailable(device) {
  return !device.enabled || device.cloud_online === false || !device.observed_at || resourceStale(device) || device.busy || homePending.has(device.key);
}

function homeStatusText(device) {
  if (!device.enabled) return "未授权";
  if (device.infrared_ac) {
    if (homeDevicePending(device)) return "正在发送指令…";
    if (!device.infrared_ac.ready) return "待配置 · 选择空调协议";
    if (device.cloud_online === false) return "红外遥控器离线";
    return "红外控制 · 无状态回读";
  }
  if (homePending.has(device.key) || device.busy) return "正在确认状态…";
  if (device.local_source?.kind === "router" && device.local_source.available) return device.cloud_online === false ? "本地在线 · 米家云离线" : "本地已连接";
  if (device.cloud_online === false) return "设备离线";
  if (device.cloud_online === true && !device.observed_at) return "米家在线 · 暂无读数";
  if (device.stale) return "等待读数更新";
  if (device.online === false) return "设备离线";
  const power = homePower(device);
  return power === null ? (device.observed_at ? "已连接" : device.cloud_online === true ? "在线 · 待读取" : "待读取") : power ? "已开启" : "已关闭";
}

function renderHomeCategories() {
  const container = $("#home-category-tabs");
  if (!container) return;
  const categories = new Map([["all", "全部"]]);
  (currentHome?.devices || []).forEach((device) => categories.set(device.category, device.category_label || "其他设备"));
  if (!categories.has(homeCategory)) homeCategory = "all";
  const signature = JSON.stringify([...categories]);
  if (container.dataset.signature !== signature) {
    container.dataset.signature = signature;
    container.replaceChildren();
    categories.forEach((label, key) => {
      const button = homeNode("button", "", label);
      button.type = "button";
      button.dataset.homeCategory = key;
      container.append(button);
    });
  }
  container.querySelectorAll("button").forEach((button) => {
    const selected = button.dataset.homeCategory === homeCategory;
    button.classList.toggle("selected", selected);
    button.setAttribute("aria-pressed", String(selected));
  });
}

function homeAssignment(device) {
  return currentHomeLayout?.assignments?.[device.infrared_ac?.parent_key || device.key] || { home_key: device.home_key, room_key: device.room_key, home_name: device.home_name, room_name: device.room_name || device.room || device.location };
}

async function loadHomeLayout(refresh = false) {
  if (homeLayoutPromise) return homeLayoutPromise;
  const button = $("#home-layout-refresh");
  if (button) button.disabled = true;
  homeLayoutPromise = request(`/api/home/layout${refresh ? "/refresh" : ""}`, { ...(refresh ? { method: "POST", body: "{}" } : {}), timeoutMs: 45000 });
  try {
    currentHomeLayout = await homeLayoutPromise;
  } catch (error) {
    currentHomeLayout = { ...(currentHomeLayout || {}), available: false, message: `家庭房间暂未同步：${error.message}` };
  } finally {
    homeLayoutPromise = null;
    if (button) button.disabled = false;
    renderHomeLayout();
    renderHomeCards();
  }
}

function renderHomeLayout() {
  const select = $("#home-room-filter");
  if (!select) return;
  const options = [{ value: "all", label: "全部家庭与房间" }];
  (currentHomeLayout?.homes || []).forEach((home) => {
    options.push({ value: JSON.stringify([home.key]), label: `${home.name} · 全部房间` });
    (home.rooms || []).forEach((room) => options.push({ value: JSON.stringify([home.key, room.key]), label: `${home.name} · ${room.name}` }));
  });
  if (!options.some((option) => option.value === homeRoomFilter)) homeRoomFilter = "all";
  select.replaceChildren(...options.map((option) => new Option(option.label, option.value)));
  select.value = homeRoomFilter;
  select.disabled = options.length === 1;
  text("#home-layout-message", currentHomeLayout?.available === false ? currentHomeLayout.message || "房间信息暂不可用，仍可查看全部设备。" : "");
}

function makePowerButton(device, control, large = false) {
  const button = homeNode("button", large ? "device-power-button large" : "device-power-button");
  button.type = "button";
  button.dataset.action = "power";
  button.addEventListener("click", (event) => {
    event.stopPropagation();
    const latest = currentHome?.devices?.find((item) => item.key === device.key);
    if (!latest || homePower(latest) === null) return;
    runHomeControl(latest.key, homePower(latest) ? control.off_action : control.action);
  });
  updatePowerButton(button, device);
  return button;
}

function updatePowerButton(button, device) {
  const power = homePower(device);
  button.classList.toggle("is-on", power === true);
  button.textContent = homePending.has(device.key) || device.busy ? "…" : "⏻";
  button.disabled = homeUnavailable(device) || power === null;
  button.setAttribute("aria-label", `${power ? "关闭" : "开启"}${device.alias || device.name}`);
  button.setAttribute("aria-pressed", String(power === true));
  button.title = homeUnavailable(device) ? "请先授权并刷新设备状态" : power === null ? "请打开设备详情读取状态" : power ? "关闭设备" : "开启设备";
}

function renderHomeCards() {
  const list = $("#home-device-list");
  const focusedKey = document.activeElement?.closest(".home-device-card")?.dataset.deviceKey;
  const focusedPower = document.activeElement?.dataset.action === "power";
  list.replaceChildren();
  const devices = currentHome?.devices || [];
  const selectedRoom = homeRoomFilter === "all" ? [] : JSON.parse(homeRoomFilter);
  const visible = devices.filter((device) => {
    const assignment = homeAssignment(device);
    return (homeCategory === "all" || device.category === homeCategory) && (!selectedRoom.length || assignment.home_key === selectedRoom[0] && (!selectedRoom[1] || assignment.room_key === selectedRoom[1]));
  });
  $("#home-empty").hidden = Boolean(devices.length);
  $("#home-filter-empty").hidden = !devices.length || Boolean(visible.length);
  visible.forEach((device) => {
    const card = homeNode("article", "home-device-card");
    const local = routerLocalResource(device);
    card.dataset.deviceKey = device.key;
    card.classList.toggle("selected", device.key === homeSelectedKey);
    card.classList.toggle("is-on", homePower(device) === true && !resourceStale(device));
    card.classList.toggle("is-stale", resourceStale(local || device));
    const open = homeNode("button", "home-device-open");
    open.type = "button";
    open.setAttribute("aria-label", `打开${device.alias || device.name}控制面板`);
    open.addEventListener("click", () => openHomeDevice(device.key));
    const icon = homeNode("span", "home-device-icon");
    icon.append(homeDeviceIcon(device.category));
    const identity = homeNode("span", "home-card-identity");
    const assignment = homeAssignment(device);
    identity.append(homeNode("strong", "", device.alias || device.name), homeNode("small", "", [device.category_label, assignment.home_name, assignment.room_name].filter(Boolean).join(" · ")));
    open.append(icon, identity);
    const controls = homeNode("div", "home-card-status");
    controls.append(homeNode("span", "", homeStatusText(device)));
    if (local) controls.append(homeNode("small", `router-source-badge${local.stale ? " stale" : ""}`, local.stale ? "本地读数待更新" : "本地只读"));
    const toggle = (device.controls || []).find((control) => control.kind === "toggle" && control.off_action);
    if (toggle) controls.append(makePowerButton(device, toggle));
    const metrics = homeNode("div", "home-card-metrics");
    if (local) {
      metrics.classList.add("router-card-metrics");
      routerSummaryFields(local).slice(0, 3).forEach((field) => {
        const value = homeNode("div");
        value.dataset.routerMetric = field.key;
        value.append(homeNode("small", "", field.label), homeNode("strong", "", displayPropertyValue(field, local.state)));
        const diagnostic = fieldReadStatus(field, local.state, local);
        if (diagnostic.status !== "ok") value.append(homeNode("small", "home-card-field-note", stateFieldValue(local.state, field) != null ? "上次读数" : diagnostic.message || diagnostic.label));
        metrics.append(value);
      });
    }
    metricDefinitions.filter((metric) => !local && metricValue(device.state, metric) !== null).slice(0, 3).forEach((metric) => {
      const value = homeNode("div");
      value.append(homeNode("small", "", metric.label), homeNode("strong", "", `${displayNumber(metricValue(device.state, metric))}${metric.unit}`));
      const field = homeDeviceFields(device).find((item) => metric.aliases.includes(item.key));
      if (field && fieldReadStatus(field, device.state).status !== "ok") value.append(homeNode("small", "home-card-field-note", "上次读数"));
      metrics.append(value);
    });
    if (!metrics.children.length) {
      const properties = (device.properties || []).filter((property) => property.readable && !property.sensitive && !["identity", "metadata", "parameter", "event", "collection"].includes(fieldRole(property, device.state)) && device.state?.[property.key] != null).slice(0, 2);
      properties.forEach((property) => {
        const value = homeNode("div");
        value.append(homeNode("small", "", property.label), homeNode("strong", "", displayPropertyValue(property, device.state)));
        const field = homeDeviceFields(device).find((item) => item.key === property.key) || property;
        if (fieldReadStatus(field, device.state).status !== "ok") value.append(homeNode("small", "home-card-field-note", "上次读数"));
        metrics.append(value);
      });
    }
    if (device.infrared_ac) {
      const profile = device.ac_profile;
      if (profile) {
        const temperature = homeNode("div");
        temperature.append(homeNode("small", "", profile.settings_source === "last_sent" ? "上次发送温度" : profile.settings_source === "protocol_defaults" ? "默认目标温度" : "学习信号温度"), homeNode("strong", "", `${profile.settings.temperature}℃`));
        const mode = homeNode("div");
        mode.append(homeNode("small", "", "设置模式"), homeNode("strong", "", profile.mode_options.find(item => item.value === profile.settings.mode)?.label || "—"));
        metrics.append(temperature, mode);
      } else metrics.append(homeNode("span", "", "选择已支持的空调协议，即可生成控制指令"));
    }
    if (!metrics.children.length) metrics.append(homeNode("span", "", device.state?.mode ? displayStateValue("mode", device.state.mode) : device.properties ? `${device.properties.length} 项属性 · ${(device.capabilities_actions || []).length} 个动作` : "打开面板，查看设备状态"));
    const message = homeMessages.get(device.key);
    const note = homeNode("p", "home-card-note", message?.message || (device.infrared_ac ? `通过 ${device.infrared_ac.parent_name} 控制` : device.local_source?.message || (device.observed_at ? `更新于 ${formatTime(device.observed_at)}` : "等待读取设备")));
    note.classList.toggle("bad", Boolean(message && !["confirmed", "pending", "accepted"].includes(message.outcome)));
    card.append(open, controls, metrics, note);
    list.append(card);
  });
  if (focusedKey) {
    const card = [...list.children].find((item) => item.dataset.deviceKey === focusedKey);
    card?.querySelector(focusedPower ? '[data-action="power"]' : ".home-device-open")?.focus({ preventScroll: true });
  }
}

function controlCurrentValue(device, control) {
  const raw = device.state?.[control.property];
  if (control.kind !== "select") return raw;
  const token = String(raw ?? "").toLowerCase();
  return (control.options || []).find((option) => String(option.value).toLowerCase() === token || String(option.label).toLowerCase() === token)?.value;
}

function homeFavoriteFanControl(device) {
  if (device.category !== "air-purifier") return null;
  return (device.extra_controls || []).find((control) =>
    control.property === "favorite-fan-level" && control.kind === "range" &&
    [control.min, control.max, control.step].every(value => typeof value === "number" && Number.isFinite(value)) &&
    control.min < control.max && control.step > 0
  ) || null;
}

function homePrimaryControls(device) {
  const controls = device.controls || [];
  const favorite = homeFavoriteFanControl(device);
  return favorite && !controls.some(control => control.property === favorite.property)
    ? [...controls, {...favorite, label: "最爱风速", unit: "级"}]
    : controls;
}

function buildHomeControls(device) {
  const container = $("#home-control-list");
  container.replaceChildren();
  homePrimaryControls(device).forEach((control) => {
    const section = homeNode("div", "home-control");
    section.dataset.controlAction = control.action;
    const heading = homeNode("div", "home-control-heading");
    heading.append(homeNode("span", "", control.label), homeNode("output", "home-control-value"));
    section.append(heading);
    if (control.kind === "toggle") {
      section.classList.add("power-control");
      section.append(makePowerButton(device, control, true));
    } else if (control.kind === "range") {
      const range = homeNode("input", "home-range");
      range.type = "range";
      range.min = control.min;
      range.max = control.max;
      range.step = control.step || 1;
      range.dataset.controlInput = control.action;
      range.setAttribute("aria-label", control.label);
      range.addEventListener("input", () => {
        homeDrafts.set(`${device.key}:${control.action}`, range.value);
        section.querySelector("output").textContent = `${displayNumber(Number(range.value))}${control.unit || ""} · 待应用`;
        range.setAttribute("aria-valuetext", `${range.value}${control.unit || ""}，待应用`);
        const latest = currentHome?.devices?.find((item) => item.key === device.key);
        section.querySelector("button").disabled = !latest || (control.property === "favorite-fan-level" ? genericControlUnavailable(latest, control) : homeUnavailable(latest));
      });
      const limits = homeNode("div", "home-range-limits");
      limits.append(homeNode("span", "", `${control.min}${control.unit || ""}`), homeNode("span", "", `${control.max}${control.unit || ""}`));
      const apply = homeNode("button", "secondary-button home-apply", control.property === "favorite-fan-level" ? "应用风速" : "应用");
      apply.type = "button";
      apply.setAttribute("aria-label", `应用${control.label}`);
      apply.addEventListener("click", () => runHomeControl(device.key, control.action, Number(range.value)));
      section.append(range, limits, apply);
      if (control.property === "favorite-fan-level") {
        const note = homeNode("p", "capability-control-note");
        note.dataset.favoriteFanNote = "true";
        section.append(note);
      }
    } else if (control.kind === "select") {
      const choices = homeNode("div", "home-mode-choices");
      choices.setAttribute("role", "group");
      choices.setAttribute("aria-label", control.label);
      (control.options || []).forEach((option) => {
        const button = homeNode("button", "home-mode-choice", option.label || option.value);
        button.type = "button";
        button.dataset.value = String(option.value);
        button.addEventListener("click", () => runHomeControl(device.key, control.action, option.value));
        choices.append(button);
      });
      section.append(choices);
    } else return;
    container.append(section);
  });
  if (!container.children.length) container.append(homeNode("p", "home-history-empty", device.properties ? "更多属性、设置与动作见下方完整能力。" : "该设备目前只支持查看数据。"));
}

function genericControlUnavailable(device, control) {
  return !device.enabled || device.cloud_online === false || device.busy || homePending.has(device.key) || control.enabled === false;
}

function capabilitySection(device, key, title, open = false) {
  const section = homeNode("details", "capability-section");
  const savedKey = `${device.key}:${key}`;
  section.open = homeCapabilitySections.get(savedKey) ?? open;
  section.append(homeNode("summary", "", title));
  section.addEventListener("toggle", () => homeCapabilitySections.set(savedKey, section.open));
  return section;
}

function capabilityInput(device, control, field, isAction) {
  const wrapper = homeNode("label", "capability-input");
  const label = homeNode("span", "", field.label || control.label);
  const draftKey = field.draft_key || `${device.key}:${control.action}${isAction ? `:${field.key}` : ""}`;
  const draft = homeDrafts.get(draftKey);
  let input;
  if (field.kind === "select" || field.kind === "toggle") {
    input = homeNode("select");
    const options = field.kind === "toggle" ? [{ value: field.off_value ?? false, label: "关闭" }, { value: field.on_value ?? true, label: "开启" }] : field.options || [];
    input.append(new Option("请选择", ""));
    options.forEach((option, index) => input.append(new Option(option.label || String(option.value), String(index))));
    input.capabilityOptions = options;
  } else {
    input = homeNode("input");
    input.type = field.kind === "range" || /^(u?int\d*|float|double|number)$/.test(field.format || "") ? "number" : "text";
    if (input.type === "number") {
      if (field.min != null) input.min = field.min;
      if (field.max != null) input.max = field.max;
      input.step = field.step || "any";
      input.inputMode = "decimal";
    } else input.maxLength = field.max_length || 256;
    input.placeholder = field.unit ? `请输入数值（${field.unit}）` : "请输入";
  }
  input.required = true;
  input.dataset.capabilityInput = field.key || control.property || control.action;
  input.dataset.draftKey = draftKey;
  input.setAttribute("aria-label", field.label || control.label);
  if (draft !== undefined) input.value = draft;
  input.addEventListener("input", () => {
    homeDrafts.set(draftKey, input.value);
    updateCapabilitySlider(input);
    const latest = currentHome?.devices?.find((item) => item.key === device.key);
    const section = wrapper.closest(".capability-control");
    const submit = section?.querySelector('button[type="submit"]');
    if (submit) submit.disabled = !latest || genericControlUnavailable(latest, control) || (!isAction && input.value === "");
  });
  wrapper.append(label, input);
  if (field.kind === "range" && Number.isFinite(Number(field.min)) && Number.isFinite(Number(field.max))) {
    const slider = homeNode("input", "home-range capability-range");
    slider.type = "range";
    slider.min = field.min;
    slider.max = field.max;
    slider.step = field.step || "any";
    slider.dataset.capabilitySlider = "true";
    slider.setAttribute("aria-label", `${field.label || control.label}滑动调节`);
    const preview = homeNode("output", "capability-range-preview");
    slider.addEventListener("input", () => {
      input.value = slider.value;
      input.dispatchEvent(new Event("input", { bubbles: true }));
    });
    wrapper.insertBefore(slider, input);
    wrapper.append(preview);
    input.capabilitySlider = slider;
    input.capabilityPreview = preview;
    input.capabilityUnit = field.unit || "";
    updateCapabilitySlider(input);
  }
  if (field.min != null || field.max != null || field.unit) {
    wrapper.append(homeNode("small", "", `${field.min ?? "不限"} — ${field.max ?? "不限"}${field.unit ? ` ${field.unit}` : ""}${field.step ? ` · 步长 ${field.step}` : ""}`));
  }
  return wrapper;
}

function updateCapabilitySlider(input) {
  const slider = input.capabilitySlider;
  if (!slider) return;
  const valid = input.value !== "" && Number.isFinite(Number(input.value));
  if (valid) slider.value = input.value;
  input.capabilityPreview.textContent = valid ? `${displayNumber(Number(input.value))}${input.capabilityUnit}${homeDrafts.has(input.dataset.draftKey) ? " · 待应用" : ""}` : "待输入，拖动后点击应用";
  slider.setAttribute("aria-valuetext", valid ? input.capabilityPreview.textContent : "暂无当前值，尚未应用");
}

function readCapabilityInput(input) {
  if (input.capabilityOptions) return input.capabilityOptions[Number(input.value)]?.value;
  return input.type === "number" ? Number(input.value) : input.value;
}

function buildCapabilityControl(device, control, isAction) {
  const form = homeNode("form", "capability-control");
  form.dataset.capabilityAction = control.action;
  form.dataset.capabilityType = isAction ? "action" : "property";
  const heading = homeNode("div", "home-control-heading");
  heading.append(homeNode("strong", "", control.label), homeNode("output", "capability-current"));
  form.append(heading);
  const fields = isAction ? control.inputs || [] : [control];
  fields.forEach((field) => form.append(capabilityInput(device, control, field, isAction)));
  if (isAction && control.outputs?.length) form.append(homeNode("p", "capability-control-note", `返回字段：${control.outputs.map((item) => item.label || "结果").join("、")}`));
  const note = homeNode("p", "capability-control-note");
  note.dataset.capabilityNote = "true";
  form.append(note);
  const submit = homeNode("button", "secondary-button", isAction ? "执行动作" : "应用设置");
  submit.type = "submit";
  submit.setAttribute("aria-label", `${isAction ? "执行" : "应用"}${control.label}`);
  form.append(submit);
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    if (!form.reportValidity()) return;
    const inputs = [...form.querySelectorAll("[data-capability-input]")];
    const value = isAction ? Object.fromEntries(inputs.map((input) => [input.dataset.capabilityInput, readCapabilityInput(input)])) : readCapabilityInput(inputs[0]);
    runHomeControl(device.key, control.action, value);
  });
  return form;
}

function capabilityPropertyCard(property) {
  const card = homeNode("div", "capability-property");
  card.dataset.propertyKey = property.key;
  card.append(homeNode("span", "capability-property-label", property.label || property.key), homeNode("strong", "capability-property-value"));
  const badges = homeNode("small", "capability-property-badges");
  badges.textContent = [property.readable ? "可读取" : "不可读取", property.writable ? "可设置" : "只读", property.notifiable ? "支持事件上报" : "", property.sensitive ? "敏感信息" : ""].filter(Boolean).join(" · ");
  card.append(badges, homeNode("small", "capability-property-note"));
  return card;
}

function buildHomeCapabilities(device) {
  const container = $("#home-capabilities");
  if (!container) return;
  container.replaceChildren();
  const properties = device.properties || [];
  const promoted = homeFavoriteFanControl(device);
  const controls = (device.extra_controls || []).filter(control => control !== promoted);
  const actions = device.capabilities_actions || [];
  const events = device.capabilities_events || [];
  container.hidden = !properties.length && !actions.length && !events.length;
  if (container.hidden) return;
  const head = homeNode("div", "capability-heading");
  head.append(homeNode("h3", "", "设置、资料与高级能力"), homeNode("p", "", "常用操作在上方；设备档案、完整字段和读取诊断按需展开。"));
  container.append(head);
  const settingsLayer = capabilitySection(device, "layer:settings", `更多设置 · ${controls.length} 项`);
  settingsLayer.dataset.homeLayer = "settings";
  if (controls.length) container.append(settingsLayer);
  const archived = properties.filter((property) => ["identity", "metadata", "collection"].includes(fieldRole(property, device.state)) && !property.sensitive);
  const archivedKeys = new Set(archived.map((property) => property.key));
  const archiveLayer = capabilitySection(device, "layer:identity", `设备档案与低频资料 · ${archived.length} 项`);
  archiveLayer.dataset.homeLayer = "identity";
  if (archived.length) {
    const grid = homeNode("div", "capability-properties");
    archived.forEach((property) => grid.append(capabilityPropertyCard(property)));
    archiveLayer.append(grid);
    container.append(archiveLayer);
  }
  const diagnosticsLayer = capabilitySection(device, "layer:diagnostics", `完整能力与读取诊断 · ${properties.length} 项属性`);
  diagnosticsLayer.dataset.homeLayer = "diagnostics";
  container.append(diagnosticsLayer);
  const counts = homeNode("div", "capability-counts");
  [["属性", properties.length], ["可读取", properties.filter((item) => item.readable).length], ["可写属性", properties.filter((item) => item.writable).length], ["动作", actions.length], ["事件", events.length]].forEach(([label, count]) => {
    const stat = homeNode("div");
    stat.append(homeNode("strong", "", String(count)), homeNode("span", "", label));
    counts.append(stat);
  });
  diagnosticsLayer.append(counts);
  const serviceNames = new Set([...(device.services || []).map((service) => service.label), ...[...properties, ...controls, ...actions, ...events].map((item) => item.service || "其他能力")].filter(Boolean));
  serviceNames.forEach((serviceName) => {
    const matches = (item) => (item.service || "其他能力") === serviceName;
    const serviceProperties = properties.filter((property) => matches(property) && !archivedKeys.has(property.key));
    const serviceControls = controls.filter(matches);
    const serviceActions = actions.filter(matches);
    const serviceEvents = events.filter(matches);
    if (!serviceProperties.length && !serviceControls.length && !serviceActions.length && !serviceEvents.length) return;
    const section = capabilitySection(device, `service:${serviceName}`, `${serviceName} · ${serviceProperties.length} 项属性`);
    const grid = homeNode("div", "capability-properties");
    serviceProperties.forEach((property) => grid.append(capabilityPropertyCard(property)));
    section.append(grid);
    if (serviceControls.length) {
      const adjustments = capabilitySection(device, `settings:${serviceName}`, `${serviceName} · ${serviceControls.length} 项`);
      serviceControls.forEach((control) => adjustments.append(buildCapabilityControl(device, control, false)));
      settingsLayer.append(adjustments);
    }
    if (serviceActions.length) {
      const actionSection = capabilitySection(device, `actions:${serviceName}`, `设备动作 · ${serviceActions.length} 项`);
      serviceActions.forEach((action) => actionSection.append(buildCapabilityControl(device, action, true)));
      section.append(actionSection);
    }
    if (serviceEvents.length) {
      const eventSection = capabilitySection(device, `events:${serviceName}`, `事件能力 · ${serviceEvents.length} 项`);
      serviceEvents.forEach((event) => {
        const row = homeNode("div", "capability-event");
        row.append(homeNode("strong", "", event.label), homeNode("span", "", event.status || "事件订阅未接入"));
        const labels = (event.arguments || []).map((arg) => typeof arg === "string" ? properties.find((item) => item.key === arg)?.label || arg : arg.label || "事件参数");
        if (labels.length) row.append(homeNode("small", "", `包含：${labels.join("、")}`));
        eventSection.append(row);
      });
      section.append(eventSection);
    }
    if (serviceProperties.length || serviceActions.length || serviceEvents.length) diagnosticsLayer.append(section);
  });
}

function updateHomeCapabilities(device) {
  const container = $("#home-capabilities");
  if (!container) return;
  container.querySelectorAll("[data-property-key]").forEach((row) => {
    const property = (device.properties || []).find((item) => item.key === row.dataset.propertyKey);
    if (property) {
      row.querySelector(".capability-property-value").textContent = displayPropertyValue(property, device.state);
      const diagnostic = fieldReadStatus(property, device.state);
      row.dataset.readStatus = diagnostic.status;
      const previous = stateFieldValue(device.state, property) != null && !["ok", "sensitive"].includes(diagnostic.status);
      row.querySelector(".capability-property-note").textContent = [diagnostic.label, previous ? "显示上次读数" : "", diagnostic.message || property.reason, fieldCadence(property, { resource_id: homeResourceId(device), state: device.state }), diagnostic.observed_at ? `最近读取 ${formatTime(diagnostic.observed_at)}` : "", diagnostic.source ? fieldSourceLabel(diagnostic.source) : "", diagnostic.attempted_at ? `尝试于 ${formatTime(diagnostic.attempted_at)}` : ""].filter(Boolean).join(" · ");
    }
  });
  container.querySelectorAll("[data-capability-action]").forEach((section) => {
    const action = section.dataset.capabilityAction;
    const isAction = section.dataset.capabilityType === "action";
    const control = (isAction ? device.capabilities_actions : device.extra_controls)?.find((item) => item.action === action);
    if (!control) return;
    const unavailable = genericControlUnavailable(device, control);
    const reason = !device.enabled ? "请先在设备管理中授权" : device.cloud_online === false ? "设备离线，待重新连接后操作" : control.reason || control.confirmation_note || (control.requires_confirmation ? "执行前需要确认" : isAction ? "发送后会显示设备的接收结果" : "设置后将读取实际状态");
    section.querySelector("[data-capability-note]").textContent = reason;
    const current = device.state?.[control.property];
    const output = section.querySelector(".capability-current");
    const property = (device.properties || []).find((item) => item.key === control.property) || { key: control.property, unit: control.unit };
    output.textContent = isAction ? (control.enabled === false ? "暂不可执行" : "可执行") : displayPropertyValue(property, device.state);
    section.querySelectorAll("[data-capability-input]").forEach((input) => {
      input.disabled = unavailable;
      if (input.capabilitySlider) input.capabilitySlider.disabled = unavailable;
      if (!isAction && !homeDrafts.has(input.dataset.draftKey) && document.activeElement !== input) {
        if (input.capabilityOptions) {
          const optionIndex = input.capabilityOptions.findIndex((option) => String(option.value).toLowerCase() === String(current).toLowerCase() || String(option.label).toLowerCase() === String(current).toLowerCase());
          input.value = optionIndex < 0 ? "" : String(optionIndex);
        } else input.value = current ?? "";
      }
      updateCapabilitySlider(input);
    });
    section.querySelector('button[type="submit"]').disabled = unavailable || (!isAction && !homeDrafts.has(`${device.key}:${control.action}`));
  });
}

function homeExtensionKind(device) {
  if (!device) return "";
  if (device.infrared_ac) return "infrared";
  if (device.category === "speaker" || /wifispeaker/.test(device.model || "")) return "speaker";
  if (["infrared-remote-control", "ir-remote-control", "remote-control"].includes(device.category) || /^chuangmi\.ir\./.test(device.model || "")) return "infrared";
  return "";
}

function extensionControls(extension) {
  return [...(extension?.controls || []), ...(extension?.actions || [])].filter((item) => item && typeof item.action === "string");
}

function homeControlKey(device) {
  return device.infrared_ac?.parent_key || device.key;
}

function renderInfraredManagementEntries() {
  const list = $("#home-management-list");
  if (!list || !currentHome) return;
  list.querySelectorAll("[data-infrared-ac-device]").forEach(row => row.remove());
  (currentHome.devices || []).filter(device => device.infrared_ac).forEach(device => {
    const row = homeNode("div", "device-row");
    row.dataset.infraredAcDevice = device.key;
    const identity = homeNode("div", "device-identity");
    identity.append(homeNode("strong", "", device.alias || device.name),
      homeNode("span", "", `空调 · ${homeStatusText(device)}`),
      homeNode("small", "", `关联遥控器：${device.infrared_ac.parent_name} · 设备授权由关联遥控器管理`));
    const open = homeNode("button", "secondary-button", "打开空调");
    open.type = "button";
    open.addEventListener("click", () => openHomeDevice(device.key));
    row.append(identity, open);
    list.append(row);
  });
}

function homeDevicePending(device) {
  return homePending.has(device.key) || homePending.has(homeControlKey(device));
}

function homeDeviceExtension(device) {
  const extension = homeExtensions.get(homeControlKey(device));
  if (!device.infrared_ac || !extension) return extension;
  return { ...extension, ac_profiles: (extension.ac_profiles || []).filter(profile => profile.key === device.infrared_ac.profile_key),
    controls: extensionControls(extension).filter(control => ["ir_ac_send", "ir_ac_ai_permission"].includes(control.action)) };
}

function syncInfraredDeviceEntries() {
  if (!currentHome) return;
  const physical = (currentHome.devices || []).filter(device => !device.infrared_ac);
  const seeds = currentHome.infrared_devices || [];
  const entries = [];
  const previousSelected = currentHome.devices?.find(device => device.key === homeSelectedKey);
  const parents = [...new Set(seeds.map(device => device.infrared_ac.parent_key))];
  parents.forEach(parentKey => {
    const parent = physical.find(device => device.key === parentKey);
    if (!parent) return;
    const base = seeds.find(device => device.infrared_ac.parent_key === parentKey);
    const extension = homeExtensions.get(parentKey);
    const profiles = parent.enabled && Array.isArray(extension?.ac_profiles) ? extension.ac_profiles : null;
    const candidates = profiles === null ? seeds.filter(device => device.infrared_ac.parent_key === parentKey) :
      (profiles.length ? profiles : [null]).map(profile => ({ ...base,
        key: `ir-ac:${parentKey}:${profile?.key || "pending"}`,
        name: profile ? profile.name.replace(/^(?:打开|开启|关闭|关掉)\s*|\s*(?:开机|关机)$/g, "").trim() || "空调" : "空调",
        model: profile?.protocol_label || "待选择空调协议", ac_profile: profile,
        infrared_ac: { ...base.infrared_ac, profile_key: profile?.key || "", ready: Boolean(profile) },
      }));
    candidates.forEach(device => entries.push({ ...device, alias: device.name, enabled: parent.enabled,
      cloud_online: parent.cloud_online, state: {}, online: false, stale: false, observed_at: null,
      infrared_ac: { ...device.infrared_ac, parent_name: parent.alias || parent.name } }));
  });
  currentHome.devices = [...physical, ...entries];
  if (previousSelected?.infrared_ac && !currentHome.devices.some(device => device.key === homeSelectedKey)) {
    homeSelectedKey = entries.find(device => device.infrared_ac.parent_key === previousSelected.infrared_ac.parent_key)?.key || "";
  }
  text("#home-device-count", `${currentHome.devices.length} 台`);
  renderInfraredManagementEntries();
}

function acceptHomeExtension(key, extension) {
  if (!extension || !["speaker", "infrared"].includes(extension.kind)) return;
  const previous = homeExtensions.get(key);
  if (previous?.observed_at && extension.observed_at && new Date(previous.observed_at) > new Date(extension.observed_at)) return;
  homeExtensions.set(key, extension);
  syncInfraredDeviceEntries();
}

function extensionUnavailable(device, control) {
  const extension = homeDeviceExtension(device);
  const loading = homeExtensionLoads.has(homeControlKey(device));
  if (device.infrared_ac && !device.infrared_ac.ready) return true;
  if (["ir_ac_ai_permission", "ir_ac_create"].includes(control.action)) return !device.enabled || homeDevicePending(device) || !extension || loading;
  return genericControlUnavailable(device, control) || homeDevicePending(device) || !extension || extension.available === false || control.available === false || loading;
}

async function loadHomeExtensions(key, refresh = false) {
  const device = currentHome?.devices?.find((item) => item.key === key);
  if (!homeExtensionKind(device)) return;
  key = homeControlKey(device);
  if (homeExtensionLoads.has(key)) return homeExtensionLoads.get(key);
  const promise = request(`/api/home/devices/${encodeURIComponent(key)}/extensions${refresh ? "/refresh" : ""}`, {
    ...(refresh ? { method: "POST", body: "{}" } : {}), timeoutMs: 45000,
  });
  homeExtensionLoads.set(key, promise);
  renderHomeDetail();
  try {
    const extension = await promise;
    acceptHomeExtension(key, extension);
  } catch (error) {
    homeExtensions.set(key, { ...(homeExtensions.get(key) || {}), kind: homeExtensionKind(device), available: false, message: `专属服务暂未连接：${error.message}` });
  } finally {
    homeExtensionLoads.delete(key);
    renderHomeCategories();
    renderHomeCards();
    renderHomeDetail();
  }
}

function extensionButton(device, control, label = control.label, value) {
  const button = homeNode("button", "secondary-button extension-command", label);
  button.type = "button";
  button.dataset.extensionCommand = control.action;
  button.addEventListener("click", () => runHomeControl(device.key, control.action, value));
  return button;
}

function extensionForm(device, control, fields, { object = false, submitLabel = "应用设置", value = {} } = {}) {
  const form = homeNode("form", "capability-control extension-form");
  form.dataset.extensionAction = control.action;
  form.append(homeNode("strong", "extension-form-title", control.label));
  fields.forEach((field) => {
    const wrapper = capabilityInput(device, { ...control, enabled: control.available !== false }, field, object);
    const input = wrapper.querySelector("[data-capability-input]");
    input.dataset.extensionInput = field.key || control.action;
    input.dataset.extensionState = field.state || "";
    if (field.type === "url") {
      input.type = "url";
      input.placeholder = "https://…（音箱可访问的音频链接）";
    } else if (field.placeholder) input.placeholder = field.placeholder;
    if (field.default != null && !homeDrafts.has(input.dataset.draftKey)) input.value = field.default;
    input.addEventListener("input", () => updateHomeExtensions(device));
    form.append(wrapper);
  });
  if (control.reason) form.append(homeNode("p", "capability-control-note", control.reason));
  const submit = homeNode("button", "secondary-button", submitLabel);
  submit.type = "submit";
  submit.dataset.extensionCommand = control.action;
  form.append(submit);
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    if (!form.reportValidity()) return;
    const inputs = [...form.querySelectorAll("[data-extension-input]")];
    const submitted = object ? { ...value, ...Object.fromEntries(inputs.map((input) => [input.dataset.extensionInput, readCapabilityInput(input)])) } : readCapabilityInput(inputs[0]);
    runHomeControl(device.key, control.action, submitted, { draftKeys: inputs.map((input) => input.dataset.draftKey) });
  });
  return form;
}

function buildSpeakerExtension(device, extension, container) {
  const now = homeNode("div", "speaker-now-playing");
  now.append(homeNode("span", "eyebrow", "正在播放"), homeNode("strong", "speaker-track"), homeNode("span", "speaker-playback"));
  container.append(now);
  const controls = extensionControls(extension);
  const playback = homeNode("div", "speaker-playback-buttons");
  controls.filter((control) => control.type === "button").forEach((control) => playback.append(extensionButton(device, control)));
  if (playback.children.length) container.append(playback);
  const forms = homeNode("div", "extension-settings-grid");
  controls.filter((control) => control.type !== "button").forEach((control) => {
    const isVolume = control.action === "speaker_volume";
    const field = { ...control, key: control.action, kind: control.type === "number" ? "range" : control.type === "select" ? "select" : "text", state: isVolume ? "volume" : control.action === "speaker_loop" ? "loop" : "", unit: isVolume ? "%" : "", format: control.type === "number" ? "number" : "string", max_length: control.type === "url" ? 2048 : 500, placeholder: control.action === "speaker_tts" ? "输入希望小爱朗读的内容" : "请输入" };
    forms.append(extensionForm(device, control, [field], { submitLabel: control.type === "text" ? "朗读内容" : control.type === "url" ? "播放音频" : "应用设置" }));
  });
  container.append(forms, homeNode("p", "extension-footnote", "媒体控制与文字播报由小爱服务执行；播放内容、在线状态及账号权限会影响结果。"));
}

function acCommandKey(deviceKey, profileKey) {
  return `${deviceKey}:ir_ac_send:${profileKey}:`;
}

function cancelAcAdjustments(deviceKey, profileKey) {
  for (const [key, command] of homeAcCommands) {
    if (deviceKey && (command.deviceKey !== deviceKey || command.profileKey !== profileKey)) continue;
    clearTimeout(command.timer);
    command.patch = {};
    if (!command.sending) homeAcCommands.delete(key);
  }
}

function queueAcAdjustment(device, profileKey, patch, immediate = false) {
  const key = acCommandKey(device.key, profileKey);
  const command = homeAcCommands.get(key) || {deviceKey: device.key, profileKey, patch: {}};
  clearTimeout(command.timer);
  Object.assign(command.patch, patch);
  command.status = "queued";
  command.message = "等待发送…连续调整会合并";
  homeAcCommands.set(key, command);
  updateHomeExtensions(device);
  if (immediate) void flushAcAdjustment(key);
  else command.timer = setTimeout(() => flushAcAdjustment(key), 450);
}

async function flushAcAdjustment(key) {
  const command = homeAcCommands.get(key);
  if (!command || command.sending || !Object.keys(command.patch).length) return;
  const device = currentHome?.devices?.find(item => item.key === command.deviceKey);
  if (!device || homeSelectedKey !== device.key) {
    cancelAcAdjustments(command.deviceKey, command.profileKey);
    return;
  }
  const extension = homeDeviceExtension(device);
  const control = extensionControls(extension).find(item => item.action === "ir_ac_send");
  if (homeDevicePending(device) || homeExtensionLoads.has(homeControlKey(device))) {
    command.timer = setTimeout(() => flushAcAdjustment(key), 150);
    return;
  }
  if (!control || !extension?.ac_profiles?.some(item => item.key === command.profileKey) || extensionUnavailable(device, control)) {
    command.status = "failed";
    command.message = "暂未发送：设备未连接或未授权，调整已保留";
    updateHomeExtensions(device);
    return;
  }
  const patch = {...command.patch};
  command.patch = {};
  command.sending = true;
  command.status = "sending";
  command.message = "正在发送…";
  const draftKeys = Object.keys(patch).map(field => key + field);
  const result = await runHomeControl(device.key, "ir_ac_send", {profile_key: command.profileKey, settings: patch},
    {draftKeys, draftValues: Object.fromEntries(draftKeys.map(draft => [draft, homeDrafts.get(draft)]))});
  command.sending = false;
  if (result && ["accepted", "confirmed"].includes(result.outcome)) {
    command.status = "accepted";
    command.message = "已发送 · 空调实际状态无法回读";
  } else {
    command.status = "failed";
    command.patch = {...patch, ...command.patch};
    command.message = "发送未确认，调整已保留；可点击重试";
  }
  renderHomeDetail();
}

function buildInfraredAc(device, extension, container) {
  (extension.ac_profiles || []).forEach((profile) => {
    const card = homeNode("section", "ir-ac-panel");
    card.dataset.acProfile = profile.key;
    const top = homeNode("div", "ir-ac-heading");
    const title = homeNode("div");
    title.append(homeNode("p", "eyebrow", "空调完整控制"), homeNode("h3", "", profile.name || "已学习空调"));
    top.append(title, homeNode("span", "ir-ac-status", "无状态回读"));
    card.append(top);
    if (profile.source_kind === "protocol") card.append(homeNode("p", "extension-footnote", "协议生成控制指令 · 无需学习或绑定按键"));
    const form = homeNode("form", "ir-ac-form");
    form.dataset.extensionAction = "ir_ac_send";
    const draftPrefix = acCommandKey(device.key, profile.key);
    const stored = (key) => homeDrafts.has(draftPrefix + key) ? homeDrafts.get(draftPrefix + key) : profile.settings[key];
    const settings = () => Object.fromEntries([...form.querySelectorAll("[data-ac-field]")].map((input) => [input.dataset.acField, input.type === "checkbox" ? input.checked : input.dataset.acNumber ? Number(input.value) : input.value]));
    const bind = (input, key) => {
      input.dataset.acField = key;
      input.dataset.acDraft = draftPrefix + key;
      const value = stored(key);
      if (input.type === "checkbox") input.checked = Boolean(value); else input.value = value ?? "";
      input.addEventListener("input", (event) => {
        const value = input.type === "checkbox" ? input.checked : input.dataset.acNumber ? Number(input.value) : input.value;
        homeDrafts.set(draftPrefix + key, value);
        const mode = form.querySelector('[data-ac-field="mode"]')?.value;
        if (["auto", "dry"].includes(mode)) {
          const temp = form.querySelector('[data-ac-field="temperature"]');
          if (temp) { temp.value = 25; homeDrafts.set(draftPrefix + "temperature", 25); }
        }
        if (!["cool", "dry"].includes(mode)) {
          const xfan = form.querySelector('[data-ac-field="xfan"]');
          if (xfan) { xfan.checked = false; homeDrafts.set(draftPrefix + "xfan", false); }
        }
        if (key === "fan") {
          for (const feature of ["turbo", "quiet"]) {
            const toggle = form.querySelector(`[data-ac-field="${feature}"]`);
            if (toggle) { toggle.checked = false; homeDrafts.set(draftPrefix + feature, false); }
          }
        } else if (["turbo", "quiet"].includes(key) && value) {
          const other = key === "turbo" ? "quiet" : "turbo";
          const toggle = form.querySelector(`[data-ac-field="${other}"]`);
          if (toggle) { toggle.checked = false; homeDrafts.set(draftPrefix + other, false); }
        }
        if (!form.reportValidity()) {
          cancelAcAdjustments(device.key, profile.key);
          updateHomeExtensions(device);
          return;
        }
        const patch = Object.fromEntries([...form.querySelectorAll("[data-ac-field]")]
          .filter(field => homeDrafts.has(field.dataset.acDraft))
          .map(field => [field.dataset.acField, field.type === "checkbox" ? field.checked : field.dataset.acNumber ? Number(field.value) : field.dataset.acField === "swing_horizontal" ? field.value === "true" : field.value]));
        queueAcAdjustment(device, profile.key, patch, event.detail?.acImmediate === true);
      });
      return input;
    };
    const select = (key, label, options, numeric = false) => {
      const wrapper = homeNode("label", "ir-ac-field");
      const input = homeNode("select");
      input.setAttribute("aria-label", label);
      options.forEach((option) => input.append(new Option(option.label, String(option.value))));
      if (numeric) input.dataset.acNumber = "true";
      bind(input, key);
      wrapper.append(homeNode("span", "", label), input);
      return wrapper;
    };
    const hero = homeNode("div", "ir-ac-temperature");
    const temperature = homeNode("input");
    temperature.type = "number"; temperature.min = 16; temperature.max = 30; temperature.step = 1;
    temperature.required = true; temperature.dataset.acNumber = "true";
    temperature.setAttribute("aria-label", "空调目标温度");
    bind(temperature, "temperature");
    const stepper = (delta, label) => {
      const button = homeNode("button", "ir-ac-step", delta < 0 ? "−" : "+");
      button.type = "button"; button.dataset.acTemperatureStep = "true";
      button.setAttribute("aria-label", label);
      button.addEventListener("click", () => {
        const previous = Number(temperature.value || 25);
        const target = Math.min(30, Math.max(16, previous + delta));
        if (target === previous) return;
        temperature.value = target;
        temperature.dispatchEvent(new CustomEvent("input", {bubbles: true, detail: {acImmediate: true}}));
      });
      return button;
    };
    const temperatureValue = homeNode("div", "ir-ac-temperature-value");
    temperatureValue.append(temperature, homeNode("span", "", "℃"), homeNode("small", "", "目标温度 · ＋／－ 即时发送"));
    hero.append(stepper(-1, "降低目标温度"), temperatureValue, stepper(1, "提高目标温度"));
    form.append(hero);
    const main = homeNode("div", "ir-ac-grid");
    main.append(select("mode", "运行模式", profile.mode_options), select("fan", "风速", [
      {value: 0, label: "自动风速"}, ...[1,2,3,4,5].map(value => ({value, label: `${value} 档风速`}))
    ], true));
    main.append(select("swing_vertical", "上下扫风", profile.swing_options, true), select("swing_horizontal", "左右扫风", [{value:"false",label:"关闭"},{value:"true",label:"开启"}]));
    // Checkbox keeps boolean semantics for power; no inferred current state.
    const powerField = homeNode("label", "ir-ac-toggle ir-ac-power-setting");
    const power = bind(Object.assign(homeNode("input"), {type:"checkbox"}), "power");
    powerField.append(power, homeNode("span", "", "电源开启"));
    main.append(powerField);
    form.append(main);
    // Left/right swing uses explicit boolean options, not truthy strings.
    const horizontal = form.querySelector('[data-ac-field="swing_horizontal"]');
    const readSettings = () => {const value = settings(); value.swing_horizontal = horizontal.value === "true"; return value;};
    const extras = capabilitySection(device, `ac-extra:${profile.key}`, "更多功能", true);
    const features = homeNode("div", "ir-ac-features");
    [["turbo","强劲模式"], ["quiet","静音模式"], ["light","显示灯"], ["ion_filter","净化 / 健康"], ["xfan","关机干燥（X-Fan）"]].forEach(([key,label]) => {
      const wrapper = homeNode("label", "ir-ac-toggle");
      const input = bind(Object.assign(homeNode("input"), {type:"checkbox"}), key);
      wrapper.append(input, homeNode("span", "", label)); features.append(wrapper);
    });
    extras.append(features, homeNode("p", "extension-footnote", "附加功能按协议提供，是否响应取决于空调机型。自动与除湿模式的温度固定为25℃；干燥仅在制冷或除湿时有效。"));
    form.append(extras);
    const fanHint = homeNode("p", "extension-footnote ir-ac-fan-hint");
    fanHint.setAttribute("role", "status");
    form.append(fanHint);
    const actions = homeNode("div", "ir-ac-actions");
    const status = homeNode("p", "extension-footnote ir-ac-auto-status", "调整后自动发送；连续调整会合并，无需应用或确认。");
    status.setAttribute("role", "status");
    form.append(status);
    const apply = homeNode("button", "primary-button", "重试发送");
    apply.type = "submit"; apply.dataset.extensionCommand = "ir_ac_send"; apply.hidden = true;
    const reset = homeNode("button", "secondary-button", "撤销未发送调整");
    reset.type = "button";
    reset.dataset.acDiscard = "true";
    reset.addEventListener("click", () => {
      cancelAcAdjustments(device.key, profile.key);
      for (const key of [...homeDrafts.keys()]) if (key.startsWith(draftPrefix)) homeDrafts.delete(key);
      homeExtensionSignature = ""; renderHomeDetail();
    });
    actions.append(apply, reset); form.append(actions);
    form.addEventListener("submit", (event) => {
      event.preventDefault(); if (!form.reportValidity()) return;
      const patch = Object.fromEntries([...form.querySelectorAll("[data-ac-field]")]
        .filter(input => homeDrafts.has(input.dataset.acDraft))
        .map(input => [input.dataset.acField, readSettings()[input.dataset.acField]]));
      if (Object.keys(patch).length) queueAcAdjustment(device, profile.key, patch, true);
    });
    const quick = homeNode("div", "ir-ac-quick");
    [[true,"开机"],[false,"关机"]].forEach(([on,label])=>{
      const button = homeNode("button", on ? "secondary-button" : "secondary-button ir-ac-off", label);
      button.type="button"; button.dataset.extensionCommand="ir_ac_send";
      button.addEventListener("click",()=>{
        power.checked = on;
        power.dispatchEvent(new CustomEvent("input", {bubbles: true, detail: {acImmediate: true}}));
      });
      quick.append(button);
    });
    card.append(quick, form);
    const permission = homeNode("div", "ir-ac-permission");
    const permissionLabel = homeNode("label", "ir-ac-toggle");
    const enabled = homeNode("input"); enabled.type="checkbox"; enabled.checked=profile.ai_control_enabled===true;
    enabled.dataset.acAiPermission="true";
    const permissionDraft = `${device.key}:ir_ac_ai_permission:${profile.key}`;
    if(homeDrafts.has(permissionDraft))enabled.checked=homeDrafts.get(permissionDraft);
    enabled.addEventListener("input",()=>homeDrafts.set(permissionDraft,enabled.checked));
    permissionLabel.append(enabled, homeNode("span","","允许 AI 控制这台空调"));
    const save = homeNode("button","secondary-button","保存权限"); save.type="button";save.dataset.extensionCommand="ir_ac_ai_permission";
    save.addEventListener("click",()=>runHomeControl(device.key,"ir_ac_ai_permission",{profile_key:profile.key,enabled:enabled.checked},{draftKeys:[permissionDraft]}));
    permission.append(permissionLabel, save, homeNode("small","","开启并保存后，AI 可按你的明确指令发送空调设置。"));
    card.append(permission);
    const source = homeNode("p", "extension-footnote ir-ac-last-command");
    const last = profile.last_attempt;
    source.textContent = ({last_sent:"显示上次发送设置",protocol_defaults:"显示协议默认设置",learned_signal:"显示已学习信号中的设置"}[profile.settings_source] || "显示配置设置") +
      (last ? ` · 最近指令${({accepted:"已发送",unconfirmed:"结果未知",pending:"结果未确认",failed:"失败"})[last.outcome] || "结果未确认"}` : "") +
      "。这些值不是空调当前状态。";
    card.append(source, homeNode("p", "extension-footnote", "未开放：定时、自定义睡眠曲线与未确认的机型专用功能。可继续学习这些功能的实体遥控器按键，按原样重放。"));
    container.append(card);
  });
}

function updateInfraredAc(device, extension) {
  document.querySelectorAll(".ir-ac-panel").forEach((card) => {
    const profile = extension?.ac_profiles?.find(item=>item.key===card.dataset.acProfile);
    if(!profile)return;
    const disabled = extensionUnavailable(device,{action:"ir_ac_send",available:true});
    const localDisabled = !device.enabled || homeDevicePending(device) || homeExtensionLoads.has(homeControlKey(device));
    card.querySelectorAll("[data-ac-field]").forEach(input => {
      if (!homeDrafts.has(input.dataset.acDraft) && document.activeElement !== input) {
        const value = profile.settings[input.dataset.acField];
        if (input.type === "checkbox") input.checked = Boolean(value);
        else input.value = value == null ? "" : String(value);
      }
    });
    const mode = card.querySelector('[data-ac-field="mode"]')?.value;
    const turbo = card.querySelector('[data-ac-field="turbo"]')?.checked;
    const quiet = card.querySelector('[data-ac-field="quiet"]')?.checked;
    const fanHint = card.querySelector('.ir-ac-fan-hint');
    if (fanHint) fanHint.textContent = turbo || quiet
      ? `${turbo ? "强劲" : "静音"}模式优先控制风速；选择风速将退出强劲和静音，并自动发送。`
      : "强劲与静音互斥；选择风速会退出这两种模式。";
    const fixedTemp = ["auto","dry"].includes(mode);
    card.querySelectorAll("[data-ac-field]").forEach(input=>{
      input.disabled=disabled || input.dataset.acField==="temperature" && fixedTemp || input.dataset.acField==="xfan" && !["cool","dry"].includes(mode);
    });
    card.querySelectorAll("[data-ac-temperature-step]").forEach(button=>button.disabled=disabled||fixedTemp);
    card.querySelector("[data-ac-ai-permission]").disabled=localDisabled;
    const command = homeAcCommands.get(acCommandKey(device.key, profile.key));
    const autoStatus = card.querySelector(".ir-ac-auto-status");
    const hasDraft = [...homeDrafts.keys()].some(key => key.startsWith(acCommandKey(device.key, profile.key)));
    if (autoStatus) autoStatus.textContent = command?.message || (hasDraft ? "未发送的调整已保留；修改后自动发送，或点击重试。" : "调整后自动发送；连续调整会合并，无需应用或确认。");
    const retry = card.querySelector('button[type="submit"]');
    if (retry) retry.hidden = command?.status !== "failed" && (Boolean(command) || !hasDraft);
    const discard = card.querySelector("[data-ac-discard]");
    if (discard) {
      discard.hidden = !hasDraft;
      discard.disabled = homeDevicePending(device);
    }
  });
}

function buildInfraredExtension(device, extension, container) {
  const controls = extensionControls(extension);
  const control = (action) => controls.find((item) => item.action === action);
  buildInfraredAc(device, extension, container);
  if (control("ir_ac_create")) {
    const setup = capabilitySection(device, "ir:protocol-ac", "添加协议空调");
    setup.append(homeNode("p", "extension-footnote", "已支持 Kelvinator 128 位协议及部分兼容机型。选择已确认匹配的协议后，即可直接生成温度、模式、风速等指令，无需学习按键。"));
    setup.append(extensionForm(device, control("ir_ac_create"), [{key:"label",label:"空调名称",max_length:60,default:"空调"}],
      {object:true,value:{protocol:"kelvinator"},submitLabel:"添加空调"}));
    container.append(setup);
  }
  const remotes = extension.remotes || [];
  const learned = extension.learned_buttons || [];
  if (!remotes.length && !learned.length) container.append(homeNode("p", "extension-empty", "尚未发现已配对的遥控器。可先在米家添加遥控器，再点击“刷新专属服务”；支持学习的设备也可在下面录入按键。"));
  remotes.forEach((remote) => {
    const section = capabilitySection(device, `remote:${remote.key}`, `${remote.name || "遥控器"}${remote.category ? ` · ${remote.category}` : ""}`, true);
    const keypad = homeNode("div", "ir-keypad");
    (remote.buttons || []).forEach((button) => {
      if (!control("ir_send")) return;
      const command = extensionButton(device, control("ir_send"), button.label || "遥控按键", { remote_key: remote.key, button_key: button.key });
      command.dataset.remoteKey = remote.key;
      command.dataset.buttonKey = button.key;
      keypad.append(command);
    });
    if (!keypad.children.length) keypad.append(homeNode("p", "extension-empty", "这个遥控器暂未返回可用按键。"));
    section.append(keypad);
    container.append(section);
  });
  const learnedSection = capabilitySection(device, "ir:learned", `已学习按键 · ${learned.length} 个`, true);
  if (!learned.length) learnedSection.append(homeNode("p", "extension-empty", "保存学到的按键后，可在这里一键发送。"));
  learned.forEach((button) => {
    const row = homeNode("div", "ir-learned-button");
    if (control("ir_send_learned")) row.append(extensionButton(device, control("ir_send_learned"), button.label || "已学习按键", { button_key: button.key }));
    else row.append(homeNode("strong", "", button.label || "已学习按键"));
    const settings = capabilitySection(device, `learned:${button.key}`, "管理按键");
    if (control("ir_learn_rename")) {
      const rename = extensionForm(device, control("ir_learn_rename"), [{ key: "label", label: "按键名称", max_length: 60, default: button.label, draft_key: `${device.key}:ir_learn_rename:${button.key}:label` }], { object: true, value: { button_key: button.key }, submitLabel: "保存名称" });
      settings.append(rename);
    }
    if (control("ir_learn_delete")) settings.append(extensionButton(device, control("ir_learn_delete"), "删除按键", { button_key: button.key }));
    row.append(settings);
    learnedSection.append(row);
  });
  container.append(learnedSection);
  if (extension.learning?.supported) {
    const section = capabilitySection(device, "ir:learning", "学习实体遥控器按键", true);
    section.append(homeNode("p", "extension-footnote", "开始学习后，将实体遥控器对准红外设备并按下目标键。点击检查，成功后为按键命名并保存。"));
    section.append(homeNode("p", "ir-learning-status"));
    const buttons = homeNode("div", "speaker-playback-buttons");
    if (control("ir_learn_start")) buttons.append(extensionButton(device, control("ir_learn_start"), "开始学习", {}));
    if (control("ir_learn_read")) {
      const read = extensionButton(device, control("ir_learn_read"), "检查学习结果");
      read.dataset.learningRequires = "active";
      buttons.append(read);
    }
    section.append(buttons);
    if (control("ir_learn_save")) {
      const save = extensionForm(device, control("ir_learn_save"), [{ key: "label", label: "新按键名称", max_length: 60, placeholder: "例如：客厅空调开关" }], { object: true, submitLabel: "保存学到的按键" });
      save.dataset.learningRequires = "ready";
      section.append(save);
    }
    container.append(section);
  }
  container.append(homeNode("p", "extension-footnote", "红外指令通常无法回读家电状态。发送成功表示网关接收了指令，请观察家电确认；学习能力仍取决于设备固件和云端支持。"));
}

function renderHomeExtensions(device) {
  const container = $("#home-extensions");
  if (!container) return;
  const kind = homeExtensionKind(device);
  container.hidden = !kind;
  if (!kind) return;
  const extension = homeDeviceExtension(device);
  const signature = `${device.key}:${JSON.stringify([kind, extensionControls(extension), extension?.remotes, extension?.learned_buttons, extension?.ac_profiles, extension?.learning?.supported])}`;
  if (homeExtensionSignature !== signature) {
    homeExtensionSignature = signature;
    container.replaceChildren();
    const header = homeNode("div", "section-heading compact");
    const title = homeNode("div");
    title.append(homeNode("p", "eyebrow", "专属服务"), homeNode("h3", "", device.infrared_ac ? "空调控制" : kind === "speaker" ? "小爱播放与播报" : "万能遥控器"));
    const refresh = homeNode("button", "secondary-button", "刷新专属服务");
    refresh.type = "button";
    refresh.dataset.extensionRefresh = "true";
    refresh.addEventListener("click", () => loadHomeExtensions(device.key, true));
    header.append(title, refresh);
    container.append(header, homeNode("p", "extension-connection-message"));
    if (extension && kind === "speaker") buildSpeakerExtension(device, extension, container);
    if (device.infrared_ac) {
      const link = homeNode("button", "secondary-button", `管理关联遥控器 · ${device.infrared_ac.parent_name}`);
      link.type = "button";
      link.dataset.acParent = device.infrared_ac.parent_key;
      link.addEventListener("click", () => openHomeDevice(device.infrared_ac.parent_key));
      if (extension?.ac_profiles?.length) buildInfraredAc(device, extension, container);
      else container.append(homeNode("p", "extension-empty", device.infrared_ac.ready ? "正在加载这台空调的控制设置…" : "这台空调尚未选择协议。进入关联遥控器，点击“添加协议空调”，即可使用已支持协议直接生成指令；已有的兼容学习信号也可继续使用。"));
      container.append(link);
    } else if (extension && kind === "infrared") buildInfraredExtension(device, extension, container);
  }
  updateHomeExtensions(device);
}

function updateHomeExtensions(device) {
  if (device.key !== homeSelectedKey) return;
  device = currentHome?.devices?.find((item) => item.key === device.key) || device;
  const container = $("#home-extensions");
  if (!container || container.hidden) return;
  const extension = homeDeviceExtension(device);
  const loading = homeExtensionLoads.has(homeControlKey(device));
  const controls = extensionControls(extension);
  const status = container.querySelector(".extension-connection-message");
  status.textContent = loading ? "正在连接专属服务…" : extension?.message || (extension?.available ? "专属服务已连接" : "正在准备专属服务…");
  status.classList.toggle("bad", extension?.available === false && !loading);
  container.querySelector("[data-extension-refresh]").disabled = loading || homeDevicePending(device) || !device.enabled;
  container.querySelectorAll("[data-extension-command]").forEach((button) => {
    const control = controls.find((item) => item.action === button.dataset.extensionCommand);
    const learningRequirement = button.dataset.learningRequires || button.closest("[data-learning-requires]")?.dataset.learningRequires;
    button.disabled = !control || extensionUnavailable(device, control) || Boolean(learningRequirement && !extension?.learning?.[learningRequirement]);
    button.title = control?.available === false ? control.reason || "设备暂不支持" : device.cloud_online === false ? "设备离线" : "";
  });
  container.querySelectorAll("[data-extension-input]").forEach((input) => {
    const form = input.closest("[data-extension-action]");
    const control = controls.find((item) => item.action === form.dataset.extensionAction);
    input.disabled = !control || extensionUnavailable(device, control);
    if (input.capabilitySlider) input.capabilitySlider.disabled = input.disabled;
    const state = input.dataset.extensionState;
    if (state && !homeDrafts.has(input.dataset.draftKey) && document.activeElement !== input) {
      const current = extension?.state?.[state];
      input.value = input.capabilityOptions ? String(input.capabilityOptions.findIndex((option) => String(option.value) === String(current))) : current ?? "";
    }
    updateCapabilitySlider(input);
  });
  const track = container.querySelector(".speaker-track");
  if (track) {
    const state = extension?.state || {};
    track.textContent = state.title || "暂无曲目信息";
    container.querySelector(".speaker-playback").textContent = [({ playing: "播放中", paused: "已暂停", idle: "空闲", unknown: "播放状态未知" })[state.playback] || "播放状态未知", state.artist].filter(Boolean).join(" · ");
  }
  if (extension?.kind === "infrared") updateInfraredAc(device, extension);
  const learning = container.querySelector(".ir-learning-status");
  if (learning) learning.textContent = extension?.learning?.message || (extension?.learning?.ready ? "已学到信号，请命名并保存。" : extension?.learning?.active ? "等待实体遥控器信号…" : "尚未开始学习");
}

function renderHomeDetail() {
  const device = currentHome?.devices?.find((item) => item.key === homeSelectedKey);
  $("#home-device-detail").hidden = !device;
  if (!device) return;
  const statusResource = routerLocalResource(device) || device;
  text("#home-detail-name", device.alias || device.name);
  setPill("#home-detail-status", homeStatusText(device), resourceStale(statusResource) || !device.enabled ? "bad" : "ok");
  text("#home-detail-time", device.infrared_ac ? "红外控制不提供空调实时状态" : statusResource.observed_at ? `最近确认 ${formatTime(statusResource.observed_at)}` : "尚无有效读数");
  text("#home-detail-support", device.support_note || "");
  const staticProperties = (device.properties || []).map(({ read_status, status, message, observed_at, attempted_at, code, value, ...metadata }) => metadata);
  const signature = `${device.key}:${JSON.stringify([device.controls || [], staticProperties, device.extra_controls || [], device.capabilities_actions || [], device.capabilities_events || [], device.services || []])}`;
  if (homeDetailSignature !== signature) {
    homeDetailSignature = signature;
    $("#home-detail-icon").replaceChildren(homeDeviceIcon(device.category));
    buildHomeControls(device);
    buildHomeCapabilities(device);
  }
  $("#home-detail-icon").classList.toggle("is-on", homePower(device) === true);
  const metrics = $("#home-detail-metrics");
  metrics.replaceChildren();
  const local = routerLocalResource(device);
  metrics.classList.toggle("is-stale", resourceStale(local || device));
  const sourcePanel = $("#home-local-source");
  sourcePanel.hidden = !local;
  sourcePanel.replaceChildren();
  if (local) {
    sourcePanel.append(homeNode("span", `status-pill ${local.stale ? "bad" : "ok"}`, local.stale ? "路由器本地 · 读数待更新" : "路由器本地 · 只读"), homeNode("p", "", device.local_source.message || "本地状态只读展示"), homeNode("small", "", local.observed_at ? `本地最近读取 ${formatTime(local.observed_at)}` : "尚无本地有效读数"));
    const fields = (local.fields || []).filter((field) => fieldPriority(field, local.state) === "primary" && !["identity", "metadata", "parameter", "event"].includes(fieldRole(field, local.state)) && (field.key !== "local.system-temperature" || stateFieldValue(local.state, field) != null));
    appendHubFieldCards(metrics, local, fields, { compact: true });
  } else appendMetricCards(metrics, device.state, { compact: true, includeDetails: false, properties: homeDeviceFields(device) });
  updateHomeCapabilities(device);
  renderHomeExtensions(device);
  $("#home-detail-refresh").disabled = !device.enabled || device.busy || homeDevicePending(device);
  $("#home-detail-history").hidden = !currentHub?.resources?.some((resource) => resource.resource_id === homeResourceId(device));
  $("#home-detail-health").hidden = device.category !== "watch";
  homePrimaryControls(device).forEach((control) => {
    const section = [...$("#home-control-list").children].find((node) => node.dataset.controlAction === control.action);
    if (!section) return;
    const current = controlCurrentValue(device, control);
    const draft = homeDrafts.get(`${device.key}:${control.action}`);
    const busy = control.property === "favorite-fan-level" ? genericControlUnavailable(device, control) : homeUnavailable(device);
    if (control.kind === "toggle") {
      updatePowerButton(section.querySelector("button"), device);
      section.querySelector("output").textContent = displayStateValue("on", current);
    } else if (control.kind === "range") {
      const input = section.querySelector("input");
      const value = draft ?? current;
      if (value != null && Number.isFinite(Number(value))) input.value = value;
      input.disabled = busy;
      input.setAttribute("aria-valuetext", value == null ? "暂无当前值，拖动后点击应用" : `${input.value}${control.unit || ""}${draft !== undefined ? "，待应用" : ""}`);
      section.querySelector("output").textContent = value == null ? "待读取" : `${displayNumber(Number(value))}${control.unit || ""}${draft !== undefined ? " · 待应用" : ""}`;
      section.querySelector("button").disabled = busy || draft === undefined;
      const note = section.querySelector('[data-favorite-fan-note]');
      if (note) {
        const mode = controlCurrentValue(device, (device.controls || []).find(item => item.property === "mode") || {property: "mode", kind: "select", options: []});
        note.textContent = control.enabled === false ? control.reason || "此设备暂不支持设置最爱风速。"
          : `${String(mode).toLowerCase() === "favorite" ? "当前为最爱模式。" : "仅在最爱模式下生效，请先点击上方“最爱”。"} ${control.min}～${control.max}级，步长${control.step}；拖动后点击应用风速。保存档位不会自动切换模式。`;
      }
    } else if (control.kind === "select") {
      const matched = (control.options || []).find((option) => String(option.value) === String(current));
      section.querySelector("output").textContent = matched?.label || displayStateValue(control.property, device.state?.[control.property]);
      section.querySelectorAll("button").forEach((button) => {
        const active = button.dataset.value === String(current);
        button.classList.toggle("selected", active);
        button.setAttribute("aria-pressed", String(active));
        button.disabled = busy || active;
      });
    }
  });
  const result = homeMessages.get(device.key);
  const message = result?.message || (device.infrared_ac ? !device.enabled ? "请在关联遥控器的设备管理中授权。" : "空调指令已发送。" : !device.enabled ? "在下方设备管理中授权后，即可控制这个设备。" : resourceStale(device) ? "读数已过期，请先刷新状态后再操作。" : "调整后将确认设备的实际状态，多端会自动同步。");
  text("#home-detail-message", message);
  $("#home-detail-message").classList.toggle("bad", Boolean(result && !["pending", "confirmed", "accepted"].includes(result.outcome)));
}

function openHomeDevice(key) {
  if (key !== homeSelectedKey) cancelAcAdjustments();
  homeSelectedKey = key;
  showView("home");
  renderHomeCards();
  renderHomeDetail();
  $("#home-device-detail").scrollIntoView?.({ block: "start", behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
  const device = currentHome?.devices?.find((item) => item.key === key);
  if (homeExtensionKind(device)) loadHomeExtensions(key);
  if (device?.enabled && homeExtensionKind(device) !== "infrared") refreshHomeDevice(key);
}

function applyHomeResult(key, result) {
  const target = currentHome?.devices?.find((item) => item.key === key);
  if (result.extensions) acceptHomeExtension(target ? homeControlKey(target) : key, result.extensions);
  const device = currentHome?.devices?.find((item) => item.key === key);
  if (device && result.observed_at && Number.isFinite(new Date(result.observed_at).getTime()) && result.state && Object.keys(result.state).length && (!device.observed_at || new Date(result.observed_at) >= new Date(device.observed_at))) {
    Object.assign(device, { state: result.state, observed_at: result.observed_at, stale: false, online: true });
  }
  if (result.resource) {
    if (!currentHub) currentHub = { resources: [], events: [] };
    refreshHubView(result.resource);
  }
}

async function refreshHomeDevice(key) {
  const device = currentHome?.devices?.find((item) => item.key === key);
  if (device?.infrared_ac) return loadHomeExtensions(key, true);
  if (homePending.has(key)) return;
  homePending.add(key);
  renderHomeCards();
  renderHomeDetail();
  try {
    const result = await request(`/api/home/devices/${encodeURIComponent(key)}/state`, { timeoutMs: 45000 });
    applyHomeResult(key, result);
    homeMessages.set(key, { outcome: "confirmed", message: "设备状态已刷新。" });
  } catch (error) {
    if (error.payload) applyHomeResult(key, error.payload);
    homeMessages.set(key, { outcome: "failed", message: `状态暂未更新：${error.message}` });
  } finally {
    homePending.delete(key);
    renderHomeCards();
    renderHomeDetail();
  }
}

async function runHomeControl(key, action, value, options = {}) {
  const device = currentHome?.devices?.find((item) => item.key === key);
  if (!device || !action) return;
  const isExtension = /^(speaker_|ir_)/.test(action);
  // AC views share the gateway endpoint, while drafts/messages stay per AC.
  if (device.infrared_ac && (!['ir_ac_send', 'ir_ac_ai_permission'].includes(action) || value?.profile_key !== device.infrared_ac.profile_key)) return;
  const controlKey = homeControlKey(device);
  const capability = (isExtension ? extensionControls(homeDeviceExtension(device)) : [...(device.extra_controls || []), ...(device.capabilities_actions || [])]).find((item) => item.action === action);
  if (isExtension ? !capability || extensionUnavailable(device, capability) : capability ? genericControlUnavailable(device, capability) : homeUnavailable(device)) return;
  let confirmedByUser = false;
  if (capability?.requires_confirmation) {
    const description = capability.reason ? `\n${capability.reason}` : "";
    confirmedByUser = window.confirm(`确定对“${device.alias || device.name}”执行“${capability.label}”吗？${description}`);
    if (!confirmedByUser) return;
  }
  homePending.add(key);
  if (device.infrared_ac) homePending.add(controlKey);
  homeMessages.set(key, { outcome: "pending", message: "正在执行，等待设备确认…" });
  renderHomeCards();
  renderHomeDetail();
  const requestId = globalThis.crypto?.randomUUID?.() || `web-${Date.now()}-${Math.random().toString(36).slice(2)}`;
  let extensionReadback = false;
  try {
    const result = await request(`/api/home/devices/${encodeURIComponent(controlKey)}/control`, {
      method: "POST", body: JSON.stringify({ action, ...(value === undefined ? {} : { value }), request_id: requestId, ...(confirmedByUser ? { confirmed: true } : {}) }), timeoutMs: 45000,
    });
    applyHomeResult(key, result);
    extensionReadback = Boolean(result.extensions);
    const confirmed = result.outcome === "confirmed";
    const accepted = result.outcome === "accepted";
    homeMessages.set(key, { outcome: confirmed ? "confirmed" : accepted ? "accepted" : "unconfirmed", message: result.message || (confirmed ? "设备已确认操作。" : accepted ? "设备已接收指令，此动作没有可回读状态。" : "指令已发送，实际状态尚未确认，请刷新状态。") });
    if (confirmed || accepted) {
      for (const draftKey of homeDrafts.keys()) {
        if ((options.draftKeys ? options.draftKeys.includes(draftKey) : draftKey === `${key}:${action}` || draftKey.startsWith(`${key}:${action}:`)) &&
            (!options.draftValues || homeDrafts.get(draftKey) === options.draftValues[draftKey])) homeDrafts.delete(draftKey);
      }
    }
    showToast(homeMessages.get(key).message, !confirmed && !accepted);
    return result;
  } catch (error) {
    if (error.payload) applyHomeResult(key, error.payload);
    const outcome = error.payload?.outcome || "unconfirmed";
    const message = error.payload?.message || `操作结果未确认：${error.message}。请先刷新状态。`;
    homeMessages.set(key, { outcome, message });
    showToast(message, true);
    return error.payload || {outcome, message};
  } finally {
    homePending.delete(key);
    if (device.infrared_ac) homePending.delete(controlKey);
    if (isExtension && !extensionReadback) await loadHomeExtensions(key, true);
    renderHomeCards();
    renderHomeDetail();
  }
}

function filteredHubResources(resources) {
  const query = hubFilterSearch.trim().toLocaleLowerCase();
  return resources.filter((resource) => {
    if (hubFilterKind !== "all" && resource.kind !== hubFilterKind) return false;
    if (hubFilterRoom !== "all" && hubRoom(resource) !== hubFilterRoom) return false;
    if (hubFilterPurpose !== "all" && !hubResourceFields(resource).some((field) => ["usage", "records"].includes(hubFilterPurpose) ? fieldPresentation(field, resource.state).group === hubFilterPurpose : hubFilterPurpose === "telemetry" ? ["measurement", "counter"].includes(fieldRole(field, resource.state)) : fieldRole(field, resource.state) === hubFilterPurpose)) return false;
    if (!query) return true;
    return [resource.name, resource.resource_id, resource.location, resource.summary]
      .filter(Boolean)
      .some((value) => String(value).toLocaleLowerCase().includes(query));
  });
}

function hubRoom(resource) {
  const device = deviceForResource(resource);
  const assignment = device && homeAssignment(device);
  return resource.location || [assignment?.home_name, assignment?.room_name].filter(Boolean).join(" / ") || "未分组";
}

function fieldPriority(field, state) {
  if (["primary", "secondary", "detail"].includes(field.priority)) return field.priority;
  const role = fieldRole(field, state);
  return ["measurement", "counter", "state"].includes(role) ? "primary" : role === "setting" ? "secondary" : "detail";
}

function fieldCadence(field, resource) {
  const policy = field.refresh_policy || field.read_status?.refresh_policy;
  if (policy) return policy;
  const seconds = field.refresh_seconds ?? field.read_status?.refresh_seconds;
  if (typeof seconds === "number") {
    if (seconds === 0) return "按需读取";
    if (seconds >= 86400) return seconds === 86400 ? "每日更新" : `每${displayNumber(seconds / 86400)}天更新`;
    if (seconds >= 3600) return `每${displayNumber(seconds / 3600)}小时更新`;
    if (seconds >= 300 && seconds % 60 === 0) return `每${seconds / 60}分钟更新`;
    return `每${seconds}秒更新`;
  }
  if (fieldPriority(field, resource?.state) === "detail") return "低频资料 · 按需读取";
  if (deviceForResource(resource)) {
    const interval = Number(currentHub?.sampling?.interval_seconds);
    if (interval > 0) return `每${interval}秒更新`;
  }
  return "随数据源上报";
}

function appendHubFieldCards(container, resource, fields, { compact = false, source = false, showCadence = true } = {}) {
  fields.forEach((field) => {
    const card = homeNode("div", `metric-card${compact ? " compact" : ""}`);
    card.dataset.layerField = field.key;
    card.dataset.fieldRole = fieldRole(field, resource.state);
    const raw = stateFieldValue(resource.state, field);
    const value = homeNode("strong", typeof raw === "number" && !identityField(field) && field.type !== "enum" ? "metric-value" : "");
    const diagnostic = fieldReadStatus(field, resource.state, resource);
    if (typeof raw === "number" && !identityField(field) && field.type !== "enum" && diagnostic.status !== "sensitive") {
      const presentation = metricPresentation(field, raw);
      value.textContent = presentation.value;
      if (presentation.unit) value.append(homeNode("small", "metric-unit", presentation.unit));
    } else value.textContent = displayPropertyValue(field, resource.state);
    card.append(homeNode("span", "metric-label", field.label || field.key), value);
    if (source) card.append(homeNode("small", "hub-field-origin-note", `${resource.name} · ${hubRoom(resource)}`));
    if (diagnostic.status !== "ok") {
      card.append(homeNode("small", "metric-read-note", `${raw != null && diagnostic.status !== "sensitive" ? "上次读数 · " : ""}${diagnostic.label}`));
      if (diagnostic.message && showCadence) card.append(homeNode("small", "metric-read-note", diagnostic.message));
      card.classList.add("has-stale-field");
    }
    if (diagnostic.source_updated_at && diagnostic.status !== "sensitive") {
      card.append(homeNode("small", "metric-source-note", `云端属性更新 ${formatTime(diagnostic.source_updated_at)} · 实时性未核实`));
    }
    const observed = diagnostic.observed_at || field.observed_at || (diagnostic.status === "ok" ? resource.observed_at : "");
    if (showCadence) card.append(homeNode("small", "hub-field-cadence", `${fieldCadence(field, resource)}${observed ? ` · 最近读取 ${formatTime(observed)}` : " · 尚无读取记录"}`));
    container.append(card);
  });
}

function renderHubBrowseFilters(resources) {
  const roomSelect = $("#hub-filter-room");
  const rooms = [...new Set(resources.map(hubRoom))];
  if (hubFilterRoom !== "all" && !rooms.includes(hubFilterRoom)) hubFilterRoom = "all";
  roomSelect.replaceChildren(new Option("全部房间", "all"), ...rooms.map((room) => new Option(room, room)));
  roomSelect.value = hubFilterRoom;
}

function appendTypedFieldNotes(container, field, resource) {
  const diagnostic = fieldReadStatus(field, resource.state, resource);
  const raw = stateFieldValue(resource.state, field);
  if (diagnostic.status !== "ok") {
    container.classList.add("has-stale-field");
    container.append(homeNode("small", "metric-read-note", `${raw != null && !["not_readable", "unknown"].includes(diagnostic.status) ? "上次读数 · " : ""}${diagnostic.label}`));
  }
  if (diagnostic.source_updated_at) container.append(homeNode("small", "metric-source-note", `云端属性更新 ${formatTime(diagnostic.source_updated_at)} · 实时性未核实`));
  const observed = diagnostic.observed_at || field.observed_at || (diagnostic.status === "ok" ? resource.observed_at : "");
  container.append(homeNode("small", "hub-field-cadence", `${fieldCadence(field, resource)}${observed ? ` · 最近读取 ${formatTime(observed)}` : " · 尚无读取记录"}`));
}

function appendTypedMetrics(container, resource, fields) {
  fields.forEach((field) => {
    const presentation = fieldPresentation(field, resource.state);
    const raw = stateFieldValue(resource.state, field);
    const card = homeNode("article", "metric-card hub-typed-metric");
    card.dataset.layerField = field.key;
    card.dataset.valueKind = presentation.kind;
    const heading = homeNode("div", "hub-metric-heading");
    heading.append(homeNode("span", "metric-label", field.label || field.key));
    if (["current_count", "total_count", "cumulative_count"].includes(presentation.kind)) heading.append(homeNode("small", "hub-kind-label", { current_count: "当前数量", total_count: "数量总计", cumulative_count: "累计用量" }[presentation.kind]));
    const value = homeNode("strong", "metric-value");
    if (typeof raw === "number" && !["duration", "countdown"].includes(presentation.kind)) {
      const formatted = metricPresentation(field, raw);
      value.textContent = formatted.value;
      if (formatted.unit) value.append(homeNode("small", "metric-unit", formatted.unit));
    } else value.textContent = typedFieldValue(field, resource);
    card.append(heading, value);
    if (presentation.kind === "percentage" && typeof raw === "number" && raw >= 0 && raw <= 100) {
      const meter = homeNode("meter", "hub-percentage-meter");
      meter.min = 0; meter.max = 100; meter.value = raw;
      meter.setAttribute("aria-label", `${field.label || field.key} ${displayNumber(raw)}%`);
      meter.textContent = `${displayNumber(raw)}%`;
      card.append(meter);
    }
    appendTypedFieldNotes(card, field, resource);
    container.append(card);
  });
}

function appendTypedRows(container, resource, fields, { states = false } = {}) {
  fields.forEach((field) => {
    const presentation = fieldPresentation(field, resource.state);
    const row = homeNode("div", `hub-value-row${states ? " hub-state-row" : ""}`);
    row.dataset.layerField = field.key;
    row.dataset.valueKind = presentation.kind;
    const heading = homeNode("div", "hub-value-identity");
    heading.append(homeNode("span", "", field.label || field.key));
    if (["duration", "countdown"].includes(presentation.kind)) heading.append(homeNode("small", "hub-kind-label", presentation.kind === "countdown" ? "倒计时" : "运行时长"));
    const value = homeNode("strong", states && ["boolean", "enum"].includes(presentation.kind) ? "hub-state-badge" : "hub-value-text", typedFieldValue(field, resource));
    const raw = stateFieldValue(resource.state, field);
    if (presentation.kind === "boolean") value.dataset.boolean = raw === true ? "on" : raw === false ? "off" : "unknown";
    row.append(heading, value);
    const notes = homeNode("div", "hub-value-notes");
    appendTypedFieldNotes(notes, field, resource);
    row.append(notes);
    container.append(row);
  });
}

function structuredFieldPreview(value, resource, basePath, depth = 0) {
  const list = homeNode("div", "hub-record-values");
  const fields = hubResourceFields(resource);
  const entries = value && typeof value === "object" ? Object.entries(value) : [["内容", value]];
  const safe = entries.filter(([key]) => {
    const path = [...basePath, Array.isArray(value) ? Number(key) : key];
    return !/token|password|passwd|secret|credential|authorization|cookie|密钥|密码/i.test(key) && !fields.some((field) => fieldReadStatus(field, resource.state, resource).status === "sensitive" && JSON.stringify((field.path || [field.key]).map(String)) === JSON.stringify(path.map(String)));
  });
  safe.slice(0, 8).forEach(([key, item]) => {
    const path = [...basePath, Array.isArray(value) ? Number(key) : key];
    const field = fields.find((candidate) => JSON.stringify((candidate.path || [candidate.key]).map(String)) === JSON.stringify(path.map(String)));
    const entry = homeNode("div", "hub-record-entry");
    const term = homeNode("span", "hub-record-key", field?.label || (Array.isArray(value) ? `记录 ${Number(key) + 1}` : stateLabels[key] || ({ name: "名称", value: "数值", date: "日期", timestamp: "时间", time: "时间", type: "类型", message: "内容", status: "状态", count: "数量" })[key] || key));
    const detail = homeNode("span", "hub-record-value");
    if (item && typeof item === "object") {
      const details = homeNode("details", "hub-record-nested");
      details.append(homeNode("summary", "", `${Array.isArray(item) ? item.length : Object.keys(item).length} 项明细`));
      if (depth < 2) details.append(structuredFieldPreview(item, resource, path, depth + 1));
      else details.append(homeNode("span", "hub-muted", "层级较深，已收起嵌套内容。"));
      detail.append(details);
    } else detail.textContent = field ? typedFieldValue(field, resource) : typeof item === "string" && /^[\s]*[\[{]/.test(item) ? "结构化文本 · 已收起原始内容" : String(displayStateValue(key, item)).slice(0, 240);
    entry.append(term, detail);
    list.append(entry);
  });
  if (!safe.length) list.append(homeNode("div", "hub-muted", entries.length ? "敏感内容已隐藏" : "暂无记录"));
  if (safe.length > 8) list.append(homeNode("div", "hub-record-overflow", `另有 ${safe.length - 8} 项，预览显示前 8 项。`));
  return list;
}

function appendTypedRecords(container, resource, fields) {
  fields.forEach((field) => {
    const raw = stateFieldValue(resource.state, field);
    const record = homeNode("article", "hub-record-card");
    record.dataset.layerField = field.key;
    const heading = homeNode("div", "hub-record-heading");
    heading.append(homeNode("h4", "", field.label || field.key), homeNode("span", "hub-kind-label", raw && typeof raw === "object" ? `${Array.isArray(raw) ? raw.length : Object.keys(raw).length} 项` : "事件记录"));
    record.append(heading);
    if (raw && typeof raw === "object") record.append(structuredFieldPreview(raw, resource, field.path || [field.key]));
    else record.append(homeNode("p", "hub-record-text", typedFieldValue(field, resource)));
    appendTypedFieldNotes(record, field, resource);
    container.append(record);
  });
}

function renderHubContentFilter(resource) {
  const fields = normalHubFields(resource);
  const groups = Object.fromEntries(Object.keys(hubGroupLabels).map((group) => [group, fields.filter((field) => fieldPresentation(field, resource.state).group === group)]));
  const selected = hubContentGroups.get(resource.resource_id) || "all";
  const tabs = $("#hub-category-tabs");
  tabs.replaceChildren();
  [["all", "全部内容"], ...Object.entries(hubGroupLabels)].forEach(([group, label]) => {
    const count = group === "all" ? fields.length : groups[group].length;
    const button = homeNode("button", group === selected ? "selected" : "");
    button.type = "button"; button.dataset.contentCategory = group;
    button.setAttribute("aria-pressed", String(group === selected));
    button.disabled = group !== "all" && !count;
    button.append(homeNode("span", "", label), homeNode("small", "", String(count)));
    tabs.append(button);
  });
  $$("#hub-detail [data-content-group]").forEach((section) => {
    const group = section.dataset.contentGroup;
    section.hidden = !groups[group].length || selected !== "all" && selected !== group;
    if (selected === group && section.tagName === "DETAILS") section.open = true;
  });
  $(".hub-typed-content").hidden = !["measurements", "usage", "states"].some((group) => groups[group].length && (selected === "all" || selected === group));
  $("#hub-detail .hub-detail-grid").classList.toggle("hub-history-only", $(".hub-typed-content").hidden);
}

function renderHubDiagnosticSummary(resource, fields) {
  const counts = { available: 0, unavailable: 0, not_readable: 0, sensitive: 0 };
  fields.forEach((field) => {
    const status = fieldReadStatus(field, resource.state, resource).status;
    if (status === "sensitive") counts.sensitive += 1;
    else if (["parameter", "event"].includes(field.role) || status === "not_readable") counts.not_readable += 1;
    else if (status === "ok" && stateFieldValue(resource.state, field) != null) counts.available += 1;
    else counts.unavailable += 1;
  });
  const summary = $("#hub-diagnostic-summary");
  summary.replaceChildren(homeNode("span", "hub-diagnostic-label", "读取情况"));
  [["available", "正常"], ["unavailable", "待更新"], ["not_readable", "事件 / 动作"], ["sensitive", "已隐藏"]].forEach(([status, label]) => {
    if (!counts[status]) return;
    const button = homeNode("button", `hub-diagnostic-chip${status === "unavailable" ? " needs-attention" : ""}`, `${label} ${counts[status]}`);
    button.type = "button"; button.dataset.diagnosticFilter = status === "unavailable" ? "attention" : status;
    button.setAttribute("aria-label", `查看${label}的 ${counts[status]} 个字段`);
    summary.append(button);
  });
}

function renderHubDetailLayers(resource) {
  const allFields = hubResourceFields(resource);
  const fields = normalHubFields(resource);
  const groups = Object.fromEntries(Object.keys(hubGroupLabels).map((group) => [group, fields.filter((field) => fieldPresentation(field, resource.state).group === group)]));
  const readings = [...groups.measurements, ...groups.usage].filter((field) => stateFieldValue(resource.state, field) != null);
  text("#hub-layer-counts", `${readings.length} 项已有读数 · ${groups.states.length} 项运行状态 · 按内容分类，读取情况单独标注`);
  [["state", groups.measurements], ["usage", groups.usage]].forEach(([name, selected]) => {
    const grid = $(`#hub-${name}-grid`);
    grid.replaceChildren();
    appendTypedMetrics(grid, resource, selected);
  });
  const operating = $("#hub-operating-states");
  operating.replaceChildren(homeNode("h3", "", "运行状态"));
  const stateList = homeNode("div", "hub-value-list");
  appendTypedRows(stateList, resource, groups.states, { states: true });
  operating.append(stateList);
  [["settings", groups.settings], ["identity", groups.profile], ["records", groups.records]].forEach(([name, selected]) => {
    const container = $(`#hub-${name}-grid`);
    const expanded = new Set([...container.querySelectorAll("details[open]")].map((item) => item.parentElement.previousElementSibling?.textContent));
    container.replaceChildren();
    if (name === "records") appendTypedRecords(container, resource, selected);
    else appendTypedRows(container, resource, selected);
    container.querySelectorAll("details").forEach((item) => { if (expanded.has(item.parentElement.previousElementSibling?.textContent)) item.open = true; });
    text(`#hub-${name}-count`, `${selected.length} 项`);
  });
  $("#hub-settings-control").hidden = !deviceForResource(resource);
  text("#hub-advanced-count", `${allFields.length} 个字段`);
  renderHubContentFilter(resource);
  renderHubDiagnosticSummary(resource, allFields);
}

function hubResourceFields(resource) {
  if (!resource) return [];
  const fields = new Map((deviceForResource(resource)?.properties || []).map((property) => [property.key, { ...property, path: [property.key] }]));
  (resource.fields || []).forEach((field) => fields.set(field.key, { ...fields.get(field.key), ...field, read_status: undefined }));
  Object.keys(resource.state || {}).forEach((key) => {
    const metric = metricDefinitions.find((item) => item.aliases.includes(key));
    if (!fields.has(key)) fields.set(key, { key, path: [key], label: metric?.label || stateLabels[key] || key, unit: metric?.unit || "", readable: true, writable: false, source: resource.source, observed_at: resource.observed_at });
  });
  return [...fields.values()];
}

function renderHubFields(resource) {
  const list = $("#hub-field-list");
  if (!list) return;
  const fields = hubResourceFields(resource);
  const filters = hubFieldFilters.get(resource.resource_id) || { status: "all", type: "all", source: "all", role: "all", search: "" };
  const typeLabels = { number: "数值", string: "文字", boolean: "开关", enum: "枚举", array: "列表", object: "数据组", null: "空值", unknown: "类型待确认" };
  const types = [...new Set(fields.map((field) => fieldType(field, resource.state)))];
  const sources = [...new Set(fields.map((field) => fieldReadStatus(field, resource.state, resource).source || "unknown"))];
  const roles = [...new Set(fields.map((field) => fieldRole(field, resource.state)))];
  const fill = (selector, values, title, label, selected) => {
    const select = $(selector);
    select.replaceChildren(new Option(title, "all"), ...values.map((value) => new Option(label(value), value)));
    select.value = values.includes(selected) ? selected : "all";
    return select.value;
  };
  filters.type = fill("#hub-field-type", types, "全部数据类型", (type) => typeLabels[type] || type, filters.type);
  filters.source = fill("#hub-field-source", sources, "全部来源", (source) => source === "unknown" ? "来源未标注" : fieldSourceLabel(source), filters.source);
  filters.role = fill("#hub-field-role", roles, "全部字段用途", (role) => fieldRoleLabels[role], filters.role || "all");
  $("#hub-field-status").value = filters.status;
  if (document.activeElement !== $("#hub-field-search")) $("#hub-field-search").value = filters.search;
  hubFieldFilters.set(resource.resource_id, filters);
  const available = (field) => fieldReadStatus(field, resource.state, resource).status === "ok" && stateFieldValue(resource.state, field) != null;
  const query = filters.search.trim().toLocaleLowerCase();
  const visible = fields.filter((field) => {
    const diagnostic = fieldReadStatus(field, resource.state, resource);
    return (filters.status === "all" || (filters.status === "available" ? available(field) : filters.status === "unavailable" ? !available(field) && !["parameter", "event"].includes(fieldRole(field, resource.state)) : filters.status === "attention" ? !available(field) && !["sensitive", "not_readable"].includes(diagnostic.status) && !["parameter", "event"].includes(field.role) : diagnostic.status === filters.status)) &&
      (filters.type === "all" || fieldType(field, resource.state) === filters.type) &&
      (filters.source === "all" || (diagnostic.source || "unknown") === filters.source) &&
      (filters.role === "all" || fieldRole(field, resource.state) === filters.role) &&
      (!query || [field.label, field.key, field.semantic, fieldSourceLabel(diagnostic.source)].some((value) => String(value || "").toLocaleLowerCase().includes(query)));
  });
  text("#hub-field-count", `${fields.length} 个字段 · ${fields.filter(available).length} 个当前有读数 · 显示 ${visible.length} 个`);
  list.replaceChildren();
  const roleOrder = Object.keys(fieldRoleLabels);
  visible.sort((a, b) => roleOrder.indexOf(fieldRole(a, resource.state)) - roleOrder.indexOf(fieldRole(b, resource.state)));
  let previousRole = "";
  visible.forEach((field) => {
    const role = fieldRole(field, resource.state);
    if (role !== previousRole) {
      const heading = homeNode("h4", "hub-field-role-heading", fieldRoleLabels[role]);
      if (role === "identity") heading.append(homeNode("small", "", "用于识别设备，不参与趋势或数值比较"));
      list.append(heading);
      previousRole = role;
    }
    const diagnostic = fieldReadStatus(field, resource.state, resource);
    const row = homeNode("article", "hub-field-row");
    row.dataset.fieldKey = field.key;
    row.dataset.fieldStatus = diagnostic.status;
    const identity = homeNode("div", "hub-field-identity");
    identity.append(homeNode("strong", "", field.label || field.key), homeNode("small", "", field.key), homeNode("span", "", [typeLabels[fieldType(field, resource.state)] || "类型待确认", field.unit, field.writable ? "可设置" : ""].filter(Boolean).join(" · ")));
    const value = homeNode("div", "hub-field-value");
    value.append(homeNode("small", "hub-field-caption", "当前值"), homeNode("strong", "", displayPropertyValue(field, resource.state)));
    const previous = stateFieldValue(resource.state, field) != null && !["ok", "sensitive"].includes(diagnostic.status);
    if (previous) value.append(homeNode("small", "", "上次读数，当前尚未更新"));
    const status = homeNode("div", "hub-field-diagnostic");
    const badge = homeNode("span", `status-pill ${diagnostic.status === "ok" ? "ok" : ["unavailable", "offline", "stale"].includes(diagnostic.status) ? "bad" : ""}`, diagnostic.label);
    status.append(badge);
    if (diagnostic.message && diagnostic.status !== "sensitive") status.append(homeNode("small", "", diagnostic.message));
    if (diagnostic.code != null && diagnostic.code !== 0) status.append(homeNode("small", "field-error-code", `服务返回码：${diagnostic.code}`));
    const origin = homeNode("div", "hub-field-origin");
    origin.append(homeNode("span", "", fieldSourceLabel(diagnostic.source)));
    origin.append(homeNode("small", "", fieldCadence(field, resource)));
    if (field.source_field || diagnostic.source_field) origin.append(homeNode("small", "", `来源字段：${field.source_field || diagnostic.source_field}`));
    if (diagnostic.source_updated_at) origin.append(homeNode("time", "", `云端属性更新 ${formatTime(diagnostic.source_updated_at)} · 非采样时间`));
    const observed = diagnostic.observed_at || field.observed_at || (diagnostic.status === "ok" ? resource.observed_at : "");
    if (observed) origin.append(homeNode("time", "", `读数 ${formatTime(observed)}`));
    if (diagnostic.attempted_at) origin.append(homeNode("time", "", `尝试 ${formatTime(diagnostic.attempted_at)}`));
    if (!observed && !diagnostic.attempted_at) origin.append(homeNode("small", "", "暂无读取记录"));
    row.append(identity, value, status, origin);
    list.append(row);
  });
  if (!visible.length) list.append(homeNode("p", "hub-history-empty", fields.length ? "没有符合筛选条件的字段。" : "数据源尚未上报字段，收到数据后会自动建立字段目录。"));
}

function renderHubDetail(resource) {
  if (!resource) {
    $("#hub-detail").hidden = true;
    return;
  }
  $("#hub-detail").hidden = false;
  text("#hub-detail-title", resource.name);
  text("#hub-detail-id", [hubKinds[resource.kind] || "数据源", resource.location].filter(Boolean).join(" · "));
  const stale = resourceStale(resource);
  setPill("#hub-detail-state-pill", stale ? "数据已过期" : "数据有效", stale ? "bad" : "ok");
  const readAt = resource.observed_at || resource.received_at;
  text("#hub-detail-time", readAt ? `${stale ? "最近有效读数" : "最新读数"} · ${formatTime(readAt)}` : "尚无有效读数，可在字段目录中查看读取情况。");
  renderHubDetailLayers(resource);
  $("#hub-detail-control").hidden = !deviceForResource(resource);
  renderHubFields(resource);
}

function storageTime(value) {
  const timestamp = value ? new Date(value).getTime() : NaN;
  return Number.isFinite(timestamp) ? new Intl.DateTimeFormat("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false }).format(timestamp) : "尚无记录";
}

function storageSize(value) {
  if (!Number.isFinite(Number(value)) || value == null) return "—";
  const bytes = Math.max(0, Number(value));
  const scale = bytes ? Math.min(3, Math.floor(Math.log(bytes) / Math.log(1024))) : 0;
  return `${displayNumber(bytes / 1024 ** scale)} ${["B", "KB", "MB", "GB"][Math.max(0, scale)]}`;
}

function renderHubStorageSampling() {
  const sampling = currentHub?.sampling || {};
  const sources = Object.entries(sampling.sources || {}).map(([key, source]) => {
    const label = { cloud: "米家云端", router: "路由器本地" }[key] || "设备来源";
    return `${label}：${source.last_error ? "读取待恢复" : source.last_success_at ? `最近成功 ${storageTime(source.last_success_at)}` : "等待有效采样"}`;
  });
  text("#hub-storage-sampling", sources.length ? sources.join("；") : sampling.last_error ? "最近采样有异常，已有读数保留原始时间；可在设备详情查看读取诊断。" : sampling.last_sample_at ? `最近完成采样 ${storageTime(sampling.last_sample_at)}；读数是否有效以各字段的成功时间为准。` : "等待设备提供有效读数。");
}

function renderHubStorage() {
  const database = hubStorage?.database || {};
  const stored = hubStorage?.history || {};
  const policy = hubStorage?.policy || {};
  const backup = hubStorage?.backup || {};
  const maintenance = hubStorage?.maintenance || {};
  const hasStatus = typeof stored.event_count === "number";
  const faults = [hubStorageError, backup.last_error ? `备份暂未成功：${backup.last_error}` : "", maintenance.last_error ? `维护暂未成功：${maintenance.last_error}` : ""].filter(Boolean);
  const busy = Boolean(hubStoragePromise);
  $("#hub-storage-refresh").disabled = busy;
  setPill("#hub-storage-pill", faults.length ? "需要关注" : busy ? "正在读取" : backup.last_success_at ? "已备份" : hasStatus ? "等待备份" : "待查看", faults.length ? "bad" : backup.last_success_at ? "ok" : "");
  text("#hub-storage-summary", hasStatus ? `${stored.event_count.toLocaleString("zh-CN")} 条记录 · ${storageSize(Number(database.bytes || 0) + Number(database.wal_bytes || 0))}` : "查看保存与备份状态");
  text("#hub-storage-message", faults.length ? `${faults.join("；")}${hasStatus && hubStorageError ? "。下方保留上次读取的状态。" : ""}` : busy ? "正在读取存储状态…" : hasStatus ? `状态更新于 ${storageTime(hubStorageLoadedAt)}。健康记录由独立健康中心管理。` : "存储状态尚未提供，可稍后刷新。");
  $("#hub-storage-message").classList.toggle("bad", Boolean(faults.length));
  text("#hub-storage-events", hasStatus ? stored.event_count.toLocaleString("zh-CN") : "—");
  text("#hub-storage-resources", hasStatus ? `${stored.resource_count ?? 0} 个已保存数据源 · ${stored.enabled_resource_count ?? 0} 个已启用` : "等待统计");
  text("#hub-storage-size", database.bytes != null ? storageSize(Number(database.bytes) + Number(database.wal_bytes || 0)) : "—");
  text("#hub-storage-space", database.free_bytes != null ? `其中 ${storageSize(database.free_bytes)} 可由后续写入复用` : "含待写入数据");
  text("#hub-storage-backup", backup.last_success_at ? storageTime(backup.last_success_at) : "尚无成功备份");
  text("#hub-storage-backup-note", backup.last_success_at ? `已校验备份 · ${storageSize(backup.last_size_bytes)}` : hasStatus ? "等待自动备份完成" : "等待备份信息");
  text("#hub-storage-coverage", stored.oldest_observed_at ? `${storageTime(stored.oldest_observed_at)} — ${storageTime(stored.newest_observed_at)}（不同设备的覆盖范围可能不同）` : "尚无历史记录");
  text("#hub-storage-retention", policy.max_events_per_resource ? `每个数据源最多 ${policy.max_events_per_resource.toLocaleString("zh-CN")} 条${policy.max_age_days ? `，最长 ${policy.max_age_days} 天` : ""}；达到任一上限后逐步清理较早历史，当前状态单独保存。` : "等待保留规则");
  text("#hub-storage-backup-policy", policy.backup_interval_hours ? `每 ${policy.backup_interval_hours} 小时自动备份，保留最近 ${policy.backup_keep_daily ?? "—"} 个每日备份${policy.backup_keep_weekly ? `，另保留 ${policy.backup_keep_weekly} 个每周备份` : ""}。` : "等待备份规则");
  text("#hub-storage-maintenance", maintenance.running ? "正在维护，设备读数继续更新。" : maintenance.last_success_at ? `最近成功 ${storageTime(maintenance.last_success_at)}${stored.pending_compression ? `，还有 ${stored.pending_compression.toLocaleString("zh-CN")} 条历史等待整理` : ""}` : maintenance.last_attempt_at ? `最近尝试 ${storageTime(maintenance.last_attempt_at)}` : "等待首次维护");
  renderHubStorageSampling();
}

async function loadHubStorage(force = false) {
  if (hubStoragePromise) return hubStoragePromise;
  if (!force && hubStorageLoadedAt && Date.now() - hubStorageLoadedAt < 60000) return;
  hubStoragePromise = (async () => {
    try {
      const result = await request("/api/hub/storage", { timeoutMs: 15000 });
      if (typeof result.history?.event_count !== "number") throw new Error("服务尚未提供存储统计");
      hubStorage = result;
      hubStorageLoadedAt = Date.now();
      hubStorageError = "";
    } catch (error) {
      hubStorageError = `存储状态暂不可用：${error.message}`;
    }
  })().finally(() => {
    hubStoragePromise = null;
    renderHubStorage();
  });
  renderHubStorage();
  return hubStoragePromise;
}

function renderHub(hub) {
  currentHub = hub;
  renderOverviewSnapshot(hub);
  syncHomeResources();
  renderHomeCards();
  renderHomeDetail();
  const resources = hub.resources || [];
  renderHubBrowseFilters(resources);
  if (currentHubSelection && !resources.some((item) => item.resource_id === currentHubSelection)) {
    currentHubSelection = "";
    hubHistoryRequest += 1;
    hubHistoryLoading = false;
    hubHistoryKey = "";
    hubHistoryNotice = "";
    renderHubDetail(null);
  }
  const online = resources.filter((item) => !resourceStale(item));
  const environment = resources.filter((item) => metricDefinitions.some((metric) => environmentKeys.has(metric.key) && metricValue(item.state, metric, hubResourceFields(item)) !== null));
  text("#hub-resource-count", String(resources.length));
  text("#hub-online-count", String(online.length));
  text("#hub-environment-count", String(environment.length));
  text("#hub-stale-count", String(resources.length - online.length));
  const sampling = hub.sampling || {};
  renderHubStorageSampling();
  const interval = Number(sampling.interval_seconds);
  text("#hub-sampling-status", hubSnapshotFailed ? "连接暂不可用，显示最近收到的数据" : sampling.last_error ? "设备采样暂未成功，保留最近读数并自动重试" : sampling.sampling ? "正在更新设备数据…" : interval > 0 ? `设备每 ${interval >= 60 ? `${displayNumber(interval / 60)} 分钟` : `${interval} 秒`}自动采样，多端同步` : "数据源更新后会自动同步到所有终端");
  const sampledAt = sampling.last_sample_at || resources.map((item) => item.received_at).filter(Boolean).sort().at(-1);
  text("#hub-last-sample", sampledAt ? `${hubStreamConnected && !hubSnapshotFailed ? "最近采样" : "当前显示的采样"} ${formatTime(sampledAt)}` : "等待第一条设备读数");
  renderEnvironmentOverview(environment);

  const list = $("#hub-resource-list");
  const visibleResources = filteredHubResources(resources);
  list.replaceChildren();
  $("#hub-resource-empty").hidden = Boolean(resources.length);
  $("#hub-resource-filter-empty").hidden = !resources.length || Boolean(visibleResources.length);
  const roomGroups = new Map();
  visibleResources.forEach((resource) => {
    const room = hubRoom(resource);
    if (!roomGroups.has(room)) {
      const group = homeNode("section", "hub-room-group");
      group.append(homeNode("h3", "hub-room-heading", `${room} · ${visibleResources.filter((item) => hubRoom(item) === room).length} 个数据源`));
      const devices = homeNode("div", "hub-room-devices");
      group.append(devices);
      list.append(group);
      roomGroups.set(room, devices);
    }
    const row = document.createElement("div");
    row.className = `hub-resource-card${resourceStale(resource) ? " is-stale" : ""}${resource.resource_id === currentHubSelection ? " selected" : ""}`;
    row.dataset.resourceId = resource.resource_id;
    row.setAttribute("role", "group");
    row.addEventListener("click", () => selectHubResource(resource.resource_id, true));
    row.setAttribute("aria-label", `${resource.name}，数据与历史`);
    const head = document.createElement("div");
    head.className = "hub-resource-head";
    const identity = document.createElement("div");
    const name = document.createElement("strong");
    name.textContent = resource.name;
    const meta = document.createElement("span");
    meta.textContent = [hubKinds[resource.kind] || "数据源", resource.location].filter(Boolean).join(" · ");
    identity.append(name, meta);
    const state = document.createElement("span");
    state.className = `status-pill ${resourceStale(resource) ? "bad" : "ok"}`;
    state.textContent = resourceStale(resource) ? "读数已过期" : "数据有效";
    head.append(identity, state);
    const metrics = document.createElement("div");
    metrics.className = "resource-metrics";
    const fields = hubResourceFields(resource);
    const frequent = frequentResourceFields(resource);
    appendHubFieldCards(metrics, resource, frequent, { compact: true, showCadence: false });
    const states = normalHubFields(resource).filter((field) => fieldPresentation(field, resource.state).group === "states" && stateFieldValue(resource.state, field) != null).slice(0, 2);
    const statusLine = homeNode("p", "hub-device-state-line", states.map((field) => `${field.label} ${typedFieldValue(field, resource)}`).join(" · "));
    if (!frequent.length) metrics.append(homeNode("p", "hub-history-empty", states.length ? "运行状态见下方；低频资料在详情中展开。" : `${fields.length} 个字段；打开查看设备档案与读取情况。`));
    const footer = document.createElement("div");
    footer.className = "hub-resource-footer";
    const at = document.createElement("time");
    const cadences = [...new Set(frequent.map((field) => fieldCadence(field, resource)))];
    const cadence = cadences.length === 1 ? cadences[0] : cadences.length > 1 ? "按字段频率更新" : "";
    at.textContent = [cadence, resource.observed_at || resource.received_at ? `源更新 ${formatTime(resource.observed_at || resource.received_at)}` : "尚无有效读数"].filter(Boolean).join(" · ");
    const link = homeNode("button", "hub-trend-link", "查看趋势 →");
    link.type = "button";
    link.setAttribute("aria-label", `${resource.name}，查看数据与历史`);
    link.addEventListener("click", (event) => {
      event.stopPropagation();
      selectHubResource(resource.resource_id, true);
    });
    footer.append(at, link);
    const device = deviceForResource(resource);
    if (device) {
      const control = homeNode("button", "text-button resource-control-link", "控制设备 →");
      control.type = "button";
      control.setAttribute("aria-label", `控制${device.alias || device.name}`);
      control.addEventListener("click", (event) => { event.stopPropagation(); openHomeDevice(device.key); });
      footer.append(control);
    }
    row.append(head, metrics, statusLine, footer);
    roomGroups.get(room).append(row);
  });

  const events = hub.events || [];
  const eventList = $("#hub-event-list");
  eventList.replaceChildren();
  $("#hub-event-empty").hidden = Boolean(events.length);
  events.forEach((event) => {
    const row = document.createElement("div");
    row.className = "audit-row";
    const dot = document.createElement("i");
    dot.className = "ok";
    const name = document.createElement("span");
    name.textContent = resources.find((item) => item.resource_id === event.resource_id)?.name || "数据源";
    const summary = document.createElement("small");
    const eventResource = resources.find((item) => item.resource_id === event.resource_id);
    summary.textContent = event.state ? stateSummary(event.state, eventResource ? hubResourceFields(eventResource) : []) : "设备状态已更新";
    const at = document.createElement("time");
    at.textContent = formatTime(event.received_at);
    row.append(dot, name, summary, at);
    eventList.append(row);
  });

  const selected = resources.find((item) => item.resource_id === currentHubSelection);
  if (selected) {
    renderHubDetail(selected);
    loadHubHistory(selected);
  }
}

function renderEnvironmentOverview(resources) {
  const container = $("#overview-environment");
  container.replaceChildren();
  if (!resources.length) {
    const empty = document.createElement("p");
    empty.className = "hub-history-empty";
    empty.textContent = "等待环境设备的第一条读数，采样后会在这里自动呈现。";
    container.append(empty);
    return;
  }
  resources.slice(0, 3).forEach((resource) => {
    const section = document.createElement("div");
    section.className = resourceStale(resource) ? "is-stale" : "";
    const title = document.createElement("p");
    title.className = "environment-source";
    title.textContent = `${resource.name} · ${resourceStale(resource) ? "读数已过期" : "最近采样"} ${formatTime(resource.observed_at || resource.received_at)}`;
    const metrics = document.createElement("div");
    metrics.className = "resource-metrics";
    const fields = hubResourceFields(resource);
    const state = Object.fromEntries(metricDefinitions.filter((metric) => environmentKeys.has(metric.key) && metricValue(resource.state, metric, fields) !== null).flatMap((metric) => metric.aliases.filter((alias) => resource.state?.[alias] != null).map((alias) => [alias, resource.state[alias]])));
    appendMetricCards(metrics, state, { compact: true, includeDetails: false, properties: fields });
    section.append(title, metrics);
    const device = deviceForResource(resource);
    if (device) {
      const control = homeNode("button", "text-button overview-device-control", "控制设备 →");
      control.type = "button";
      control.addEventListener("click", () => openHomeDevice(device.key));
      section.append(control);
    }
    container.append(section);
  });
}

async function selectHubResource(resourceId, scrollToDetail = false) {
  currentHubSelection = resourceId;
  const resource = (currentHub?.resources || []).find((item) => item.resource_id === resourceId);
  if (!resource) return;
  renderHubDetail(resource);
  if (scrollToDetail) $("#hub-detail").scrollIntoView?.({ block: "start", behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
  $$(".hub-resource-card").forEach((card) => card.classList.toggle("selected", card.dataset.resourceId === resourceId));
  await loadHubHistory(resource);
}

async function loadHubHistory(resource, force = false, earlier = false) {
  const resourceId = resource.resource_id;
  const scope = `${resourceId}:${hubHistoryHours}`;
  const latestStamp = `${resource.received_at || ""}:${resource.observed_at || ""}`;
  const revision = resource.history_revision ?? 0;
  const key = `${scope}:${latestStamp}:${revision}`;
  if (earlier && (!hubHistoryQuery.next_cursor || hubHistory.length >= 5000)) return;
  if (!force && hubHistoryLoading && hubHistoryScope === scope) return;
  if (!force && hubHistoryKey === key) return;
  const previousKey = hubHistoryKey;
  hubHistoryKey = key;
  const requestId = ++hubHistoryRequest;
  const backfilled = hubHistoryScope === scope && hubHistoryLatestStamp === latestStamp && hubHistoryRevision !== null && hubHistoryRevision !== revision;
  hubHistoryNotice = backfilled ? "检测到历史补录，正在重新读取最近记录…" : "";
  if (hubHistoryScope !== scope || backfilled) {
    hubHistory = [];
    hubHistoryQuery = {};
    hubHistoryOlderLoaded = false;
    hubHistoryLatestStamp = "";
    hubHistoryRevision = null;
    earlier = false;
  }
  hubHistoryScope = scope;
  hubHistoryResource = resourceId;
  hubHistoryLoading = true;
  hubHistoryError = "";
  renderHubHistory();
  try {
    const limit = earlier ? Math.min(1440, 5000 - hubHistory.length) : 1440;
    const before = earlier ? `&before=${encodeURIComponent(hubHistoryQuery.next_cursor)}` : "";
    const history = await request(`/api/hub/resources/${encodeURIComponent(resourceId)}/history?hours=${hubHistoryHours}&limit=${limit}&projection=chart${before}`);
    if (requestId !== hubHistoryRequest || currentHubSelection !== resourceId) return;
    const query = history.query || history;
    const merged = new Map();
    const keepOlder = earlier || hubHistoryOlderLoaded;
    [...(keepOlder ? hubHistory : []), ...(history.events || [])].forEach((event) => {
      if (!Number.isFinite(new Date(event.observed_at || event.received_at).getTime())) return;
      merged.set(event.id != null ? `id:${event.id}` : `${event.observed_at}:${event.received_at}`, event);
    });
    hubHistory = [...merged.values()].sort((a, b) => new Date(a.observed_at || a.received_at) - new Date(b.observed_at || b.received_at)).slice(-5000);
    if (earlier || !hubHistoryOlderLoaded) hubHistoryQuery = query;
    if (earlier) hubHistoryOlderLoaded = true;
    hubHistoryLatestStamp = latestStamp;
    hubHistoryRevision = revision;
    hubHistoryNotice = backfilled ? "历史有补录，已重新读取最近记录；可继续加载更早记录" : "";
  } catch (error) {
    if (requestId !== hubHistoryRequest || currentHubSelection !== resourceId) return;
    hubHistoryError = `历史暂不可用：${error.message}`;
    hubHistoryNotice = "";
    // Allow retry on the next snapshot after a transient failure.
    hubHistoryKey = earlier ? previousKey : "";
  } finally {
    if (requestId === hubHistoryRequest && currentHubSelection === resourceId) {
      hubHistoryLoading = false;
      renderHubHistory();
    }
  }
}

function renderHubHistory() {
  const resource = currentHub?.resources?.find((item) => item.resource_id === currentHubSelection);
  if (!resource) return;
  const select = $("#hub-history-metric");
  const previous = select.value;
  const properties = hubResourceFields(resource);
  const knownAliases = new Set(metricDefinitions.flatMap((metric) => metric.aliases));
  const readable = (property) => chartableField(property, resource.state);
  const baseMetrics = metricDefinitions.filter((metric) => !properties.some((property) => metric.aliases.includes(property.key) && !readable(property))).map((metric) => {
    const field = properties.find((property) => metric.aliases.includes(property.key));
    return field ? { ...metric, path: field.path || [field.key], label: field.label || metric.label, unit: field.unit ?? metric.unit } : metric;
  });
  const extraMetrics = properties.filter((property) => readable(property) && !knownAliases.has(property.key) && (fieldType(property, resource.state) === "number" || hubHistory.some((event) => typeof stateFieldValue(event.state, property) === "number")))
    .map((property) => ({ key: property.key, path: property.path || [property.key], aliases: [property.key], label: property.label || property.key, unit: property.unit || "" }));
  const historyByMetric = new Map([...baseMetrics, ...extraMetrics].map((metric) => [metric.key, historyMetricPoints(hubHistory, metric)]));
  const available = [...baseMetrics, ...extraMetrics].filter((metric) => metricValue(resource.state, metric, properties) !== null || historyByMetric.get(metric.key).some((point) => point.value !== null));
  select.replaceChildren();
  available.forEach((metric) => {
    const option = document.createElement("option");
    option.value = metric.key;
    option.textContent = `${metric.label}${metric.unit ? `（${metric.unit}）` : ""}`;
    select.append(option);
  });
  if (available.some((metric) => metric.key === previous)) select.value = previous;
  select.disabled = !available.length;
  const metric = available.find((item) => item.key === select.value);
  const points = metric ? historyByMetric.get(metric.key) || [] : [];
  const now = Date.now();
  const from = now - hubHistoryHours * 3600000;
  const inRange = points.filter((point) => point.at >= from && point.at <= now);
  const valid = inRange.filter((point) => point.value !== null);
  const chart = $("#hub-chart");
  chart.replaceChildren();
  if (hubHistoryLoading && !hubHistory.length) {
    chart.textContent = "正在读取历史读数…";
  } else if (!valid.length) {
    chart.textContent = metric ? "这个时间范围内尚无该指标的有效采样。采样后会自动显示趋势。" : "此数据源暂无可绘制趋势的数值指标。";
  } else {
    drawHistoryChart(chart, inRange, metric, from, now);
  }
  const meta = $("#hub-history-meta");
  meta.classList.toggle("bad", Boolean(hubHistoryError));
  const parts = [hubHistoryError, hubHistoryNotice, hubHistoryLoading ? "正在更新历史…" : "", valid.length ? `${valid.length} 个有效采样点 · 最早 ${formatTime(valid[0].at)} · 最近 ${formatTime(valid.at(-1).at)}` : "尚无有效采样点"];
  const more = Boolean(hubHistoryQuery.has_more || hubHistoryQuery.truncated);
  if (more) parts.push(hubHistory.length >= 5000 ? "已达到单次展示 5,000 条上限，可缩短时间范围查看细节" : `当前已加载 ${hubHistory.length.toLocaleString("zh-CN")} 条记录，查询范围内还有更早记录`);
  else if (hubHistory.length && !hubHistoryLoading) parts.push("所选范围内已保存的记录均已加载");
  if (valid.length === 1) parts.push("目前只有一个采样点，累计后即可查看变化");
  if (valid.length > 1) parts.push(`最低 ${displayNumber(Math.min(...valid.map((point) => point.value)))}${metric.unit} · 最高 ${displayNumber(Math.max(...valid.map((point) => point.value)))}${metric.unit}`);
  parts.push("缺测时段会断开显示");
  meta.textContent = parts.filter(Boolean).join(" · ");
  const moreButton = $("#hub-history-more");
  moreButton.hidden = !more || !hubHistoryQuery.next_cursor || hubHistory.length >= 5000;
  moreButton.disabled = hubHistoryLoading;
  moreButton.textContent = hubHistoryLoading ? "正在加载…" : "加载更早记录";
  const historyList = $("#hub-history-list");
  historyList.replaceChildren();
  const rangedEvents = hubHistory.filter((event) => new Date(event.observed_at || event.received_at).getTime() >= from);
  rangedEvents.slice(-60).reverse().forEach((event) => {
    const row = document.createElement("div");
    row.className = "hub-history-row";
    const at = document.createElement("time");
    at.textContent = formatTime(event.observed_at || event.received_at);
    const summary = document.createElement("span");
    summary.textContent = stateSummary(event.state, properties);
    row.append(at, summary);
    historyList.append(row);
  });
  if (!rangedEvents.length) historyList.textContent = "该时间范围内暂无记录";
}

function drawHistoryChart(container, points, metric, from, to) {
  const svgNS = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(svgNS, "svg");
  svg.setAttribute("viewBox", "0 0 620 240");
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", `${metric.label}最近 ${hubHistoryHours} 小时变化，单位 ${metric.unit || "数值"}`);
  const valid = points.filter((point) => point.value !== null);
  let low = Math.min(...valid.map((point) => point.value));
  let high = Math.max(...valid.map((point) => point.value));
  const padding = Math.max((high - low) * .15, metric.key === "temperature" ? .5 : 1);
  low -= padding;
  if (metric.key !== "temperature" && valid.every((point) => point.value >= 0)) low = Math.max(0, low);
  high += padding;
  const x = (at) => 54 + (at - from) / (to - from) * 548;
  const y = (value) => 192 - (value - low) / (high - low) * 166;
  const make = (tag, attributes, value = "") => {
    const node = document.createElementNS(svgNS, tag);
    Object.entries(attributes).forEach(([key, attribute]) => node.setAttribute(key, String(attribute)));
    if (value) node.textContent = value;
    svg.append(node);
    return node;
  };
  for (let index = 0; index < 4; index++) {
    const value = low + (high - low) * index / 3;
    make("line", { x1: 54, x2: 602, y1: y(value), y2: y(value), class: "chart-grid" });
    make("text", { x: 45, y: y(value) + 4, "text-anchor": "end", class: "chart-label" }, displayNumber(value));
  }
  const chartTime = (value) => new Intl.DateTimeFormat("zh-CN", hubHistoryHours > 24 ? { month: "2-digit", day: "2-digit" } : { hour: "2-digit", minute: "2-digit", hour12: false }).format(value);
  [from, (from + to) / 2, to].forEach((at, index) => make("text", { x: x(at), y: 220, "text-anchor": index === 0 ? "start" : index === 2 ? "end" : "middle", class: "chart-label" }, chartTime(at)));
  make("text", { x: 54, y: 14, class: "chart-label" }, metric.unit || metric.label);
  const cadence = Math.max(60, Number(currentHub?.sampling?.interval_seconds) || 60) * 1000;
  let path = "";
  let previous = null;
  points.forEach((point) => {
    if (point.value === null) { previous = null; return; }
    const connected = previous && point.at - previous.at <= cadence * 3;
    path += `${connected ? "L" : "M"}${x(point.at).toFixed(2)},${y(point.value).toFixed(2)} `;
    previous = point;
  });
  make("path", { d: path, class: "chart-line", fill: "none" });
  valid.forEach((point) => {
    const dot = make("circle", { cx: x(point.at), cy: y(point.value), r: valid.length === 1 ? 5 : 2.3, class: "chart-point" });
    const title = document.createElementNS(svgNS, "title");
    title.textContent = `${formatTime(point.at)} · ${displayNumber(point.value)}${metric.unit}`;
    dot.append(title);
  });
  container.append(svg);
}

function startHubStream() {
  clearTimeout(hubStreamRetryTimer);
  if (document.hidden) return;
  hubStream?.close();
  setPill("#hub-stream-pill", "实时流连接中", "");
  hubStream = new EventSource("/api/hub/stream");
  hubStream.onopen = () => {
    hubStreamConnected = true;
    hubStreamRetrySeconds = 0;
    setPill("#hub-stream-pill", "多端同步已连接", "ok");
  };
  hubStream.onmessage = (event) => {
    try {
      const payload = JSON.parse(event.data);
      if (payload.type === "panel.auth_required") requirePanelLogin();
      if (payload.type === "hub.snapshot") renderHub(payload.hub);
      if (payload.type === "hub.resync") refreshAll(true);
      if (payload.type === "hub.update") refreshHubView(payload.resource);
      if (payload.type === "home.extensions") {
        acceptHomeExtension(payload.device_key, payload.extensions);
        renderHomeCategories();
        renderHomeCards();
        renderHomeDetail();
      }
    } catch { /* A malformed event should not break the main panel. */ }
  };
  hubStream.onerror = () => {
    hubStreamConnected = false;
    hubStream?.close();
    hubStream = null;
    hubStreamRetrySeconds = Math.min(30, Math.max(5, hubStreamRetrySeconds ? hubStreamRetrySeconds * 2 : 5));
    setPill("#hub-stream-pill", `同步断开 · ${hubStreamRetrySeconds} 秒后重连`, "bad");
    hubStreamRetryTimer = setTimeout(startHubStream, hubStreamRetrySeconds * 1000);
  };
}

function refreshHubView(resource) {
  if (!currentHub) return;
  const resources = (currentHub.resources || []).filter((item) => item.resource_id !== resource.resource_id);
  const events = [
    {
      resource_id: resource.resource_id,
      kind: resource.kind,
      summary: resource.summary,
      state: resource.state,
      observed_at: resource.observed_at,
      received_at: resource.received_at,
    },
    ...(currentHub.events || []),
  ].filter((event, index, all) => (
    all.findIndex((candidate) => `${candidate.resource_id}:${candidate.received_at}` === `${event.resource_id}:${event.received_at}`) === index
  )).slice(0, 30);
  currentHub = {
    ...currentHub,
    resources: [...resources, resource].sort((a, b) => `${a.kind}:${a.name}`.localeCompare(`${b.kind}:${b.name}`, "zh-CN")),
    events,
  };
  renderHub(currentHub);
}

function updateHomeActionOptions() {
  const device = $("#home-test-device").selectedOptions[0];
  const actions = device ? JSON.parse(device.dataset.actions || "[]") : [];
  const actionSelect = $("#home-test-action");
  const previous = actionSelect.value;
  actionSelect.replaceChildren();
  actions.forEach((action) => {
    const option = document.createElement("option");
    option.value = action;
    option.textContent = actionLabels[action] || action;
    actionSelect.append(option);
  });
  if (actions.includes(previous)) actionSelect.value = previous;
  const needsValue = ["set_brightness", "set_color_temperature", "set_temperature", "set_mode", "set_speed"].includes(actionSelect.value);
  $("#home-test-value").disabled = !needsValue;
}

async function refreshAll(silent = false) {
  if (refreshPromise) return refreshPromise;
  const endpoints = [
    ["status", "/api/status"],
    ["config", "/api/config"],
    ["home", "/api/home"],
    ["hub", "/api/hub"],
  ];
  refreshPromise = (async () => {
    const results = await Promise.allSettled(endpoints.map(([, path]) => request(path)));
    const failures = [];
    const values = {};
    results.forEach((result, index) => {
      const [name] = endpoints[index];
      if (result.status === "fulfilled") values[name] = result.value;
      else failures.push(`${name}: ${result.reason?.message || "请求失败"}`);
    });
    if (values.config) renderConfig(values.config);
    if (values.status) renderStatus(values.status);
    if (values.home) renderHome(values.home);
    hubSnapshotFailed = !values.hub;
    if (values.hub) renderHub(values.hub);
    else if (currentHub) renderHub(currentHub);
    if (failures.length) {
      $("#live-dot").className = "bad";
      text("#live-label", failures.length === endpoints.length ? "页面无法连接" : "部分数据暂不可用");
      if (!silent) showToast(failures[0], true);
    } else if (!currentStatus?.last_error) {
      $("#live-dot").className = "ok";
    }
  })().finally(() => { refreshPromise = null; });
  return refreshPromise;
}

function showView(name, { updateHash = true } = {}) {
  if (name !== "home") cancelAcAdjustments();
  $$(".view").forEach((node) => node.classList.toggle("active", node.id === `view-${name}`));
  $$(".tab").forEach((node) => {
    const selected = node.dataset.view === name;
    node.classList.toggle("active", selected);
    if (selected) node.setAttribute("aria-current", "page");
    else node.removeAttribute("aria-current");
  });
  const activeNav = $(`[data-view="${name}"]`);
  if (activeNav) {
    text("#workspace-current", activeNav.textContent.trim());
    const navigation = activeNav.closest(".tabs");
    if (navigation.scrollWidth > navigation.clientWidth) {
      navigation.scrollTo({ left: Math.max(0, activeNav.offsetLeft - navigation.offsetLeft - 16), behavior: "auto" });
    }
  }
  if (updateHash && location.hash !== `#${name}`) history.replaceState(null, "", `#${name}`);
  if (name === "services") loadModelRegistry();
  if (name === "data") loadHubStorage();
  if (name === "health") {
    if (!healthStarted) loadHealthSource();
    else renderHealthSource();
  }
  if (name === "chat") loadAutomations();
}

function healthDateValid(value) {
  if (typeof value !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return false;
  const timestamp = Date.parse(`${value}T00:00:00Z`);
  return Number.isFinite(timestamp) && new Date(timestamp).toISOString().slice(0, 10) === value;
}

function healthTimestamp(value) {
  if (typeof value === "number") return value < 1e12 ? value * 1000 : value;
  return value ? new Date(value).getTime() : NaN;
}

function healthTime(value) {
  if (typeof value === "string" && /^\d{2}:\d{2}(?::\d{2})?$/.test(value)) return value;
  const timestamp = healthTimestamp(value);
  return Number.isFinite(timestamp) ? formatTime(timestamp) : "暂无";
}

function healthWebUrl(value) {
  try {
    const url = new URL(value);
    return ["http:", "https:"].includes(url.protocol) && !url.username && !url.password ? url.href : "";
  } catch { return ""; }
}

function healthResource(name) {
  return currentHealthSource?.resources?.find((resource) => resource.resource_id === `health.${name}`);
}

function healthField(resource, key) {
  return (resource?.fields || []).find((field) => field.key === key) || { key, path: [key], label: key, status: "unavailable" };
}

function healthDisplay(field, state) {
  const value = stateFieldValue(state, field);
  if (fieldReadStatus(field, state).status === "sensitive") return "敏感信息已隐藏";
  if (value == null) return "暂无记录";
  if (/(?:start|end)_time$/.test(field.key)) return healthTime(value);
  if (typeof value === "number" && ["分钟", "min"].includes(field.unit) && value >= 60) {
    const hours = Math.floor(Math.round(value) / 60);
    const minutes = Math.round(value) % 60;
    return `${hours} 小时${minutes ? ` ${minutes} 分` : ""}`;
  }
  return displayPropertyValue(field, state);
}

async function loadHealthSource(refresh = false) {
  healthStarted = true;
  const sequence = ++healthRequestSequence;
  const date = healthSelectedDate;
  const days = healthDays;
  const key = `${date}:${days}`;
  currentHealthSource = healthSnapshots.get(key) || null;
  healthLoading = true;
  renderHealthSource();
  const query = new URLSearchParams({ days: String(days), ...(date ? { date } : {}) });
  try {
    const snapshot = await request(refresh ? "/api/health-source/refresh" : `/api/health-source?${query}`, {
      ...(refresh ? { method: "POST", body: JSON.stringify({ days, ...(date ? { date } : {}) }) } : {}), timeoutMs: 30000,
    });
    if (sequence !== healthRequestSequence) return;
    currentHealthSource = snapshot;
    healthSnapshots.set(key, snapshot);
    if (healthDateValid(snapshot.selected_date)) {
      healthSelectedDate = snapshot.selected_date;
      healthSnapshots.set(`${snapshot.selected_date}:${days}`, snapshot);
    }
    if (healthSnapshots.size > 16) healthSnapshots.delete(healthSnapshots.keys().next().value);
  } catch (error) {
    if (sequence !== healthRequestSequence) return;
    const previous = healthSnapshots.get(key);
    currentHealthSource = { ...(previous || {}), available: false, stale: Boolean(previous), message: `健康中心暂未读取成功：${error.message}`, checked_at: new Date().toISOString() };
  } finally {
    if (sequence === healthRequestSequence) {
      healthLoading = false;
      renderHealthSource();
    }
  }
}

function healthMetricCard(resource, key) {
  const field = healthField(resource, key);
  const card = homeNode("article", "health-metric-card");
  card.dataset.healthMetric = `${resource?.resource_id || "health"}:${key}`;
  card.append(homeNode("span", "health-metric-label", field.label || key), homeNode("strong", "", healthDisplay(field, resource?.state || {})), homeNode("small", "", resource?.name || "健康中心"));
  const diagnostic = fieldReadStatus(field, resource?.state || {});
  if (diagnostic.status !== "ok") card.append(homeNode("small", "health-muted", diagnostic.message || diagnostic.label));
  return card;
}

function renderHealthSleep() {
  const resource = healthResource("sleep");
  const summary = $("#health-sleep-summary");
  const stages = $("#health-sleep-stages");
  summary.replaceChildren();
  stages.replaceChildren();
  if (!resource) {
    summary.append(homeNode("p", "health-muted", "健康中心暂未提供所选日期的睡眠记录。"));
    return;
  }
  const primary = homeNode("div", "health-sleep-main");
  ["total_min", "score", "efficiency_percent"].forEach((key) => primary.append(healthMetricCard(resource, key)));
  summary.append(primary);
  const interval = [resource.state?.start_time, resource.state?.end_time];
  summary.append(homeNode("p", "health-muted", interval.some((value) => value != null) ? `睡眠时段 ${healthTime(interval[0])} — ${healthTime(interval[1])}` : "暂无睡眠起止时间"));
  const keys = ["deep_min", "light_min", "rem_min", "awake_min"];
  const measured = keys.filter((key) => typeof resource.state?.[key] === "number");
  const total = measured.reduce((sum, key) => sum + Math.max(0, resource.state[key]), 0);
  if (total > 0) {
    const bar = homeNode("div", "health-stage-bar");
    bar.setAttribute("aria-label", "已记录的睡眠阶段占比");
    measured.forEach((key) => {
      if (resource.state[key] <= 0) return;
      const segment = homeNode("span", `health-stage ${key}`);
      segment.style.flexGrow = resource.state[key];
      segment.title = `${healthField(resource, key).label} ${displayNumber(resource.state[key])} 分钟`;
      bar.append(segment);
    });
    stages.append(bar);
  }
  const legend = homeNode("div", "health-stage-legend");
  [...keys, "nap_min"].forEach((key) => {
    const field = healthField(resource, key);
    const item = homeNode("div");
    item.append(homeNode("i", key), homeNode("span", "", field.label), homeNode("strong", "", healthDisplay(field, resource.state)));
    legend.append(item);
  });
  stages.append(legend);
  if (!measured.length) stages.append(homeNode("p", "health-muted", "这一天未提供睡眠分期。总时长有记录时仍可查看，缺少的阶段不会按 0 计算。"));
}

function renderHealthWorkouts() {
  const container = $("#health-workouts");
  container.replaceChildren();
  const workouts = currentHealthSource?.workouts || [];
  text("#health-workout-count", `${workouts.length} 条记录`);
  if (!workouts.length) container.append(homeNode("p", "health-muted", "健康中心未提供所选日期的运动记录。"));
  workouts.forEach((workout) => {
    const row = homeNode("article", "health-workout");
    row.append(homeNode("strong", "", workout.label || workout.type || "运动记录"), homeNode("time", "", `${workout.date || currentHealthSource.selected_date || ""}${workout.start_time ? ` · ${healthTime(workout.start_time)}` : ""}`));
    const details = homeNode("div", "health-workout-metrics");
    [["duration_min", "时长", "分钟"], ["distance_km", "距离", "km"], ["calories", "消耗", "kcal"], ["avg_hr", "平均心率", "次/分"], ["pace", "配速", ""], ["train_load", "训练负荷", ""]].forEach(([key, label, unit]) => {
      if (workout[key] == null) return;
      const value = typeof workout[key] === "number" ? displayNumber(workout[key]) : String(workout[key]);
      const item = homeNode("span");
      item.append(homeNode("small", "", label), homeNode("strong", "", `${value}${unit ? ` ${unit}` : ""}`));
      details.append(item);
    });
    row.append(details);
    container.append(row);
  });
}

function renderHealthFields() {
  const list = $("#health-field-list");
  list.replaceChildren();
  (currentHealthSource?.resources || []).forEach((resource) => {
    list.append(homeNode("h4", "health-field-group", resource.name));
    (resource.fields || []).forEach((field) => {
      const diagnostic = fieldReadStatus(field, resource.state || {});
      const row = homeNode("article", "hub-field-row health-field-row");
      row.dataset.healthField = `${resource.resource_id}:${field.key}`;
      const identity = homeNode("div", "hub-field-identity");
      identity.append(homeNode("strong", "", field.label || field.key), homeNode("small", "", field.key));
      const value = homeNode("div", "hub-field-value");
      value.append(homeNode("strong", "", healthDisplay(field, resource.state || {})));
      const status = homeNode("div", "hub-field-diagnostic");
      status.append(homeNode("span", `status-pill ${diagnostic.status === "ok" ? "ok" : ""}`, diagnostic.label), homeNode("small", "", diagnostic.message || ""));
      const origin = homeNode("div", "hub-field-origin");
      origin.append(homeNode("span", "", resource.name || "健康中心"), homeNode("time", "", resource.date || currentHealthSource.selected_date || "日期未提供"));
      row.append(identity, value, status, origin);
      list.append(row);
    });
  });
}

function renderHealthTrend() {
  const series = currentHealthSource?.series || [];
  const select = $("#health-trend-metric");
  const previous = select.value;
  select.replaceChildren(...series.map((item) => new Option(`${item.label}${item.unit ? `（${item.unit}）` : ""}`, item.metric)));
  if (series.some((item) => item.metric === previous)) select.value = previous;
  select.disabled = !series.length || healthLoading;
  const selected = series.find((item) => item.metric === select.value);
  const chart = $("#health-trend-chart");
  const records = $("#health-trend-records");
  chart.replaceChildren();
  records.replaceChildren();
  if (!selected) {
    chart.append(homeNode("p", "health-muted", "健康中心暂未提供趋势记录。"));
    text("#health-trend-note", "");
    return;
  }
  const pointsByDay = new Map((selected.points || []).filter((point) => healthDateValid(point.date)).map((point) => [point.date, point]));
  let points;
  if (healthDateValid(currentHealthSource.start_date) && healthDateValid(currentHealthSource.end_date)) {
    const start = Date.parse(`${currentHealthSource.start_date}T00:00:00Z`);
    const end = Date.parse(`${currentHealthSource.end_date}T00:00:00Z`);
    points = Array.from({ length: Math.min(31, Math.max(0, Math.round((end - start) / 86400000) + 1)) }, (_, index) => {
      const date = new Date(start + index * 86400000).toISOString().slice(0, 10);
      return { date, value: pointsByDay.get(date)?.value ?? null };
    });
  } else points = [...pointsByDay.values()].sort((a, b) => a.date.localeCompare(b.date)).slice(-healthDays);
  const valid = points.filter((point) => typeof point.value === "number" && Number.isFinite(point.value));
  points.slice().reverse().forEach((point) => {
    const row = homeNode("div", "health-trend-record");
    row.append(homeNode("time", "", point.date), homeNode("strong", "", typeof point.value === "number" && Number.isFinite(point.value) ? `${displayNumber(point.value)}${selected.unit ? ` ${selected.unit}` : ""}` : "未记录"));
    records.append(row);
  });
  text("#health-trend-note", [selected.message, `${valid.length} 天有记录${points.length ? ` / ${points.length} 天` : ""}`, "缺测日期留空，不按 0 计算"].filter(Boolean).join(" · "));
  if (!valid.length) {
    chart.append(homeNode("p", "health-muted", "所选时间范围内暂无这个指标的有效记录。"));
    return;
  }
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", "0 0 760 260");
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", `${selected.label}，${currentHealthSource.start_date || ""} 至 ${currentHealthSource.end_date || ""}，${valid.length} 天有效记录，单位 ${selected.unit || "数值"}`);
  const low = Math.min(...valid.map((point) => point.value));
  const high = Math.max(...valid.map((point) => point.value));
  const padding = Math.max((high - low) * .15, 1);
  const min = Math.max(0, low - padding);
  const max = high + padding;
  const x = (index) => 66 + (points.length > 1 ? index / (points.length - 1) : .5) * 660;
  const y = (value) => 206 - (value - min) / (max - min || 1) * 180;
  const make = (name, attributes, label) => {
    const node = document.createElementNS(ns, name);
    Object.entries(attributes).forEach(([key, value]) => node.setAttribute(key, value));
    if (label != null) node.textContent = label;
    return node;
  };
  [min, (min + max) / 2, max].forEach((value) => {
    svg.append(make("line", { x1: 66, x2: 726, y1: y(value), y2: y(value), class: "health-chart-grid" }), make("text", { x: 54, y: y(value) + 4, "text-anchor": "end", class: "health-chart-label" }, displayNumber(value)));
  });
  const segments = [];
  let open = false;
  points.forEach((point, index) => {
    if (typeof point.value !== "number" || !Number.isFinite(point.value)) { open = false; return; }
    segments.push(`${open ? "L" : "M"}${x(index)} ${y(point.value)}`);
    open = true;
  });
  svg.append(make("path", { d: segments.join(" "), class: "health-chart-line", fill: "none" }));
  points.forEach((point, index) => {
    if (typeof point.value !== "number" || !Number.isFinite(point.value)) return;
    const dot = make("circle", { cx: x(index), cy: y(point.value), r: 4, class: "health-chart-dot" });
    dot.append(make("title", {}, `${point.date} · ${displayNumber(point.value)} ${selected.unit || ""}`));
    svg.append(dot);
  });
  if (points.length) {
    svg.append(make("text", { x: 66, y: 240, class: "health-chart-label" }, points[0].date.slice(5)), make("text", { x: 726, y: 240, "text-anchor": "end", class: "health-chart-label" }, points.at(-1).date.slice(5)));
  }
  chart.append(svg);
}

function renderHealthSource() {
  const snapshot = currentHealthSource;
  const content = $("#health-content");
  const empty = $("#health-empty");
  const hasContent = Boolean(snapshot?.resources?.length || snapshot?.series?.length || snapshot?.workouts?.length);
  content.hidden = !hasContent;
  empty.hidden = hasContent;
  content.classList.toggle("health-cached", Boolean(snapshot?.stale));
  $("#health-refresh").disabled = healthLoading;
  $("#health-refresh").textContent = healthLoading ? "正在读取…" : "刷新展示";
  $("#health-date").value = healthSelectedDate;
  $("#health-days").value = String(healthDays);
  const pill = healthLoading ? ["正在读取", ""] : snapshot?.stale ? ["显示缓存", "bad"] : snapshot?.available ? ["只读连接正常", "ok"] : ["暂未连接", "bad"];
  setPill("#health-source-pill", ...pill);
  text("#health-source-message", healthLoading ? "正在读取健康中心已保存的数据…" : snapshot?.message || "暂未取得健康中心数据，请稍后刷新展示。");
  const upstreamTimes = (snapshot?.resources || []).map((resource) => healthTimestamp(resource.synced_at)).filter(Number.isFinite);
  text("#health-upstream-time", [`源端最新记录：${snapshot?.latest_data_date || "暂无"}`, upstreamTimes.length ? `源端更新 ${formatTime(Math.max(...upstreamTimes))}` : "源端未提供更新时间"].join(" · "));
  text("#health-fetched-time", [`中枢读取时间：${healthTime(snapshot?.fetched_at)}`, snapshot?.stale && snapshot?.checked_at ? `最近尝试 ${healthTime(snapshot.checked_at)}` : ""].filter(Boolean).join(" · "));
  text("#health-selection-summary", snapshot?.selected_date ? `${snapshot.selected_date} · ${healthDays} 天趋势` : "默认查看源端最近有记录的日期");
  const url = healthWebUrl(snapshot?.service?.web_url);
  const origin = $("#health-origin-link");
  origin.hidden = !url;
  if (url) origin.href = url;
  else origin.removeAttribute("href");
  empty.querySelector("strong").textContent = healthLoading ? "正在读取健康中心" : "暂时没有可展示的健康记录";
  empty.querySelector("p").textContent = healthLoading ? "所选日期的数据将在读取完成后展示。" : snapshot?.message || "可打开原健康中心查看数据同步与连接状态。";
  if (!hasContent) return;
  text("#health-daily-date", snapshot.selected_date || "日期未提供");
  const daily = $("#health-daily-metrics");
  daily.replaceChildren();
  [["activity", ["steps", "distance_m", "calories", "active_min", "standing_count", "vigorous_min"]], ["heart", ["resting_hr", "average_hr", "min_spo2", "stress_average"]], ["training", ["count", "duration_min"]]].forEach(([name, keys]) => {
    const resource = healthResource(name);
    if (resource) keys.forEach((key) => daily.append(healthMetricCard(resource, key)));
  });
  renderHealthSleep();
  renderHealthWorkouts();
  renderHealthFields();
  renderHealthTrend();
}

async function withBusy(button, action) {
  const old = button.textContent;
  button.disabled = true;
  button.textContent = "处理中…";
  try { return await action(); }
  finally { button.disabled = false; button.textContent = old; }
}

$$('[data-view]').forEach((button) => button.addEventListener("click", () => showView(button.dataset.view)));
$$('[data-go]').forEach((button) => button.addEventListener("click", () => showView(button.dataset.go)));
$("#health-refresh").addEventListener("click", () => loadHealthSource(true));
$("#health-date").addEventListener("change", (event) => {
  if (event.target.value && !healthDateValid(event.target.value)) return;
  healthSelectedDate = event.target.value;
  loadHealthSource();
});
$("#health-days").addEventListener("change", (event) => {
  const value = Number(event.target.value);
  if (![7, 30].includes(value)) return;
  healthDays = value;
  loadHealthSource();
});
$("#health-latest").addEventListener("click", () => {
  healthSelectedDate = "";
  loadHealthSource();
});
$("#health-trend-metric").addEventListener("change", renderHealthTrend);
$$(["#connection-form", "#behavior-form"].join(",")).forEach((form) => {
  form.querySelectorAll("input, select").forEach((input) => {
    input.addEventListener("input", () => { input.dataset.dirty = "true"; });
    input.addEventListener("change", () => { input.dataset.dirty = "true"; });
  });
});

$("#refresh").addEventListener("click", () => refreshAll());
$("#reconnect").addEventListener("click", async (event) => {
  await withBusy(event.currentTarget, async () => {
    await request("/api/actions/reconnect", { method: "POST", body: "{}" });
    showToast("已提交重连");
    setTimeout(() => refreshAll(true), 1200);
  }).catch((error) => showToast(error.message, true));
});

$("#connection-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = $("#save-connection");
  const result = $("#save-result");
  result.className = "action-result";
  result.textContent = "";
  const payload = Object.fromEntries(new FormData(event.currentTarget).entries());
  await withBusy(button, async () => {
    const response = await request("/api/config", { method: "PUT", body: JSON.stringify(payload) });
    clearDirtyFields(event.currentTarget);
    renderConfig(response.config);
    result.textContent = "已保存，正在重连";
    result.classList.add("ok");
    event.currentTarget.querySelectorAll('input[type="password"]').forEach((input) => { input.value = ""; });
    setTimeout(() => refreshAll(true), 1500);
  }).catch((error) => {
    result.textContent = error.message;
    result.classList.add("bad");
  });
});

$("#clear-form").addEventListener("click", () => {
  $("#connection-form").reset();
  clearDirtyFields($("#connection-form"));
  if (currentConfig) renderConfig(currentConfig);
});

$("#behavior-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = $("#save-behavior");
  const result = $("#behavior-result");
  result.className = "action-result";
  const payload = Object.fromEntries(new FormData(event.currentTarget).entries());
  await withBusy(button, async () => {
    const response = await request("/api/config", { method: "PUT", body: JSON.stringify(payload) });
    clearDirtyFields(event.currentTarget);
    renderConfig(response.config);
    result.textContent = "偏好已生效";
    result.classList.add("ok");
  }).catch((error) => {
    result.textContent = error.message;
    result.classList.add("bad");
  });
});

/* ---------------------------------------------------------------- Markdown
   只造 DOM 节点，不拼 HTML 字符串：模型输出里的 <script> 之类不会被解析。
   CSP 是 style-src 'self'，所以样式全部走 app.css 的类，不能写 style 属性。 */

const MD_LINK = /^(?:https?:|mailto:|\/|#)/i;

function mdEl(tag, className) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  return node;
}

/** 行内：`代码`、**粗**、*斜*、~~删除线~~、[文字](链接)、裸网址。 */
function mdInline(target, source) {
  const pattern = /(`[^`\n]+`)|(\*\*[^*\n]+\*\*)|(__[^_\n]+__)|(~~[^~\n]+~~)|(\*[^*\n]+\*)|(_[^_\n]+_)|(\[[^\]\n]+\]\([^)\s]+\))|(<?https?:\/\/[^\s<>()]+>?)/g;
  let cursor = 0;
  let match;
  while ((match = pattern.exec(source)) !== null) {
    if (match.index > cursor) target.append(source.slice(cursor, match.index));
    const token = match[0];
    cursor = match.index + token.length;
    if (match[1]) {
      const code = mdEl("code", "md-inline-code");
      code.textContent = token.slice(1, -1);
      target.append(code);
    } else if (match[2] || match[3]) {
      const strong = mdEl("strong");
      mdInline(strong, token.slice(2, -2));
      target.append(strong);
    } else if (match[4]) {
      const del = mdEl("del");
      mdInline(del, token.slice(2, -2));
      target.append(del);
    } else if (match[5] || match[6]) {
      const em = mdEl("em");
      mdInline(em, token.slice(1, -1));
      target.append(em);
    } else if (match[7]) {
      const cut = token.indexOf("](");
      const label = token.slice(1, cut);
      const href = token.slice(cut + 2, -1).trim();
      if (!MD_LINK.test(href)) {
        target.append(token);
      } else {
        const link = mdEl("a", "md-link");
        link.textContent = label;
        link.href = href;
        link.rel = "noopener noreferrer";
        link.target = "_blank";
        target.append(link);
      }
    } else {
      const raw = token.replace(/^<|>$/g, "");
      const link = mdEl("a", "md-link");
      link.textContent = raw;
      link.href = raw;
      link.rel = "noopener noreferrer";
      link.target = "_blank";
      target.append(link);
    }
  }
  if (cursor < source.length) target.append(source.slice(cursor));
  return target;
}

function mdStarts(lines, index) {
  const line = lines[index];
  if (!line.trim()) return true;
  if (/^\s*(`{3,}|~{3,})/.test(line)) return true;
  if (/^\s*#{1,6}\s+/.test(line)) return true;
  if (/^\s*([-*_])\1{2,}\s*$/.test(line)) return true;
  if (/^\s*>/.test(line)) return true;
  if (/^\s*([-*+]|\d+[.)])\s+/.test(line)) return true;
  if (line.includes("|") && /^\s*\|?[\s:|-]+\|/.test(lines[index + 1] || "")) return true;
  return false;
}

function mdTable(rows) {
  const table = mdEl("table", "md-table");
  const head = mdEl("thead");
  const headRow = mdEl("tr");
  rows[0].forEach((cell) => {
    const th = mdEl("th");
    mdInline(th, cell);
    headRow.append(th);
  });
  head.append(headRow);
  const body = mdEl("tbody");
  rows.slice(1).forEach((cells) => {
    const tr = mdEl("tr");
    rows[0].forEach((_, column) => {
      const td = mdEl("td");
      mdInline(td, cells[column] || "");
      tr.append(td);
    });
    body.append(tr);
  });
  table.append(head, body);
  return table;
}

const mdCells = (line) => line.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((cell) => cell.trim());

/** 块级：围栏代码、标题、分隔线、引用、列表、表格、段落。 */
function mdBlocks(source) {
  const root = document.createDocumentFragment();
  const lines = String(source ?? "").replace(/\r\n?/g, "\n").split("\n");
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) {
      i++;
      continue;
    }

    const fence = line.match(/^\s*(`{3,}|~{3,})\s*([\w+#.-]*)\s*$/);
    if (fence) {
      const mark = fence[1];
      const body = [];
      i++;
      while (i < lines.length && !new RegExp(`^\\s*${mark[0] === "`" ? "`" : "~"}{${mark.length},}\\s*$`).test(lines[i])) {
        body.push(lines[i]);
        i++;
      }
      i++;
      const pre = mdEl("pre", "md-code");
      if (fence[2]) pre.dataset.lang = fence[2];
      const code = mdEl("code");
      code.textContent = body.join("\n");
      pre.append(code);
      root.append(pre);
      continue;
    }

    const heading = line.match(/^\s*(#{1,6})\s+(.*)$/);
    if (heading) {
      const node = mdEl(`h${heading[1].length}`);
      mdInline(node, heading[2].replace(/\s+#+\s*$/, ""));
      root.append(node);
      i++;
      continue;
    }

    if (/^\s*([-*_])\1{2,}\s*$/.test(line)) {
      root.append(mdEl("hr"));
      i++;
      continue;
    }

    if (/^\s*>/.test(line)) {
      const quote = mdEl("blockquote");
      while (i < lines.length && /^\s*>/.test(lines[i])) {
        quote.append(mdInline(mdEl("p"), lines[i].replace(/^\s*>\s?/, "")));
        i++;
      }
      root.append(quote);
      continue;
    }

    if (/^\s*([-*+]|\d+[.)])\s+/.test(line)) {
      const ordered = /^\s*\d+[.)]\s+/.test(line);
      const list = mdEl(ordered ? "ol" : "ul");
      let start = 0;
      while (i < lines.length) {
        const item = lines[i].match(/^(\s*)([-*+]|\d+[.)])\s+(.*)$/);
        if (!item) break;
        const li = mdEl("li");
        if (item[1].length >= 2) li.className = "md-nested";
        mdInline(li, item[3]);
        list.append(li);
        if (!start && ordered) start = Number.parseInt(item[2], 10) || 1;
        i++;
        if (i < lines.length && /^\s+\S/.test(lines[i]) && !/^\s*([-*+]|\d+[.)])\s+/.test(lines[i])) {
          // 缩进的续行并入上一项
          const extra = lines[i].replace(/^\s+/, "");
          mdInline(li, " " + extra);
          i++;
        }
      }
      if (ordered && start > 1) list.start = start;
      root.append(list);
      continue;
    }

    if (line.includes("|") && /^\s*\|?[\s:|-]+\|/.test(lines[i + 1] || "")) {
      const rows = [mdCells(line)];
      i += 2;
      while (i < lines.length && lines[i].includes("|") && lines[i].trim()) {
        rows.push(mdCells(lines[i]));
        i++;
      }
      root.append(mdTable(rows));
      continue;
    }

    const paragraph = mdEl("p");
    while (i < lines.length && !mdStarts(lines, i)) {
      if (paragraph.childNodes.length) paragraph.append(mdEl("br"));
      mdInline(paragraph, lines[i].trim());
      i++;
    }
    root.append(paragraph);
  }
  return root;
}

function renderChatMarkdown(target, source) {
  target.classList.add("chat-markdown");
  target.textContent = "";
  target.append(mdBlocks(source));
}

function appendChat(role, value) {
  const row = document.createElement("div");
  row.className = `chat-message ${role}`;
  const avatar = document.createElement("span");
  avatar.textContent = role === "user" ? "你" : "AI";
  // 用户自己打的字原样显示；助手回复按 Markdown 渲染。
  const content = document.createElement(role === "user" ? "p" : "div");
  if (role === "user") content.textContent = value;
  else renderChatMarkdown(content, value);
  row.append(avatar, content);
  const log = $("#chat-log");
  log.append(row);
  // Scrolling the message itself also scrolls the document when the composer
  // is near the bottom of a long page. Keep the outer page position intact
  // and move only the chat history viewport to its latest message.
  const behavior = window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth";
  if (typeof log.scrollTo === "function") log.scrollTo({ top: log.scrollHeight, behavior });
  else log.scrollTop = log.scrollHeight;
}

let chatContext = true;
const newChatConversationId = () => {
  if (window.crypto?.getRandomValues) {
    return Array.from(window.crypto.getRandomValues(new Uint8Array(24)), byte => byte.toString(16).padStart(2, "0")).join("");
  }
  return "chat-" + Date.now().toString(36) + Math.random().toString(36).slice(2) + Math.random().toString(36).slice(2);
};
let chatConversationId = newChatConversationId();

const CHAT_MODE_NOTES = {
  long: "记住本窗口前几轮，可以接着聊。",
  short: "每一轮都独立，不带上一轮内容。",
};

function applyChatMode(mode) {
  chatContext = mode === "long";
  $$("[data-chat-mode]").forEach((button) => {
    const active = button.dataset.chatMode === mode;
    button.classList.toggle("is-active", active);
    button.setAttribute("aria-pressed", active ? "true" : "false");
  });
  text("#chat-mode-note", CHAT_MODE_NOTES[mode]);
  // The speaker's read-aloud cap does not apply to console text.
  text("#chat-answer-limit", chatContext ? "长对话 · 记住前几轮" : "短对话 · 每轮独立");
}

function automationScheduleLabel(task) {
  const schedule = task.schedule || {};
  if (schedule.type === "delay") return "延迟 " + Math.max(1, Math.round(Number(schedule.delay_seconds) / 60)) + " 分钟";
  if (schedule.type === "daily") return "每天 " + (schedule.at || "");
  if (schedule.type === "weekly") return "每周 " + (Array.isArray(schedule.days) ? schedule.days.map(day => ["一", "二", "三", "四", "五", "六", "日"][day]).join("、") : "") + " " + (schedule.at || "");
  return "单次 " + (schedule.at || "");
}

function automationStatusLabel(status) {
  return ({pending: "待执行", claimed: "等待执行", running: "执行中", paused: "已暂停", unknown: "结果待核对", failed: "失败", completed: "已完成", cancelled: "已取消"})[status] || status || "未知";
}

function renderAutomations(tasks) {
  const list = $("#automation-list");
  if (!list) return;
  list.replaceChildren();
  const active = (tasks || []).filter(task => !["completed", "cancelled"].includes(task.status));
  if (!active.length) {
    list.append(homeNode("span", "muted", "暂无待执行任务"));
    return;
  }
  active.slice(0, 8).forEach((task) => {
    const row = homeNode("div", "automation-row");
    const main = homeNode("div", "automation-row-main");
    const title = homeNode("strong", "", (task.target?.device || "设备") + " · " + (task.action || "操作"));
    const detail = homeNode("small", "", automationScheduleLabel(task) + " · " + automationStatusLabel(task.status));
    main.append(title, detail);
    if (task.last_outcome) {
      const outcome = ({confirmed: "已回读确认", accepted: "指令已受理，设备状态未确认", unconfirmed: "结果未确认", failed: "执行失败"})[task.last_outcome] || task.last_outcome;
      main.append(homeNode("small", "", "上次结果：" + outcome + (task.last_message ? " · " + task.last_message : "")));
    }
    const actions = homeNode("div", "automation-row-actions");
    if (task.status === "paused") {
      const resume = homeNode("button", "text-button", "恢复");
      resume.dataset.automationAction = "resume";
      resume.dataset.automationId = task.id;
      actions.append(resume);
    } else if (["pending", "claimed"].includes(task.status)) {
      const pause = homeNode("button", "text-button", "暂停");
      pause.dataset.automationAction = "pause";
      pause.dataset.automationId = task.id;
      actions.append(pause);
    }
    if (["pending", "claimed", "paused", "unknown", "failed"].includes(task.status)) {
      const cancel = homeNode("button", "text-button", "取消");
      cancel.dataset.automationAction = "cancel";
      cancel.dataset.automationId = task.id;
      actions.append(cancel);
    }
    row.append(main, actions);
    list.append(row);
  });
}

async function loadAutomations() {
  try {
    const result = await request("/api/automations");
    renderAutomations(result.tasks);
  } catch (error) {
    const list = $("#automation-list");
    if (list) list.textContent = "任务读取失败：" + error.message;
  }
}

$("#automation-refresh")?.addEventListener("click", () => loadAutomations());
$("#automation-list")?.addEventListener("click", async (event) => {
  const button = event.target.closest("[data-automation-action]");
  if (!button) return;
  const action = button.dataset.automationAction;
  const taskId = button.dataset.automationId;
  button.disabled = true;
  try {
    await request("/api/automations/" + encodeURIComponent(taskId) + "/action", {
      method: "POST", body: JSON.stringify({action}), timeoutMs: 15000,
    });
    await loadAutomations();
  } catch (error) {
    showToast("任务操作失败：" + error.message, true);
    button.disabled = false;
  }
});

$$("[data-chat-mode]").forEach((button) => button.addEventListener("click", () => applyChatMode(button.dataset.chatMode)));

$("#chat-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const input = $("#chat-prompt");
  const prompt = input.value.trim();
  if (!prompt) return;
  appendChat("user", prompt);
  input.value = "";
  const conversationId = chatConversationId;
  await withBusy($("#chat-send"), async () => {
    const response = await request("/api/actions/chat", {
      method: "POST",
      body: JSON.stringify({ prompt, context: chatContext, conversation_id: conversationId }),
      // Reasoning models can legitimately take longer than the generic read
      // timeout.  The server-side AI timeout and retry budget remain the
      // final bound; this only prevents the panel from aborting early.
      timeoutMs: 180000,
    });
    if (chatConversationId === conversationId) appendChat("assistant", response.answer);
    await loadAutomations();
    await refreshAll(true);
  }).catch((error) => { if (chatConversationId === conversationId) appendChat("assistant", `请求失败：${error.message}`); });
});

$$("[data-prompt]").forEach((button) => button.addEventListener("click", () => {
  $("#chat-prompt").value = button.dataset.prompt;
  $("#chat-prompt").focus();
}));

// Enter 发送，Shift + Enter 换行。Ctrl / ⌘ + Enter 依旧有效，老习惯不受影响。
// 输入法组字期间必须放过 Enter：中文选词也是 Enter 确认，不放行会把半截拼音发出去。
$("#chat-prompt").addEventListener("keydown", (event) => {
  if (event.key !== "Enter" || event.isComposing || event.keyCode === 229) return;
  if (event.shiftKey || event.altKey) return;
  event.preventDefault();
  $("#chat-form").requestSubmit();
});

$("#clear-chat").addEventListener("click", async (event) => {
  await withBusy(event.currentTarget, async () => {
    // Clearing the window used to be cosmetic: the assistant kept its memory.
    await request("/api/actions/clear-context", { method: "POST", body: JSON.stringify({conversation_id: chatConversationId}) });
    chatConversationId = newChatConversationId();
    $("#chat-log").replaceChildren();
    appendChat("assistant", "本窗口的对话和上下文已清空。");
  }).catch((error) => appendChat("assistant", `清空失败：${error.message}`));
});

$("#stop-turn").addEventListener("click", async (event) => {
  await withBusy(event.currentTarget, async () => {
    await request("/api/actions/stop-turn", { method: "POST", body: "{}" });
    showToast("当前回答已中断");
    await refreshAll(true);
  }).catch((error) => showToast(error.message, true));
});

$("#clear-context").addEventListener("click", async (event) => {
  await withBusy(event.currentTarget, async () => {
    await request("/api/actions/clear-context", { method: "POST", body: "{}" });
    showToast("对话上下文已清空");
    await refreshAll(true);
  }).catch((error) => showToast(error.message, true));
});

async function pollXiaomiLogin() {
  clearTimeout(loginPollTimer);
  try {
    const login = await request("/api/auth/xiaomi/status");
    loginTarget = login.sid || loginTarget;
    const stateSelector = loginTarget === "xiaomiio" ? "#home-auth-pill" : "#xiaomi-login-state";
    text(stateSelector, login.message || login.state);
    if (login.state === "waiting" || login.state === "starting") {
      loginPollTimer = setTimeout(pollXiaomiLogin, 1800);
    } else if (login.state === "success") {
      showToast(login.sid === "xiaomiio" ? "米家授权成功" : "小米账号登录成功，正在连接音箱");
      if (login.sid === "xiaomiio") {
        await request("/api/home/discover", { method: "POST", body: "{}" }).catch((error) => showToast(error.message, true));
      }
      await refreshAll(true);
    } else if (login.state === "error") {
      showToast(login.message || "小米账号登录失败", true);
    }
  } catch (error) {
    text("#xiaomi-login-state", error.message);
  }
}

$("#xiaomi-web-login").addEventListener("click", async (event) => {
  loginTarget = "micoapi";
  const popup = window.open("about:blank", "xiaomi-account-login", "width=520,height=760");
  await withBusy(event.currentTarget, async () => {
    const login = await request("/api/auth/xiaomi/start", { method: "POST", body: "{}" });
    text("#xiaomi-login-state", login.message || "等待登录");
    if (popup) {
      popup.opener = null;
      popup.location.replace(login.login_url);
    } else {
      window.open(login.login_url, "_blank", "noopener");
    }
    pollXiaomiLogin();
  }).catch((error) => {
    if (popup) popup.close();
    showToast(error.message, true);
  });
});

$("#home-authorize").addEventListener("click", async (event) => {
  loginTarget = "xiaomiio";
  const popup = window.open("about:blank", "xiaomi-home-login", "width=520,height=760");
  await withBusy(event.currentTarget, async () => {
    const login = await request("/api/auth/xiaomi/start", {
      method: "POST",
      body: JSON.stringify({ sid: "xiaomiio" }),
    });
    if (popup) {
      popup.opener = null;
      popup.location.replace(login.login_url);
    } else {
      window.open(login.login_url, "_blank", "noopener");
    }
    pollXiaomiLogin();
  }).catch((error) => {
    if (popup) popup.close();
    showToast(error.message, true);
  });
});

$("#home-discover").addEventListener("click", async (event) => {
  await withBusy(event.currentTarget, async () => {
    const home = await request("/api/home/discover", { method: "POST", body: "{}" });
    renderHome(home);
    showToast(`已发现 ${(home.devices || []).length} 台设备，打开详情查看完整能力`);
  }).catch((error) => showToast(error.message, true));
});

$("#home-test-device").addEventListener("change", updateHomeActionOptions);
$("#home-room-filter").addEventListener("change", (event) => {
  homeRoomFilter = event.target.value;
  renderHomeCards();
});
$("#home-layout-refresh").addEventListener("click", () => loadHomeLayout(true));
["status", "type", "source", "role", "search"].forEach((name) => {
  $( `#hub-field-${name}`).addEventListener(name === "search" ? "input" : "change", (event) => {
    const resource = currentHub?.resources?.find((item) => item.resource_id === currentHubSelection);
    if (!resource) return;
    const filters = hubFieldFilters.get(resource.resource_id) || { status: "all", type: "all", source: "all", role: "all", search: "" };
    filters[name] = event.target.value;
    hubFieldFilters.set(resource.resource_id, filters);
    renderHubFields(resource);
  });
});
$("#hub-category-tabs").addEventListener("click", (event) => {
  const button = event.target.closest("[data-content-category]");
  const resource = currentHub?.resources?.find((item) => item.resource_id === currentHubSelection);
  if (!button || !resource) return;
  hubContentGroups.set(resource.resource_id, button.dataset.contentCategory);
  renderHubContentFilter(resource);
});
$("#hub-diagnostic-summary").addEventListener("click", (event) => {
  const button = event.target.closest("[data-diagnostic-filter]");
  const resource = currentHub?.resources?.find((item) => item.resource_id === currentHubSelection);
  if (!button || !resource) return;
  hubFieldFilters.set(resource.resource_id, { status: button.dataset.diagnosticFilter, type: "all", source: "all", role: "all", search: "" });
  $("#hub-field-advanced").open = true;
  renderHubFields(resource);
  $("#hub-field-advanced").scrollIntoView?.({ behavior: "smooth", block: "nearest" });
});
$("#hub-settings-control").addEventListener("click", () => $("#hub-detail-control").click());
$("#home-category-tabs").addEventListener("click", (event) => {
  const button = event.target.closest("[data-home-category]");
  if (!button) return;
  homeCategory = button.dataset.homeCategory;
  renderHomeCategories();
  renderHomeCards();
});
$("#home-detail-close").addEventListener("click", () => {
  homeSelectedKey = "";
  homeDetailSignature = "";
  renderHomeCards();
  renderHomeDetail();
});
$("#home-detail-refresh").addEventListener("click", () => refreshHomeDevice(homeSelectedKey));
$("#home-detail-history").addEventListener("click", () => {
  const device = currentHome?.devices?.find((item) => item.key === homeSelectedKey);
  if (!device) return;
  showView("data");
  selectHubResource(homeResourceId(device), true);
});
$("#home-detail-health").addEventListener("click", () => showView("health"));
$("#hub-detail-control").addEventListener("click", () => {
  const resource = currentHub?.resources?.find((item) => item.resource_id === currentHubSelection);
  const device = resource && deviceForResource(resource);
  if (device) openHomeDevice(device.key);
});
$("#home-test-action").addEventListener("change", updateHomeActionOptions);
$("#home-test-run").addEventListener("click", async (event) => {
  const output = $("#home-test-output");
  output.textContent = "正在执行并回读状态…";
  await withBusy(event.currentTarget, async () => {
    const response = await request("/api/home/test", {
      method: "POST",
      body: JSON.stringify({
        device: $("#home-test-device").value,
        action: $("#home-test-action").value,
        value: $("#home-test-value").value,
      }),
    });
    output.replaceChildren();
    const title = document.createElement("p");
    title.className = "environment-source";
    title.textContent = `${$("#home-test-device").value} · 刚刚读取`;
    const metrics = document.createElement("div");
    metrics.className = "resource-metrics";
    const device = currentHome?.devices?.find((item) => item.alias === $("#home-test-device").value);
    appendMetricCards(metrics, response.state || {}, { properties: device?.properties || [] });
    output.append(title, metrics);
    showToast("设备操作已完成并回读");
    await refreshAll(true);
  }).catch((error) => { output.textContent = error.message; showToast(error.message, true); });
});

$("#test-ai").addEventListener("click", async (event) => {
  const output = $("#ai-test-output");
  output.textContent = "正在请求模型…";
  await withBusy(event.currentTarget, async () => {
    const response = await request("/api/actions/test-ai", {
      method: "POST",
      body: "{}",
      timeoutMs: 180000,
    });
    output.textContent = response.answer;
    showToast("AI 测试成功");
  }).catch((error) => { output.textContent = error.message; showToast(error.message, true); });
});

/* ------------------------------------------------------------ 模型中枢控制台 */

const MC_REASONING = { auto: "自动", off: "关闭", low: "低", medium: "中", high: "高" };
const MC_PROBE_SEP = "\u0000";
const MC_BATCH_LIMIT = 60;

const mc = {
  registry: null,
  probes: {},
  usage: null,
  logs: { total: 0, limit: 50, offset: 0, items: [], loaded: false, error: "" },
  logFilter: { provider: "", model: "", errors: false },
  usageHours: 24,
  csView: "overview",
  channel: "",
  channelQuery: "",
  channelBusy: false,
  modelQuery: "",
  modelFilter: "all",
  // 跨渠道选择：键是 `${providerId}\u0000${model}`，因为模型 ID 在不同渠道间可能重名。
  selection: new Set(),
  drawerProvider: "",
  drawerModel: "",
  drawerTrigger: null,
  connectProtocol: "openai",
  routeProvider: "",
  routeModels: new Map(),
  routeBusy: false,
  loading: false,
};

function mcEntryKey(providerId, model) {
  return mcProbeKey(providerId, model);
}

function mcSplitKey(key) {
  const index = key.indexOf(MC_PROBE_SEP);
  return [key.slice(0, index), key.slice(index + 1)];
}

function mcFindEntry(providerId, model) {
  const provider = (mc.registry?.providers || []).find((item) => item.id === providerId);
  if (!provider || !(provider.models || []).includes(model)) return null;
  return { provider, model };
}

function mcProbeKey(providerId, model) {
  return `${providerId}${MC_PROBE_SEP}${model}`;
}

function mcProbe(providerId, model) {
  return mc.probes[mcProbeKey(providerId, model)] || null;
}

function mcNumber(value) {
  return typeof value === "number" && Number.isFinite(value) ? value.toLocaleString("zh-CN") : "—";
}

function mcSettings(provider, model) {
  const found = (provider?.model_settings || []).find((item) => item.model === model);
  return found || { model, reasoning_level: "auto", context_window: 128000, max_output_tokens: 4096, temperature: 0.3, multimodal: false };
}

function mcModelCount(provider) {
  return (provider?.models || []).length;
}

function mcHighlight(value, query) {
  const host = document.createElement("span");
  const needle = (query || "").trim().toLowerCase();
  const index = needle ? value.toLowerCase().indexOf(needle) : -1;
  if (index < 0) {
    host.textContent = value;
    return host;
  }
  host.append(value.slice(0, index));
  const mark = document.createElement("mark");
  mark.textContent = value.slice(index, index + needle.length);
  host.append(mark, value.slice(index + needle.length));
  return host;
}

function mcBadge(label, tone) {
  return homeNode("span", `badge ${tone || "tone-idle"}`, label);
}

function mcRelative(value) {
  if (!value) return "尚未测试";
  const moment = new Date(value).getTime();
  if (!Number.isFinite(moment)) return "尚未测试";
  const seconds = Math.max(0, Math.round((Date.now() - moment) / 1000));
  if (seconds < 60) return `${seconds} 秒前`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)} 分钟前`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} 小时前`;
  return `${Math.floor(seconds / 86400)} 天前`;
}

/* ------------------------------------------------------------------ 渲染 */

/**
 * Whether the gateway really demands a bearer token.
 *
 * `auth_required` is the authoritative field; `token_required` is the older
 * name and is only a fallback for an older backend. Reading the wrong one made
 * a keyless gateway advertise itself as token-protected.
 */
function gatewayAuthRequired() {
  const gateway = mc.registry?.gateway || {};
  if (typeof gateway.auth_required === "boolean") return gateway.auth_required;
  return Boolean(gateway.token_required);
}

function renderMcGateway() {
  const origin = location.origin;
  text("#gateway-openai-base", `${origin}/v1`);
  text("#gateway-anthropic-base", origin);
  const providers = mc.registry?.providers || [];
  const enabled = providers.filter((provider) => provider.enabled);
  const models = new Set(enabled.flatMap((provider) => provider.models || []));
  const protectedGateway = gatewayAuthRequired();
  text("#gateway-token-state", protectedGateway ? "密钥校验已启用" : "免密访问 · Key 可填 local");
  const active = mc.registry?.active || {};
  const provider = providers.find((item) => item.id === active.provider_id);
  const hasActive = provider?.enabled && provider.models?.includes(active.model);
  text("#gateway-active-model", hasActive ? active.model : "尚未设置默认模型");
  text("#gateway-active-provider", hasActive ? provider.name : "选择一个已启用模型，设为当前即可");
  const chain = mc.registry?.fallback || [];
  text(
    "#gateway-fallback-chain",
    chain.length
      ? `备用模型（${chain.filter((item) => item.available).length}/${chain.length} 可用）：${chain.map((item) => item.model).join(" → ")}`
      : "备用模型：未设置，主模型不通时直接报错",
  );
  $("#gateway-locate-active").disabled = !hasActive;
  setPill("#model-gateway-status", models.size ? "网关就绪" : "等待接入", models.size ? "ok" : null);
  text("#console-model-count", String(models.size));
  text("#console-provider-count", String(providers.length));
  text("#console-provider-note", `${enabled.length} 个已启用`);
}

function renderMcSummary() {
  const data = mc.usage;
  const range = mc.usageHours >= 24 ? `近 ${mc.usageHours / 24} 天` : `近 ${mc.usageHours} 小时`;
  const label = mc.usageHours === 24 ? "近 24h 请求" : `${range}请求`;
  text("#console-requests-label", label);
  text("#drawer-usage-label", label);
  text("#console-request-count", data ? mcNumber(data.total) : "—");
  text("#console-request-note", data ? (data.total ? `成功 ${mcNumber(data.ok)} · 失败 ${mcNumber(data.failed)}` : "还没有请求记录") : "统计暂不可用");
  text("#overview-success", data && data.success_rate !== null ? `${data.success_rate}%` : "—");
  text("#overview-success-note", data && data.total ? `平均 ${mcNumber(data.avg_latency_ms)}ms · Token ${mcNumber(data.tokens)}` : "近 24 小时");
}

/* --------------------------------------------------------- 概览与上游渠道 */

function mcChannels() {
  return mc.registry?.providers || [];
}

function mcVisibleChannels() {
  const needle = mc.channelQuery.trim().toLowerCase();
  const providers = mcChannels();
  if (!needle) return providers;
  return providers.filter((provider) => (
    provider.name.toLowerCase().includes(needle)
    || (provider.base_url || "").toLowerCase().includes(needle)
    || (provider.models || []).some((model) => model.toLowerCase().includes(needle))
  ));
}

function mcChannelStatus(provider) {
  const active = mc.registry?.active || {};
  if (!provider.enabled) return { tone: "off", label: "已停用" };
  if (!mcModelCount(provider)) return { tone: "warn", label: "无模型" };
  if (active.provider_id === provider.id) return { tone: "on", label: "使用中" };
  const tested = (provider.models || []).map((model) => mcProbe(provider.id, model)).filter(Boolean);
  if (!tested.length) return { tone: "", label: "待测试" };
  return tested.some((probe) => probe.ok) ? { tone: "on", label: "可用" } : { tone: "bad", label: "全部失败" };
}

function mcStatusNode(tone, label) {
  const node = homeNode("span", `cs-status ${tone || ""}`.trim());
  node.append(homeNode("i", "cs-dot"), homeNode("span", "cs-status-label", label));
  return node;
}

function mcChannelProbeSummary(provider) {
  const models = provider.models || [];
  if (!models.length) return "—";
  const tested = models.map((model) => mcProbe(provider.id, model)).filter(Boolean);
  if (!tested.length) return "尚未测试";
  const passed = tested.filter((probe) => probe.ok).length;
  const latest = tested.reduce((best, probe) => (probe.tested_at > best ? probe.tested_at : best), "");
  return `${passed}/${tested.length} 通过 · ${mcRelative(latest)}`;
}

function mcChannelOps(provider, detailed) {
  const ops = homeNode("div", "cs-ops");
  const add = (label, action, danger) => {
    const button = homeNode("button", danger ? "secondary-button danger-button" : "secondary-button", label);
    button.type = "button";
    button.dataset.csAct = action;
    button.dataset.csProvider = provider.id;
    ops.append(button);
  };
  add("测试", "test");
  if (detailed) add("获取模型", "discover");
  add("编辑", "edit");
  add(provider.enabled ? "停用" : "启用", "toggle");
  if (detailed) add("删除", "remove", true);
  return ops;
}

function renderChannelRows(selector, { detailed = false } = {}) {
  const body = $(selector);
  if (!body) return 0;
  const providers = mcVisibleChannels();
  const active = mc.registry?.active || {};
  body.replaceChildren();
  for (const provider of providers) {
    const status = mcChannelStatus(provider);
    const row = document.createElement("tr");
    if (active.provider_id === provider.id) row.classList.add("is-active");

    const statusCell = document.createElement("td");
    statusCell.append(mcStatusNode(status.tone, status.label));
    row.append(statusCell);

    const nameCell = document.createElement("td");
    nameCell.append(homeNode("div", "cs-channel-name", provider.name));
    nameCell.append(homeNode("div", "cs-model-name", provider.protocol === "anthropic" ? "Anthropic Messages" : "OpenAI 兼容"));
    row.append(nameCell);

    const addressCell = homeNode("td", "cs-mono", provider.base_url || "未填写地址");
    addressCell.title = provider.base_url || "";
    if (provider.proxy_set) {
      const badge = homeNode("span", "cs-proxy-badge", "经代理");
      badge.title = `上游经代理 ${provider.proxy_display}`
        + (provider.proxy_auth_set ? "（含凭据）" : "");
      addressCell.append(badge);
    }
    row.append(addressCell);
    row.append(homeNode("td", "num", String(mcModelCount(provider))));
    row.append(homeNode("td", "cs-model-name", mcChannelProbeSummary(provider)));

    const opsCell = document.createElement("td");
    opsCell.append(mcChannelOps(provider, detailed));
    row.append(opsCell);
    body.append(row);
  }
  const empty = detailed ? $("#channel-empty") : null;
  if (empty) empty.hidden = mcChannels().length > 0;
  return providers.length;
}

function renderChannels() {
  const all = mcChannels();
  renderChannelRows("#channel-table-body", { detailed: true });
  const totalModels = all.reduce((total, provider) => total + mcModelCount(provider), 0);
  text("#csnav-channel-count", String(all.length));
  text("#csnav-model-count", String(totalModels));
  text("#console-provider-count", String(all.length));
  text("#console-provider-note", `${all.filter((provider) => provider.enabled).length} 个已启用`);
  text("#console-model-count", String(totalModels));
}

function renderMcOverview() {
  renderChannelRows("#overview-channel-body");
  renderMcSummary();
  text("#cs-endpoint-key", gatewayAuthRequired() ? "网关令牌（NAS 上的 ai-gateway-token）" : "local（免密，任意非空值均可）");
  renderMcChart(mc.usage?.series || [], "#overview-chart");
}

/* --------------------------------------------------------------- 模型广场 */

function mcVisibleEntries() {
  const needle = mc.modelQuery.trim().toLowerCase();
  const active = mc.registry?.active || {};
  const entries = [];
  for (const provider of mcChannels()) {
    if (mc.channel && provider.id !== mc.channel) continue;
    for (const model of provider.models || []) {
      if (needle && !model.toLowerCase().includes(needle)) continue;
      const probe = mcProbe(provider.id, model);
      if (mc.modelFilter === "active" && !(active.provider_id === provider.id && active.model === model)) continue;
      if (mc.modelFilter === "passed" && !(probe && probe.ok)) continue;
      if (mc.modelFilter === "failed" && !(probe && !probe.ok)) continue;
      if (mc.modelFilter === "untested" && probe) continue;
      entries.push({ provider, model, probe, settings: mcSettings(provider, model) });
    }
  }
  return entries;
}

function mcOpsButton(label, action, entry, danger) {
  const button = homeNode("button", danger ? "secondary-button danger-button" : "secondary-button", label);
  button.type = "button";
  button.dataset.mcAct = action;
  button.dataset.mcProvider = entry.provider.id;
  button.dataset.mcModel = entry.model;
  button.setAttribute("aria-label", `${label} ${entry.model}`);
  return button;
}

function mcFallbackKeys() {
  return new Set((mc.registry?.fallback || []).map((item) => mcEntryKey(item.provider_id, item.model)));
}

async function mcToggleFallback(providerId, model) {
  const chain = (mc.registry?.fallback || []).map((item) => ({ provider_id: item.provider_id, model: item.model }));
  const key = mcEntryKey(providerId, model);
  const exists = chain.some((item) => mcEntryKey(item.provider_id, item.model) === key);
  const next = exists
    ? chain.filter((item) => mcEntryKey(item.provider_id, item.model) !== key)
    : [...chain, { provider_id: providerId, model }];
  try {
    const payload = await request("/api/models/actions/fallback", {
      method: "POST",
      body: JSON.stringify({ fallback: next, revision: mc.registry?.revision }),
    });
    mc.registry = { ...mc.registry, fallback: payload.fallback, revision: payload.revision };
    renderModelRegistry();
    showToast(exists ? `已移出备用模型：${model}` : `已加入备用模型：${model}`);
  } catch (error) {
    showToast(error.message, true);
  }
}

function renderMcModels() {
  const body = $("#model-table-body");
  if (!body) return;
  const entries = mcVisibleEntries();
  const active = mc.registry?.active || {};
  body.replaceChildren();
  for (const entry of entries) {
    const isActive = active.provider_id === entry.provider.id && active.model === entry.model;
    const row = document.createElement("tr");
    if (isActive) row.classList.add("is-active");

    const checkCell = document.createElement("td");
    const check = document.createElement("input");
    check.type = "checkbox";
    check.checked = mc.selection.has(mcEntryKey(entry.provider.id, entry.model));
    check.dataset.mcSelect = mcEntryKey(entry.provider.id, entry.model);
    check.setAttribute("aria-label", `选择模型 ${entry.model}`);
    checkCell.append(check);
    row.append(checkCell);

    const idCell = document.createElement("td");
    const idWrap = homeNode("div", "model-id-cell");
    const idText = homeNode("div", "model-id");
    idText.append(mcHighlight(entry.model, mc.modelQuery));
    const tags = homeNode("div", "model-tags");
    tags.append(isActive ? mcBadge("当前使用", "tone-accent") : mcBadge("待命", "tone-idle"));
    if (entry.settings.multimodal) tags.append(mcBadge("多模态", "tone-ok"));
    const isFallback = mcFallbackKeys().has(mcEntryKey(entry.provider.id, entry.model));
    if (isFallback) tags.append(mcBadge("备用", "tone-accent"));
    idWrap.append(idText, tags);
    idCell.append(idWrap);
    row.append(idCell);

    const channelCell = document.createElement("td");
    const jump = homeNode("button", "text-button", entry.provider.name);
    jump.type = "button";
    jump.dataset.mcChannelJump = entry.provider.id;
    jump.title = `只看「${entry.provider.name}」的模型`;
    channelCell.append(jump);
    row.append(channelCell);

    const scale = (value) => (value >= 1000 ? `${Math.round(value / 1000)}K` : String(value));
    const paramsCell = homeNode("td", "model-params");
    paramsCell.textContent = `${MC_REASONING[entry.settings.reasoning_level] || "自动"} · ${scale(entry.settings.context_window)}/${scale(entry.settings.max_output_tokens)} · T=${entry.settings.temperature}`;
    paramsCell.title = `思考 ${MC_REASONING[entry.settings.reasoning_level] || "自动"}；上下文窗口 ${entry.settings.context_window}；最大输出 ${entry.settings.max_output_tokens}；温度 ${entry.settings.temperature}`;
    row.append(paramsCell);

    const probeCell = document.createElement("td");
    if (entry.probe) {
      const badge = mcBadge(entry.probe.ok ? `通过 ${entry.probe.latency_ms}ms` : `失败 ${entry.probe.status || "—"}`, entry.probe.ok ? "tone-ok" : "tone-bad");
      badge.title = `${entry.probe.message || ""}（${mcRelative(entry.probe.tested_at)}）`;
      probeCell.append(badge);
    } else {
      probeCell.append(mcBadge("未测试", "tone-idle"));
    }
    row.append(probeCell);

    const opsCell = document.createElement("td");
    const ops = homeNode("div", "cs-ops");
    if (!isActive) ops.append(mcOpsButton("设为当前", "activate", entry));
    if (!isActive) ops.append(mcOpsButton(isFallback ? "取消备用" : "设为备用", "fallback", entry));
    ops.append(mcOpsButton("测试", "test", entry), mcOpsButton("设置", "settings", entry));
    if (!isActive) ops.append(mcOpsButton("删除", "remove", entry, true));
    opsCell.append(ops);
    row.append(opsCell);
    body.append(row);
  }
  const empty = $("#model-table-empty");
  if (empty) {
    empty.hidden = entries.length > 0;
    if (!entries.length) {
      empty.textContent = mcChannels().length
        ? "没有匹配的模型。换个关键词，或把渠道切回「全部渠道」。"
        : "还没有渠道。先到「上游渠道」添加地址、密钥和模型。";
    }
  }
  const selectAll = $("#model-select-all");
  if (selectAll) {
    selectAll.checked = entries.length > 0 && entries.every((entry) => mc.selection.has(mcEntryKey(entry.provider.id, entry.model)));
    selectAll.indeterminate = !selectAll.checked && entries.some((entry) => mc.selection.has(mcEntryKey(entry.provider.id, entry.model)));
  }
  updateMcSelection();
}

function updateMcSelection() {
  const count = mc.selection.size;
  const label = $("#model-selection");
  if (label) {
    label.textContent = count ? `已选中 ${count} 个模型。` : "未选中模型。";
    label.classList.toggle("has-selection", count > 0);
  }
  text("#model-visible-count", `显示 ${mcVisibleEntries().length} 个`);
  for (const id of ["#model-test-selected", "#model-remove-selected"]) {
    const node = $(id);
    if (node) node.disabled = count === 0;
  }
}

function renderMcChannelFilter() {
  const select = $("#model-channel");
  if (!select) return;
  const providers = mcChannels();
  select.replaceChildren(new Option(`全部渠道（${providers.length}）`, ""));
  for (const provider of providers) select.append(new Option(`${provider.name}（${mcModelCount(provider)}）`, provider.id));
  select.value = providers.some((provider) => provider.id === mc.channel) ? mc.channel : "";
  mc.channel = select.value;
}

function mcEntryFromKey(key) {
  if (!key || key.indexOf(MC_PROBE_SEP) < 0) return null;
  const [providerId, model] = mcSplitKey(key);
  return mcFindEntry(providerId, model);
}

function renderDefaultModelSelect() {
  const select = $("#default-model-select");
  if (!select) return;
  const providers = (mc.registry?.providers || []).filter((provider) => provider.enabled && (provider.models || []).length);
  const active = mc.registry?.active || {};
  const desired = active.provider_id && active.model ? mcProbeKey(active.provider_id, active.model) : "";
  select.replaceChildren();
  let present = false;
  for (const provider of providers) {
    const group = document.createElement("optgroup");
    group.label = provider.name;
    for (const model of provider.models) {
      const key = mcProbeKey(provider.id, model);
      if (key === desired) present = true;
      group.append(new Option(model, key));
    }
    select.append(group);
  }
  select.value = present ? desired : (select.options[0]?.value || "");
  renderDefaultModelNote();
}

function renderDefaultModelNote() {
  const select = $("#default-model-select");
  const apply = $("#default-model-apply");
  const result = $("#default-model-result");
  if (!select || !apply) return;
  const entry = mcEntryFromKey(select.value);
  const active = mc.registry?.active || {};
  const isCurrent = Boolean(entry) && entry.provider.id === active.provider_id && entry.model === active.model;
  apply.disabled = !entry || isCurrent;
  apply.textContent = isCurrent ? "✓ 当前默认" : "设为默认";
  if (result) {
    result.textContent = entry
      ? (isCurrent ? `${entry.provider.name} 正在服务，音箱与 hub-active 都跟随它。` : `将把默认模型切换到 ${entry.provider.name} → ${entry.model}。`)
      : "没有可选的已启用模型，请先启用一个渠道。";
    result.classList.remove("is-error");
  }
}

function renderRoleList() {
  const host = $("#role-list");
  if (!host) return;
  const catalog = mc.registry?.role_catalog || [];
  const roles = mc.registry?.roles || {};
  const providers = (mc.registry?.providers || []).filter((provider) => provider.enabled && (provider.models || []).length);
  const active = mc.registry?.active || {};
  host.replaceChildren();
  for (const role of catalog) {
    const current = roles[role.id] || {};
    const following = !current.provider_id || !current.model;
    const row = homeNode("div", "cs-role-row");
    row.dataset.role = role.id;
    if (following) row.classList.add("is-following");

    const meta = homeNode("div", "cs-role-meta");
    meta.append(homeNode("strong", "", role.label));
    meta.append(homeNode("small", "", following
      ? (active.model ? `跟随默认模型 · ${active.model}` : "跟随默认模型")
      : `指定为 ${current.model}`));

    const controls = homeNode("div", "cs-role-controls");
    const select = document.createElement("select");
    select.className = "console-input";
    select.dataset.roleSelect = role.id;
    select.setAttribute("aria-label", role.label + " 使用的模型");
    select.append(new Option("跟随默认模型", ""));
    for (const provider of providers) {
      const group = document.createElement("optgroup");
      group.label = provider.name;
      for (const model of provider.models) group.append(new Option(model, mcProbeKey(provider.id, model)));
      select.append(group);
    }
    const desired = following ? "" : mcProbeKey(current.provider_id, current.model);
    select.value = [...select.options].some((option) => option.value === desired) ? desired : "";
    const apply = homeNode("button", "primary-button", "应用");
    apply.type = "button";
    apply.dataset.roleApply = role.id;
    controls.append(select, apply);

    const result = document.createElement("output");
    result.className = "cs-role-result";
    result.id = "role-result-" + role.id;
    result.setAttribute("role", "status");

    row.append(meta, controls, result);
    host.append(row);
    renderRoleNote(role.id);
  }
}

function renderRoleNote(roleId) {
  const select = document.querySelector('[data-role-select="' + roleId + '"]');
  const apply = document.querySelector('[data-role-apply="' + roleId + '"]');
  const result = document.getElementById("role-result-" + roleId);
  if (!select || !apply) return;
  const current = (mc.registry?.roles || {})[roleId] || {};
  const following = !current.provider_id || !current.model;
  const settled = following ? "" : mcProbeKey(current.provider_id, current.model);
  const same = select.value === settled;
  const entry = mcEntryFromKey(select.value);
  apply.disabled = same;
  apply.textContent = same ? "✓ 已应用" : "应用";
  if (!result) return;
  result.classList.remove("is-error");
  if (same) {
    result.textContent = following
      ? "当前跟随默认模型，切换默认模型会一并改变它。"
      : (entry ? "当前使用 " + entry.provider.name + " → " + current.model + "。" : "当前已指定模型。");
  } else if (!select.value) {
    result.textContent = "应用后将改为跟随默认模型。";
  } else {
    result.textContent = entry ? "应用后将使用 " + entry.provider.name + " → " + entry.model + "。" : "请选择一个模型。";
  }
}

function renderModelRegistry() {
  renderMcGateway();
  renderChannels();
  renderMcChannelFilter();
  renderDefaultModelSelect();
  renderRoleList();
  renderMcModels();
  renderMcRoutes();
  renderMcOverview();
  if (mc.drawerModel) renderMcDrawer();
}

/* ------------------------------------------------------------------ 抽屉 */

function mcDrawerEntry() {
  return mcFindEntry(mc.drawerProvider, mc.drawerModel);
}

function renderMcDrawer() {
  const entry = mcDrawerEntry();
  const drawer = $("#console-drawer");
  if (!drawer || !entry) return;
  const { provider, model } = entry;
  text("#drawer-model-name", model);
  text("#drawer-model-provider", `${provider.name} · ${provider.protocol === "anthropic" ? "Anthropic Messages" : "OpenAI 兼容"}`);
  const settings = mcSettings(provider, model);
  $("#model-settings-reasoning").value = MC_REASONING[settings.reasoning_level] ? settings.reasoning_level : "auto";
  $("#model-settings-context").value = settings.context_window;
  $("#model-settings-output").value = settings.max_output_tokens;
  $("#model-settings-temperature").value = settings.temperature;
  $("#model-settings-multimodal").checked = Boolean(settings.multimodal);
  const probe = mcProbe(provider.id, model);
  text("#drawer-probe", probe ? `${probe.ok ? "通过" : "失败"} · ${probe.latency_ms}ms · ${mcRelative(probe.tested_at)}` : "尚未测试");
  const usage = mc.usage?.models?.find((item) => item.key === model);
  text("#drawer-model-usage", usage ? `${mcNumber(usage.requests)} 次 · 失败 ${usage.failed} · 平均 ${usage.avg_latency_ms}ms` : "暂无请求记录");
}

function openMcDrawer(providerId, model, trigger) {
  mc.drawerProvider = providerId;
  mc.drawerModel = model;
  mc.drawerTrigger = trigger || document.activeElement;
  const drawer = $("#console-drawer");
  drawer.hidden = false;
  renderMcDrawer();
  $("#model-settings-result").textContent = "";
  if (!drawer.open) drawer.showModal();
}

function closeMcDrawer() {
  const drawer = $("#console-drawer");
  if (!drawer) return;
  const wasOpen = drawer.open;
  mc.drawerProvider = "";
  mc.drawerModel = "";
  if (wasOpen) drawer.close();
  drawer.hidden = true;
  if (wasOpen) {
    if (mc.drawerTrigger?.isConnected) mc.drawerTrigger.focus();
    else $("#model-search")?.focus();
  }
  mc.drawerTrigger = null;
}

/* ------------------------------------------------------------------- 加载 */

async function loadModelProbes() {
  try {
    const payload = await request("/api/models/actions/probes");
    mc.probes = payload.probes || {};
  } catch (error) {
    mc.probes = {};
  }
}

async function loadModelRegistry() {
  if (mc.loading) return;
  mc.loading = true;
  try {
    mc.registry = await request("/api/models");
    const providers = mc.registry.providers || [];
    if (mc.channel && !providers.some((provider) => provider.id === mc.channel)) mc.channel = "";
    if (mc.drawerModel && !mcFindEntry(mc.drawerProvider, mc.drawerModel)) closeMcDrawer();
    for (const key of [...mc.selection]) {
      const [providerId, model] = mcSplitKey(key);
      if (!mcFindEntry(providerId, model)) mc.selection.delete(key);
    }
    await loadModelProbes();
    renderModelRegistry();
    renderMcLogFilters();
    await loadMcUsage();
    if (mc.logs.loaded) await loadMcLogs();
  } catch (error) {
    text("#model-gateway-status", "读取失败");
    showToast(error.message, true);
  } finally {
    mc.loading = false;
  }
}

async function loadMcUsage() {
  try {
    mc.usage = await request(`/api/models/actions/usage?hours=${mc.usageHours}`);
  } catch (error) {
    mc.usage = null;
  }
  renderMcUsage();
  renderMcSummary();
  renderMcOverview();
}

async function loadMcLogs() {
  const params = new URLSearchParams();
  params.set("limit", String(mc.logs.limit));
  params.set("offset", String(mc.logs.offset));
  if (mc.logFilter.provider) params.set("provider_id", mc.logFilter.provider);
  if (mc.logFilter.model) params.set("model", mc.logFilter.model);
  if (mc.logFilter.errors) params.set("errors", "1");
  try {
    const payload = await request(`/api/models/actions/logs?${params.toString()}`);
    mc.logs.total = payload.total || 0;
    mc.logs.items = payload.items || [];
    mc.logs.loaded = true;
    mc.logs.error = "";
  } catch (error) {
    mc.logs.items = [];
    mc.logs.total = 0;
    mc.logs.loaded = true;
    mc.logs.error = error.message;
  }
  renderMcLogs();
}

/* ------------------------------------------------------------------ 日志 */

function renderMcLogs() {
  const body = $("#log-table-body");
  if (!body) return;
  body.replaceChildren();
  const items = mc.logs.items || [];
  for (const item of items) {
    const row = document.createElement("tr");
    row.append(
      homeNode("td", "", formatTime(item.at)),
      homeNode("td", "", item.provider_name || item.provider_id || "—"),
      homeNode("td", "model-id", item.model || "—")
    );
    const surface = homeNode("td", "");
    surface.append(mcBadge({ openai: "OpenAI", anthropic: "Anthropic", responses: "Responses" }[item.surface] || item.surface || "—", "tone-idle"));
    row.append(surface);
    const statusCell = document.createElement("td");
    statusCell.className = "num";
    statusCell.append(mcBadge(String(item.status || "—"), item.ok ? "tone-ok" : "tone-bad"));
    row.append(statusCell, homeNode("td", "num", `${item.latency_ms}ms`));
    const tokens = item.total_tokens || (item.prompt_tokens + item.completion_tokens);
    row.append(homeNode("td", "num", mcNumber(tokens)));
    row.append(homeNode("td", "model-id", item.error || (item.ok ? "成功" : "—")));
    [...row.children].forEach((cell, index) => {
      cell.dataset.label = ["时间", "提供方", "模型", "入口", "状态", "耗时", "Tokens", "说明"][index];
    });
    body.append(row);
  }
  const empty = $("#log-empty");
  empty.hidden = items.length > 0;
  if (!items.length && mc.logs.error) empty.textContent = mc.logs.error;
  else if (!items.length) empty.textContent = "这个范围内还没有请求记录。";
  const pages = Math.max(1, Math.ceil(mc.logs.total / mc.logs.limit));
  const page = Math.floor(mc.logs.offset / mc.logs.limit) + 1;
  text("#log-page", `第 ${page} / ${pages} 页 · 共 ${mc.logs.total} 条`);
  $("#log-prev").disabled = mc.logs.offset <= 0;
  $("#log-next").disabled = page >= pages;
  text("#log-summary", mc.logs.total
    ? `共 ${mc.logs.total} 条转发记录；日志按天保留，可在存储受限时自动清理。`
    : "打开此页后读取网关请求记录。");
}

function renderMcLogFilters() {
  const providerSelect = $("#log-filter-provider");
  const modelSelect = $("#log-filter-model");
  if (!providerSelect || !modelSelect) return;
  const providers = mcChannels();
  providerSelect.replaceChildren(new Option("全部渠道", ""));
  for (const provider of providers) providerSelect.append(new Option(provider.name, provider.id));
  providerSelect.value = providers.some((provider) => provider.id === mc.logFilter.provider) ? mc.logFilter.provider : "";
  mc.logFilter.provider = providerSelect.value;
  const models = [...new Set(providers.flatMap((provider) => provider.models || []))].sort().slice(0, 400);
  modelSelect.replaceChildren(new Option("全部模型", ""));
  for (const model of models) modelSelect.append(new Option(model, model));
  modelSelect.value = models.includes(mc.logFilter.model) ? mc.logFilter.model : "";
  mc.logFilter.model = modelSelect.value;
}

/* ------------------------------------------------------------------ 统计 */

function renderMcUsage() {
  const data = mc.usage;
  text("#usage-total", data ? mcNumber(data.total) : "—");
  text("#usage-success", data && data.success_rate !== null ? `${data.success_rate}%` : "—");
  text("#usage-latency", data ? `${mcNumber(data.avg_latency_ms)}ms` : "—");
  text("#usage-p95", data ? `${mcNumber(data.p95_latency_ms)}ms` : "—");
  text("#usage-tokens", data ? mcNumber(data.tokens) : "—");
  renderMcChart(data?.series || []);
  renderMcUsageList("#usage-models", data?.models || [], (item) => `${mcNumber(item.requests)} 次 · 失败 ${item.failed} · ${item.avg_latency_ms}ms`);
  renderMcUsageList("#usage-providers", data?.providers || [], (item) => `${mcNumber(item.requests)} 次 · 失败 ${item.failed}`);
  renderMcUsageList("#usage-errors", (data?.errors || []).map((item) => ({
    key: item.message, label: item.message, requests: item.count, failed: item.count, count: item.count,
  })), (item) => `${mcNumber(item.count)} 次`, true);
}

function renderMcUsageList(selector, items, describe, danger) {
  const host = $(selector);
  if (!host) return;
  host.replaceChildren();
  if (!items.length) {
    host.append(homeNode("p", "hub-muted", "暂无数据。"));
    return;
  }
  const peak = Math.max(...items.map((item) => item.requests || item.failed || 1), 1);
  for (const item of items) {
    const row = homeNode("div", "usage-row");
    row.append(
      homeNode("span", "usage-label", item.label || item.key || "—"),
      homeNode("span", "usage-value", describe(item))
    );
    const bar = homeNode("div", "usage-bar");
    const fill = document.createElement("i");
    const ratio = Math.max(4, Math.round(((item.requests || item.failed || 0) / peak) * 100));
    fill.style.width = `${ratio}%`;
    if (danger) fill.classList.add("bad");
    bar.append(fill);
    row.append(bar);
    host.append(row);
  }
}

function renderMcChart(series, selector = "#usage-chart") {
  const host = $(selector);
  if (!host) return;
  host.replaceChildren();
  if (!series.length) {
    host.append(homeNode("p", "console-empty", "这个范围内还没有请求。"));
    return;
  }
  const ns = "http://www.w3.org/2000/svg";
  const barWidth = 22;
  const gap = 9;
  const chartHeight = 116;
  const labelBand = 20;
  const width = series.length * (barWidth + gap) + 4;
  const height = chartHeight + labelBand;
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  svg.setAttribute("width", String(width));
  svg.setAttribute("height", String(height));
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", `最近请求量，共 ${series.reduce((total, item) => total + item.total, 0)} 次`);
  const peak = Math.max(1, ...series.map((item) => item.total));
  const step = Math.max(1, Math.ceil(series.length / 8));
  series.forEach((item, index) => {
    const x = 2 + index * (barWidth + gap);
    const totalHeight = item.total ? Math.max(3, Math.round((item.total / peak) * chartHeight)) : 0;
    const failedHeight = item.failed ? Math.max(2, Math.round((item.failed / peak) * chartHeight)) : 0;
    if (totalHeight) {
      const bar = document.createElementNS(ns, "rect");
      bar.setAttribute("x", String(x));
      bar.setAttribute("y", String(chartHeight - totalHeight));
      bar.setAttribute("width", String(barWidth));
      bar.setAttribute("height", String(totalHeight));
      bar.setAttribute("rx", "3");
      bar.setAttribute("fill", "#a9cec0");
      const title = document.createElementNS(ns, "title");
      title.textContent = `${formatTime(item.at)}：${item.total} 次请求，失败 ${item.failed}`;
      bar.append(title);
      svg.append(bar);
    }
    if (failedHeight) {
      const fail = document.createElementNS(ns, "rect");
      fail.setAttribute("x", String(x));
      fail.setAttribute("y", String(chartHeight - failedHeight));
      fail.setAttribute("width", String(barWidth));
      fail.setAttribute("height", String(failedHeight));
      fail.setAttribute("rx", "3");
      fail.setAttribute("fill", "#d99a92");
      svg.append(fail);
    }
    if (index % step === 0) {
      const label = document.createElementNS(ns, "text");
      const moment = new Date(item.at);
      const hours = series.length <= 24;
      label.textContent = Number.isFinite(moment.getTime())
        ? (hours
          ? `${String(moment.getHours()).padStart(2, "0")}:00`
          : `${moment.getMonth() + 1}/${moment.getDate()}`)
        : "";
      label.setAttribute("x", String(x + barWidth / 2));
      label.setAttribute("y", String(chartHeight + 14));
      label.setAttribute("text-anchor", "middle");
      label.setAttribute("font-size", "9");
      label.setAttribute("fill", "#5d6d61");
      svg.append(label);
    }
  });
  host.append(svg);
}

/* ------------------------------------------------------------------ 交互 */

function mcParseModels(value) {
  return [...new Set(String(value || "").split(/[\n,]+/).map((item) => item.trim()).filter(Boolean))];
}

function mcFind(providerId, model) {
  return Boolean(mcFindEntry(providerId, model));
}

function mcSelectedEntries() {
  return [...mc.selection]
    .map((key) => mcFindEntry(...mcSplitKey(key)))
    .filter(Boolean);
}

function showConsoleView(name) {
  mc.csView = name;
  document.querySelectorAll("[data-cs-view]").forEach((node) => {
    const selected = node.dataset.csView === name;
    node.classList.toggle("active", selected);
    node.setAttribute("aria-selected", selected ? "true" : "false");
  });
  for (const view of ["overview", "channels", "models", "logs", "usage"]) {
    const panel = $(`#csview-${view}`);
    if (panel) panel.hidden = view !== name;
  }
  if (name === "overview") renderMcOverview();
  if (name === "channels") renderChannels();
  if (name === "models") renderMcModels();
  if (name === "logs" && !mc.logs.loaded) loadMcLogs();
  if (name === "usage") loadMcUsage();
}

async function mcRunTests(entries) {
  const list = (entries || []).filter((entry) => mcFindEntry(entry.provider.id, entry.model));
  if (!list.length) return;
  const targets = list.slice(0, MC_BATCH_LIMIT);
  if (list.length > MC_BATCH_LIMIT) showToast(`单次最多测试 ${MC_BATCH_LIMIT} 个模型，已测试前 ${MC_BATCH_LIMIT} 个`, true);
  text("#model-selection", `正在测试 ${targets.length} 个模型…`);
  try {
    const payload = await request("/api/models/actions/test", {
      method: "POST",
      body: JSON.stringify({ targets: targets.map((entry) => ({ provider_id: entry.provider.id, model: entry.model })) }),
      timeoutMs: 300000,
    });
    for (const result of payload.results || []) {
      mc.probes[mcProbeKey(result.provider_id, result.model)] = {
        provider_id: result.provider_id, model: result.model, ok: Boolean(result.ok),
        status: result.status, latency_ms: result.latency_ms,
        message: result.message, tested_at: new Date().toISOString(),
      };
    }
    renderChannels();
    renderMcModels();
    renderMcOverview();
    if (mc.drawerModel) renderMcDrawer();
    showToast(`测试完成：通过 ${payload.passed}，失败 ${payload.failed}`, payload.failed > 0);
  } catch (error) {
    showToast(error.message, true);
  } finally {
    updateMcSelection();
    loadMcUsage();
  }
}

async function mcActivate(providerId, model) {
  if (!mcFindEntry(providerId, model)) return;
  try {
    mc.registry = await request(`/api/models/${encodeURIComponent(providerId)}/activate`, {
      method: "POST", body: JSON.stringify({ model, revision: mc.registry?.revision }),
    });
    mc.routeProvider = providerId;
    mc.routeModels.set(providerId, model);
    renderModelRegistry();
    showToast(`已切换到 ${model}，音箱与 hub-active 的新请求立即生效`);
    await refreshAll(true);
  } catch (error) {
    showToast(error.message, true);
  }
}

async function mcChannelAction(providerId, action, button) {
  const provider = mcChannels().find((item) => item.id === providerId);
  if (!provider) return;
  if (action === "edit") return openProviderDialog(provider);
  if (action === "discover") {
    return withBusy(button, async () => {
      try {
        const payload = await request(`/api/models/${encodeURIComponent(providerId)}/discover`, { method: "POST", body: "{}", timeoutMs: 60000 });
        mc.registry = payload;
        renderModelRegistry();
        renderMcLogFilters();
        const info = payload.discovery || {};
        showToast(`已从上游获取 ${info.fetched || 0} 个模型，新增 ${info.added || 0} 个`);
      } catch (error) {
        showToast(error.message, true);
      }
    });
  }
  if (action === "test") {
    const entries = (provider.models || []).map((model) => ({ provider, model }));
    if (!entries.length) { showToast("这个渠道还没有模型", true); return; }
    return withBusy(button, () => mcRunTests(entries));
  }
  if (action === "toggle") {
    try {
      mc.registry = await request(`/api/models/${encodeURIComponent(providerId)}`, {
        method: "PUT",
        body: JSON.stringify({ enabled: !provider.enabled, revision: mc.registry?.revision }),
      });
      renderModelRegistry();
      renderMcLogFilters();
      showToast(provider.enabled ? `已停用「${provider.name}」` : `已启用「${provider.name}」`);
    } catch (error) {
      showToast(error.message, true);
    }
    return;
  }
  if (action === "remove") {
    const active = mc.registry?.active?.provider_id === providerId;
    const warning = active
      ? `「${provider.name}」正在被使用。删除后音箱与使用 hub-active 的客户端会暂时失去模型，确定删除吗？`
      : `确定删除渠道「${provider.name}」及其 ${mcModelCount(provider)} 个模型配置吗？此操作不可撤销。`;
    if (!window.confirm(warning)) return;
    try {
      mc.registry = await request(`/api/models/${encodeURIComponent(providerId)}`, {
        method: "DELETE", body: JSON.stringify({ deactivate: active, revision: mc.registry?.revision }),
      });
      for (const key of [...mc.selection]) if (mcSplitKey(key)[0] === providerId) mc.selection.delete(key);
      if (mc.drawerProvider === providerId) closeMcDrawer();
      if (mc.channel === providerId) mc.channel = "";
      renderModelRegistry();
      renderMcLogFilters();
      showToast("渠道已删除");
    } catch (error) {
      showToast(error.message, true);
    }
  }
}

function mcChannelRowHandler(event) {
  const button = event.target.closest("button[data-cs-act]");
  if (!button) return;
  mcChannelAction(button.dataset.csProvider, button.dataset.csAct, button);
}

document.querySelectorAll("[data-cs-view]").forEach((button) => {
  button.addEventListener("click", () => showConsoleView(button.dataset.csView));
});
document.querySelectorAll("[data-cs-goto]").forEach((button) => {
  button.addEventListener("click", () => showConsoleView(button.dataset.csGoto));
});

$("#channel-table-body").addEventListener("click", mcChannelRowHandler);
$("#overview-channel-body").addEventListener("click", mcChannelRowHandler);

$("#channel-search").addEventListener("input", (event) => {
  mc.channelQuery = event.target.value;
  renderChannels();
});
$("#channel-refresh").addEventListener("click", (event) => withBusy(event.currentTarget, async () => {
  await loadModelRegistry();
  showToast("渠道列表已刷新");
}));
$("#overview-test-all").addEventListener("click", (event) => {
  const entries = mcChannels().flatMap((provider) => (provider.models || []).map((model) => ({ provider, model })));
  if (!entries.length) { showToast("还没有可测试的模型", true); return; }
  if (!window.confirm(`将依次测试全部渠道的 ${Math.min(entries.length, MC_BATCH_LIMIT)} 个模型，可能需要一些时间。继续吗？`)) return;
  withBusy(event.currentTarget, () => mcRunTests(entries));
});

$("#model-channel").addEventListener("change", (event) => {
  mc.channel = event.target.value;
  mc.selection.clear();
  renderMcModels();
});
$("#model-search").addEventListener("input", (event) => {
  mc.modelQuery = event.target.value;
  renderMcModels();
});
$("#model-filter").addEventListener("change", (event) => {
  mc.modelFilter = event.target.value;
  renderMcModels();
});

$("#model-table-body").addEventListener("change", (event) => {
  const box = event.target.closest("input[data-mc-select]");
  if (!box) return;
  if (box.checked) mc.selection.add(box.dataset.mcSelect);
  else mc.selection.delete(box.dataset.mcSelect);
  renderMcModels();
});

$("#model-select-all").addEventListener("change", (event) => {
  const entries = mcVisibleEntries();
  for (const entry of entries) {
    const key = mcEntryKey(entry.provider.id, entry.model);
    if (event.target.checked) mc.selection.add(key);
    else mc.selection.delete(key);
  }
  renderMcModels();
});

$("#default-model-select").addEventListener("change", () => renderDefaultModelNote());
$("#default-model-apply").addEventListener("click", async (event) => {
  const entry = mcEntryFromKey($("#default-model-select").value);
  if (!entry) return;
  await withBusy(event.currentTarget, () => mcActivate(entry.provider.id, entry.model));
  renderDefaultModelNote();
});

$("#role-list").addEventListener("change", (event) => {
  const select = event.target.closest("[data-role-select]");
  if (select) renderRoleNote(select.dataset.roleSelect);
});
$("#role-list").addEventListener("click", async (event) => {
  const button = event.target.closest("[data-role-apply]");
  if (!button || button.disabled) return;
  const roleId = button.dataset.roleApply;
  const select = document.querySelector('[data-role-select="' + roleId + '"]');
  if (!select) return;
  const entry = mcEntryFromKey(select.value);
  await withBusy(button, async () => {
    try {
      mc.registry = await request("/api/models/actions/role", {
        method: "POST",
        body: JSON.stringify({
          role: roleId,
          provider_id: entry ? entry.provider.id : "",
          model: entry ? entry.model : "",
          revision: mc.registry?.revision,
        }),
      });
      renderModelRegistry();
      showToast(entry ? "已为该助手指定模型" : "已改为跟随默认模型");
    } catch (error) {
      const result = document.getElementById("role-result-" + roleId);
      if (result) { result.textContent = error.message; result.classList.add("is-error"); }
      else showToast(error.message, true);
    }
  });
});

$("#model-table-body").addEventListener("click", async (event) => {
  const jump = event.target.closest("button[data-mc-channel-jump]");
  if (jump) {
    mc.channel = jump.dataset.mcChannelJump;
    renderMcChannelFilter();
    renderMcModels();
    return;
  }
  const button = event.target.closest("button[data-mc-act]");
  if (!button) return;
  const providerId = button.dataset.mcProvider;
  const model = button.dataset.mcModel;
  const entry = mcFindEntry(providerId, model);
  if (!entry) return;
  const action = button.dataset.mcAct;
  if (action === "activate") return mcActivate(providerId, model);
  if (action === "fallback") return withBusy(button, () => mcToggleFallback(providerId, model));
  if (action === "settings") return openMcDrawer(providerId, model, button);
  if (action === "test") return withBusy(button, () => mcRunTests([entry]));
  if (action === "remove") {
    if (!window.confirm(`确定从「${entry.provider.name}」删除模型「${model}」吗？此操作不可撤销。`)) return;
    try {
      mc.registry = await request(`/api/models/${encodeURIComponent(providerId)}/models/remove`, {
        method: "POST", body: JSON.stringify({ models: [model], revision: mc.registry?.revision }),
      });
      mc.selection.delete(mcEntryKey(providerId, model));
      if (mc.drawerProvider === providerId && mc.drawerModel === model) closeMcDrawer();
      renderModelRegistry();
      showToast("模型已删除");
    } catch (error) {
      showToast(error.message, true);
    }
  }
});

$("#model-test-selected").addEventListener("click", (event) => withBusy(event.currentTarget, () => mcRunTests(mcSelectedEntries())));
$("#model-test-all").addEventListener("click", (event) => {
  const entries = mcVisibleEntries();
  if (!entries.length) { showToast("当前没有可测试的模型", true); return; }
  if (!window.confirm(`将依次测试当前筛选出的 ${Math.min(entries.length, MC_BATCH_LIMIT)} 个模型，可能需要一些时间。继续吗？`)) return;
  withBusy(event.currentTarget, () => mcRunTests(entries));
});
$("#model-remove-selected").addEventListener("click", async () => {
  const entries = mcSelectedEntries();
  if (!entries.length) return;
  const channels = [...new Set(entries.map((entry) => entry.provider.name))];
  if (!window.confirm(`确定删除选中的 ${entries.length} 个模型吗（来自 ${channels.join("、")}）？此操作不可撤销。`)) return;
  const grouped = new Map();
  for (const entry of entries) {
    if (!grouped.has(entry.provider.id)) grouped.set(entry.provider.id, []);
    grouped.get(entry.provider.id).push(entry.model);
  }
  const failures = [];
  for (const [providerId, models] of grouped) {
    try {
      mc.registry = await request(`/api/models/${encodeURIComponent(providerId)}/models/remove`, {
        method: "POST", body: JSON.stringify({ models, revision: mc.registry?.revision }),
      });
      for (const model of models) mc.selection.delete(mcEntryKey(providerId, model));
    } catch (error) {
      failures.push(`${providerId}：${error.message}`);
    }
  }
  if (mc.drawerModel && !mcFindEntry(mc.drawerProvider, mc.drawerModel)) closeMcDrawer();
  renderModelRegistry();
  if (failures.length) showToast(failures[0], true);
  else showToast(`已删除 ${entries.length} 个模型`);
});

$("#drawer-close").addEventListener("click", closeMcDrawer);
$("#console-drawer").addEventListener("cancel", (event) => { event.preventDefault(); closeMcDrawer(); });
$("#drawer-test").addEventListener("click", (event) => {
  const entry = mcDrawerEntry();
  if (entry) withBusy(event.currentTarget, () => mcRunTests([entry]));
});
$("#model-settings-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const entry = mcDrawerEntry();
  if (!entry) return;
  try {
    mc.registry = await request(`/api/models/${encodeURIComponent(entry.provider.id)}/settings`, {
      method: "PUT",
      body: JSON.stringify({
        model: entry.model,
        revision: mc.registry?.revision,
        reasoning_level: $("#model-settings-reasoning").value,
        context_window: Number($("#model-settings-context").value),
        max_output_tokens: Number($("#model-settings-output").value),
        temperature: Number($("#model-settings-temperature").value),
        multimodal: $("#model-settings-multimodal").checked,
      }),
    });
    renderModelRegistry();
    $("#model-settings-result").textContent = "设置已保存";
    showToast("模型设置已保存");
  } catch (error) {
    $("#model-settings-result").textContent = error.message;
  }
});

/* --------------------------------------------------------- 提供方对话框 */

function openProviderDialog(provider) {
  const dialog = $("#provider-dialog");
  $("#model-provider-id").value = provider?.id || "";
  $("#model-provider-name").value = provider?.name || "";
  $("#model-provider-protocol").value = provider?.protocol || "openai";
  $("#model-provider-url").value = provider?.base_url || "";
  $("#model-provider-key").value = "";
  const proxyInput = $("#model-provider-proxy");
  // 代理只回显 scheme://host:port；库里那份（可能带凭据）不会被读出来，
  // 所以原样回传脱敏值时后端会当作「没改」，不会把凭据洗掉。
  proxyInput.value = provider?.proxy_display || "";
  proxyInput.placeholder = provider?.proxy_set
    ? `已设置：${provider.proxy_display}${provider.proxy_auth_set ? "（含凭据）" : ""}；清空 = 改回直连`
    : "例如 http://host.docker.internal:17892（留空 = 直连）";
  $("#model-provider-models").value = (provider?.models || []).join("\n");
  $("#model-provider-enabled").checked = provider ? Boolean(provider.enabled) : true;
  const editing = Boolean(provider);
  text("#provider-dialog-title", editing ? "编辑提供方" : "新增提供方");
  text("#model-provider-hint", editing
    ? "模型列表将整体替换；如需保留，请确认列表完整。API Key 留空表示保持原值；代理留空表示改回直连。"
    : "保存后可用「自动获取模型」读取上游 /models 列表。只有填了代理的提供方才会走代理，其余一律直连。");
  $("#model-provider-result").textContent = "";
  dialog.showModal();
}

function closeProviderDialog() {
  $("#provider-dialog").close();
}

$("#provider-new").addEventListener("click", () => openProviderDialog(null));
$("#provider-dialog-close").addEventListener("click", closeProviderDialog);
$("#provider-dialog-cancel").addEventListener("click", closeProviderDialog);
$("#model-provider-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const id = $("#model-provider-id").value.trim();
  const existing = mcChannels().find((provider) => provider.id === id);
  if (existing) {
    const incoming = mcParseModels($("#model-provider-models").value);
    const dropped = (existing.models || []).filter((model) => !incoming.includes(model));
    if (dropped.length && !window.confirm(`保存后将从该渠道移除 ${dropped.length} 个模型（${dropped.slice(0, 5).join("、")}${dropped.length > 5 ? "…" : ""}）。继续吗？`)) return;
  }
  const body = {
    id: id || undefined,
    name: $("#model-provider-name").value.trim(),
    protocol: $("#model-provider-protocol").value,
    base_url: $("#model-provider-url").value.trim(),
    models: mcParseModels($("#model-provider-models").value),
    api_key: $("#model-provider-key").value,
    proxy: $("#model-provider-proxy").value.trim(),
    enabled: $("#model-provider-enabled").checked,
    revision: mc.registry?.revision,
  };
  const result = $("#model-provider-result");
  const save = $("#model-provider-save");
  save.disabled = true;
  try {
    mc.registry = await request(id ? `/api/models/${encodeURIComponent(id)}` : "/api/models", {
      method: "PUT", body: JSON.stringify(body),
    });
    mc.selection.clear();
    renderModelRegistry();
    renderMcLogFilters();
    closeProviderDialog();
    showToast("渠道已保存");
  } catch (error) {
    result.textContent = error.message;
  } finally {
    save.disabled = false;
  }
});

/* --------------------------------------------------------- 添加模型对话框 */

function openModelAddDialog() {
  const select = $("#model-add-channel");
  const providers = mcChannels();
  if (!providers.length) {
    showToast("先添加一个渠道，再往里加模型", true);
    return;
  }
  select.replaceChildren();
  for (const provider of providers) select.append(new Option(`${provider.name}（${mcModelCount(provider)}）`, provider.id));
  select.value = providers.some((provider) => provider.id === mc.channel) ? mc.channel : providers[0].id;
  const describe = () => {
    const provider = providers.find((item) => item.id === select.value);
    text("#model-add-target", provider ? `加入「${provider.name}」；已存在的模型会被自动跳过。` : "—");
  };
  describe();
  select.onchange = describe;
  $("#model-add-ids").value = "";
  $("#model-add-result").textContent = "";
  const dialog = $("#model-add-dialog");
  if (!dialog.open) dialog.showModal();
}

$("#model-add").addEventListener("click", openModelAddDialog);
$("#model-add-close").addEventListener("click", () => $("#model-add-dialog").close());
$("#model-add-cancel").addEventListener("click", () => $("#model-add-dialog").close());
$("#model-add-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const providerId = $("#model-add-channel").value;
  const models = mcParseModels($("#model-add-ids").value);
  if (!providerId || !models.length) {
    $("#model-add-result").textContent = "请至少填写一个模型 ID。";
    return;
  }
  try {
    mc.registry = await request(`/api/models/${encodeURIComponent(providerId)}/models/add`, {
      method: "POST", body: JSON.stringify({ models, revision: mc.registry?.revision }),
    });
    mc.channel = providerId;
    renderModelRegistry();
    renderMcLogFilters();
    $("#model-add-dialog").close();
    showToast(`已加入 ${models.length} 个模型`);
  } catch (error) {
    $("#model-add-result").textContent = error.message;
  }
});

/* ------------------------------------------------------------ 日志与统计 */

$("#log-filter-provider").addEventListener("change", (event) => {
  mc.logFilter.provider = event.target.value;
  mc.logs.offset = 0;
  loadMcLogs();
});
$("#log-filter-model").addEventListener("change", (event) => {
  mc.logFilter.model = event.target.value;
  mc.logs.offset = 0;
  loadMcLogs();
});
$("#log-filter-errors").addEventListener("change", (event) => {
  mc.logFilter.errors = event.target.checked;
  mc.logs.offset = 0;
  loadMcLogs();
});
$("#log-refresh").addEventListener("click", (event) => withBusy(event.currentTarget, loadMcLogs));
$("#log-prev").addEventListener("click", () => {
  mc.logs.offset = Math.max(0, mc.logs.offset - mc.logs.limit);
  loadMcLogs();
});
$("#log-next").addEventListener("click", () => {
  if (mc.logs.offset + mc.logs.limit >= mc.logs.total) return;
  mc.logs.offset += mc.logs.limit;
  loadMcLogs();
});
$("#log-clear").addEventListener("click", async () => {
  if (!window.confirm("确定清空全部网关请求日志吗？用量统计也会一起归零，此操作不可撤销。")) return;
  try {
    const payload = await request("/api/models/actions/logs", { method: "DELETE" });
    mc.logs.offset = 0;
    await Promise.all([loadMcLogs(), loadMcUsage()]);
    showToast(`已清空 ${payload.removed || 0} 条日志`);
  } catch (error) {
    showToast(error.message, true);
  }
});
$("#usage-hours").addEventListener("change", (event) => {
  mc.usageHours = Number(event.target.value) || 24;
  loadMcUsage();
});
$("#usage-refresh").addEventListener("click", (event) => withBusy(event.currentTarget, loadMcUsage));
$("#console-refresh").addEventListener("click", (event) => withBusy(event.currentTarget, async () => {
  await loadModelRegistry();
  await Promise.all([loadMcLogs(), loadMcUsage()]);
  showToast("配置已刷新");
}));

/* Route selection stays local until the activation button is pressed. */
function mcRouteModel(provider) {
  const selected = mc.routeModels.get(provider.id);
  if (provider.models?.includes(selected)) return selected;
  const active = mc.registry?.active || {};
  return active.provider_id === provider.id && provider.models?.includes(active.model) ? active.model : provider.models?.[0] || "";
}

function renderMcRoutes() {
  const providers = mc.registry?.providers || [];
  const active = mc.registry?.active || {};
  if (!providers.some((provider) => provider.id === mc.routeProvider)) {
    mc.routeProvider = providers.find((provider) => provider.id === active.provider_id)?.id || providers[0]?.id || "";
  }
  const host = $("#route-configs");
  host.replaceChildren();
  providers.forEach((provider, index) => {
    const card = homeNode("article", "route-config");
    card.dataset.routeProvider = provider.id;
    const label = homeNode("label", "route-config-label");
    const input = document.createElement("input");
    input.type = "radio"; input.name = "route-provider"; input.value = provider.id;
    input.checked = provider.id === mc.routeProvider;
    const title = homeNode("span", "route-config-name", provider.name);
    label.append(input, title);
    const status = homeNode("span", "route-config-status");
    status.append(mcBadge(active.provider_id === provider.id ? "当前路由" : provider.enabled ? "可切换" : "已停用", active.provider_id === provider.id ? "tone-accent" : "tone-idle"));
    const head = homeNode("div", "route-config-head"); head.append(label, status);
    const address = homeNode("p", "route-config-address", provider.base_url || "未填写地址");
    address.title = provider.base_url || "";
    const modelLabel = homeNode("label", "route-model-label", "目标模型");
    modelLabel.htmlFor = `route-model-${index}`;
    const select = document.createElement("select");
    select.className = "console-input"; select.id = modelLabel.htmlFor; select.dataset.routeModel = provider.id;
    for (const model of provider.models || []) select.append(new Option(model, model));
    if (!select.options.length) select.append(new Option("请先导入模型", ""));
    select.value = mcRouteModel(provider); select.disabled = !provider.enabled || !provider.models?.length;
    const foot = homeNode("div", "route-config-foot");
    foot.append(homeNode("span", "", `${provider.protocol === "anthropic" ? "Anthropic" : "OpenAI"} · ${mcModelCount(provider)} 个模型`));
    const edit = homeNode("button", "route-config-edit", "编辑配置");
    edit.type = "button"; edit.dataset.routeEdit = provider.id; edit.setAttribute("aria-label", `编辑配置 ${provider.name}`);
    foot.append(edit); card.append(head, address, modelLabel, select, foot);host.append(card);
  });
  $("#route-config-empty").hidden = providers.length > 0;
  renderMcRouteSelection();
}

function renderMcRouteSelection() {
  const provider = (mc.registry?.providers || []).find((item) => item.id === mc.routeProvider);
  const model = provider ? mcRouteModel(provider) : "";
  const active = mc.registry?.active || {};
  const same = Boolean(provider && active.provider_id === provider.id && active.model === model);
  document.querySelectorAll("[data-route-provider]").forEach((card) => {
    const selected = card.dataset.routeProvider === provider?.id;
    card.classList.toggle("selected", selected);
    card.querySelector("input[type=radio]").checked = selected;
    const config = (mc.registry?.providers || []).find((item) => item.id === card.dataset.routeProvider);
    const isActive = config && config.id === active.provider_id && mcRouteModel(config) === active.model;
    card.querySelector(".route-config-status").replaceChildren(mcBadge(isActive ? "当前路由" : config?.enabled ? "可切换" : "已停用", isActive ? "tone-accent" : "tone-idle"));
  });
  text("#route-preview-provider", provider?.name || "选择一个配置");
  text("#route-preview-model", model || "尚无模型");
  text("#route-preview-label", same ? "当前生效" : "待启用路由");
  text("#route-preview-note", !provider?.enabled ? "请先在编辑配置中启用该提供方。" : !model ? "请先导入至少一个模型。" : "音箱与使用 hub-active 的客户端会跟随切换。");
  const button = $("#route-activate");
  button.disabled = mc.routeBusy || same || !provider?.enabled || !model;
  button.textContent = mc.routeBusy ? "正在切换…" : same ? "✓ 当前已启用" : "启用此路由 →";
}

$("#route-configs").addEventListener("change", (event) => {
  const target = event.target;
  if (target.matches("input[name=route-provider]")) mc.routeProvider = target.value;
  else if (target.dataset.routeModel) {
    mc.routeProvider = target.dataset.routeModel;
    mc.routeModels.set(mc.routeProvider, target.value);
  } else return;
  text("#route-result", "");
  renderMcRouteSelection();
});
$("#route-configs").addEventListener("click", (event) => {
  const button = event.target.closest("[data-route-edit]");
  if (!button) return;
  const provider = (mc.registry?.providers || []).find((item) => item.id === button.dataset.routeEdit);
  if (provider) openProviderDialog(provider);
});
$("#route-config-new").addEventListener("click", () => openProviderDialog(null));
$("#route-activate").addEventListener("click", async () => {
  const provider = (mc.registry?.providers || []).find((item) => item.id === mc.routeProvider);
  const model = provider ? mcRouteModel(provider) : "";
  if (mc.routeBusy || !provider?.enabled || !model) return;
  mc.routeBusy = true; text("#route-result", "正在启用所选配置…");
  $("#route-result").classList.remove("is-error");
  renderMcRouteSelection();
  try {
    mc.registry = await request(`/api/models/${encodeURIComponent(provider.id)}/activate`, {
      method: "POST", body: JSON.stringify({ model, revision: mc.registry?.revision }),
    });
    mc.routeProvider = provider.id; mc.routeModels.set(provider.id, model);
    renderModelRegistry();
    text("#route-result", `已启用 ${provider.name} → ${model}。新请求立即生效。`);
    $("#route-result").classList.remove("is-error");
    showToast("路由已切换");
    refreshAll(true);
  } catch (error) {
    // A timeout can happen after the server has committed the route. Read it
    // back before claiming which configuration is currently in effect.
    let reconciled = false;
    try {
      mc.registry = await request("/api/models");
      renderModelRegistry();
      reconciled = true;
    } catch (refreshError) { /* Keep the last known state with an explicit notice. */ }
    const active = mc.registry?.active || {};
    const applied = reconciled && active.provider_id === provider.id && active.model === model;
    text("#route-result", applied ? "所选路由已保存，但响应未正常完成；请检查客户端连接。" : reconciled ? `切换失败：${error.message}` : `无法确认切换结果：${error.message}。请刷新配置后查看当前路由。`);
    $("#route-result").classList.add("is-error");
    showToast(error.message, true);
  } finally {
    mc.routeBusy = false; renderMcRouteSelection();
  }
});

/* Client connection guide: read-only configuration, no automatic requests. */
function renderMcConnection() {
  const protocol = mc.connectProtocol;
  const model = $("#connect-model-select").value;
  const authenticated = gatewayAuthRequired();
  const base = protocol === "anthropic" ? location.origin : `${location.origin}/v1`;
  const path = protocol === "anthropic" ? "/v1/messages" : protocol === "responses" ? "/v1/responses" : "/v1/chat/completions";
  const descriptions = {
    openai: "用于支持 OpenAI Chat Completions 的聊天应用和 SDK。",
    anthropic: "用于 Claude Code 或 Anthropic SDK，也可以调用已接入的 GPT 模型。",
    responses: "用于使用 Responses 接口的客户端与 OpenAI SDK。",
  };
  text("#connect-description", descriptions[protocol]);
  text("#connect-base", base);
  text("#connect-key", authenticated ? "填写网关令牌" : "local");
  text("#connect-key-note", authenticated ? "从 NAS 的 ai-gateway-token 文件读取，与上游密钥不同。" : "当前未启用入口密钥校验，可填任意非空值。");
  text("#connect-model-id", model || "—");
  const body = protocol === "responses"
    ? { model, input: "你好", max_output_tokens: 256, stream: true }
    : { model, max_tokens: 256, messages: [{ role: "user", content: "你好" }], stream: true };
  const quote = (value) => "'" + value.replace(/'/g, "'\"'\"'") + "'";
  const key = authenticated ? "<网关令牌>" : "local";
  const auth = protocol === "anthropic" ? `x-api-key: ${key}` : `Authorization: Bearer ${key}`;
  const lines = [`curl -N ${quote(location.origin + path)}`, `  -H ${quote("Content-Type: application/json")}`, `  -H ${quote(auth)}`];
  if (protocol === "anthropic") lines.push("  -H 'anthropic-version: 2023-06-01'");
  lines.push(`  -d ${quote(JSON.stringify(body))}`);
  text("#connect-code", model ? lines.join(" \\\n") : "请先添加并启用至少一个模型。");
  $("#connect-panel").setAttribute("aria-labelledby", `connect-tab-${protocol}`);
  document.querySelectorAll("[data-connect-protocol]").forEach((button) => {
    const selected = button.dataset.connectProtocol === protocol;
    button.setAttribute("aria-selected", String(selected));
    button.tabIndex = selected ? 0 : -1;
  });
}

function openConnectDialog() {
  const select = $("#connect-model-select");
  const prior = select.value;
  const providers = (mc.registry?.providers || []).filter((provider) => provider.enabled);
  const active = mc.registry?.active || {};
  select.replaceChildren();
  if (providers.some((provider) => provider.id === active.provider_id && provider.models?.includes(active.model))) {
    select.append(new Option("跟随面板默认模型 · hub-active", "hub-active"));
  }
  const added = new Set(["hub-active"]);
  for (const provider of providers) {
    const group = document.createElement("optgroup");
    group.label = provider.name;
    for (const model of provider.models || []) {
      if (added.has(model)) continue;
      group.append(new Option(model, model));
      added.add(model);
    }
    if (group.children.length) select.append(group);
  }
  if ([...select.options].some((option) => option.value === prior)) select.value = prior;
  select.disabled = !select.options.length;
  renderMcConnection();
  const dialog = $("#gateway-connect-dialog");
  if (!dialog.open) dialog.showModal();
}
$("#gateway-connect").addEventListener("click", openConnectDialog);
$("#gateway-connect-inline").addEventListener("click", openConnectDialog);
$("#connect-close").addEventListener("click", () => $("#gateway-connect-dialog").close());
$("#connect-model-select").addEventListener("change", renderMcConnection);
document.querySelectorAll("[data-connect-protocol]").forEach((button) => {
  button.addEventListener("click", () => { mc.connectProtocol = button.dataset.connectProtocol; renderMcConnection(); });
});
for (const selector of [".connect-tabs", ".console-nav"]) {
  const tablist = $(selector);
  if (!tablist) continue;
  tablist.addEventListener("keydown", (event) => {
    const tabs = [...event.currentTarget.querySelectorAll('[role="tab"]')];
    const index = tabs.indexOf(event.target);
    if (index < 0 || !["ArrowRight", "ArrowLeft", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const next = event.key === "Home" ? 0 : event.key === "End" ? tabs.length - 1 : (index + (event.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length;
    tabs[next].focus(); tabs[next].click();
  });
}
$("#gateway-locate-active").addEventListener("click", () => {
  const active = mc.registry?.active;
  if (!active?.provider_id) return;
  mc.channel = active.provider_id;
  showConsoleView("channels");
  renderMcChannelFilter();
  const body = $("#channel-table-body");
  if (body) {
    body.scrollIntoView({ behavior: "auto", block: "center" });
    const row = [...body.querySelectorAll("tr")].find((item) => item.classList.contains("is-active"));
    if (row) row.querySelector("button")?.focus({ preventScroll: true });
  }
});

document.querySelectorAll("[data-copy]").forEach((button) => {
  button.addEventListener("click", async () => {
    const node = document.getElementById(button.dataset.copy);
    const value = node?.textContent?.trim();
    if (!value || value === "—") return;
    let copied = false;
    try {
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(value);
        copied = true;
      }
    } catch (error) {
      copied = false;
    }
    if (!copied) {
      const scratch = document.createElement("textarea");
      scratch.value = value;
      scratch.setAttribute("readonly", "");
      scratch.className = "clipboard-scratch";
      const priorFocus = document.activeElement;
      (button.closest("dialog[open]") || document.body).append(scratch);
      scratch.select();
      try { copied = document.execCommand("copy"); } catch (error) { copied = false; }
      scratch.remove();
      priorFocus?.focus();
    }
    showToast(copied ? "已复制到剪贴板" : "复制失败，请手动选择文本", !copied);
  });
});

$("#test-tts").addEventListener("click", async (event) => {
  const output = $("#tts-test-output");
  output.textContent = "正在发送到音箱…";
  await withBusy(event.currentTarget, async () => {
    await request("/api/actions/test-tts", {
      method: "POST",
      body: JSON.stringify({ text: $("#tts-text").value }),
    });
    output.textContent = "播放指令已发送";
    showToast("TTS 指令已发送");
  }).catch((error) => { output.textContent = error.message; showToast(error.message, true); });
});

$("#hub-filter-kind").addEventListener("change", (event) => {
  hubFilterKind = event.currentTarget.value;
  if (currentHub) renderHub(currentHub);
});
$("#hub-filter-room").addEventListener("change", (event) => {
  hubFilterRoom = event.target.value;
  if (currentHub) renderHub(currentHub);
});
$("#hub-filter-purpose").addEventListener("change", (event) => {
  hubFilterPurpose = event.target.value;
  if (currentHub) renderHub(currentHub);
});
$("#hub-filter-search").addEventListener("input", (event) => {
  hubFilterSearch = event.currentTarget.value;
  if (currentHub) renderHub(currentHub);
});
$("#hub-detail-close").addEventListener("click", () => {
  currentHubSelection = "";
  hubHistoryRequest += 1;
  hubHistoryLoading = false;
  hubHistoryKey = "";
  hubHistoryNotice = "";
  renderHubDetail(null);
});

$("#hub-history-metric").addEventListener("change", () => {
  hubHistoryNotice = "";
  renderHubHistory();
});
$("#hub-history-more").addEventListener("click", () => {
  const resource = currentHub?.resources?.find((item) => item.resource_id === currentHubSelection);
  if (resource && !hubHistoryLoading) loadHubHistory(resource, true, true);
});
$("#hub-storage-refresh").addEventListener("click", () => loadHubStorage(true));
$("#hub-data-management").addEventListener("toggle", (event) => {
  if (event.currentTarget.open) loadHubStorage();
});
$("#hub-history-hours").addEventListener("change", (event) => {
  hubHistoryHours = Number(event.currentTarget.value);
  const resource = currentHub?.resources?.find((item) => item.resource_id === currentHubSelection);
  if (resource) {
    hubHistory = [];
    loadHubHistory(resource, true);
  }
});
$("#hub-sample-refresh").addEventListener("click", async (event) => {
  if (hubSamplePromise) return;
  hubSamplePromise = withBusy(event.currentTarget, async () => {
    const response = await request("/api/hub/refresh", { method: "POST", body: "{}", timeoutMs: 45000 });
    if (response.hub) renderHub(response.hub);
    showToast(response.hub?.sampling?.last_error ? "采样暂未成功，保留最近的有效读数" : "设备数据已更新");
  }).catch((error) => showToast(error.message, true)).finally(() => { hubSamplePromise = null; });
  await hubSamplePromise;
});

window.addEventListener("hashchange", () => {
  const name = location.hash.replace("#", "");
  if (["overview", "connection", "services", "home", "data", "health", "chat", "preferences"].includes(name)) {
    showView(name, { updateHash: false });
  }
});

document.addEventListener("visibilitychange", () => {
  if (document.hidden) {
    clearTimeout(hubStreamRetryTimer);
    hubStream?.close();
    hubStream = null;
    hubStreamConnected = false;
  } else {
    startHubStream();
    refreshAll(true);
  }
});

const initialView = location.hash.replace("#", "");
if (panelCsrfToken) {
  $("#panel-account").hidden = false;
  text("#panel-account-name", document.querySelector('meta[name="panel-user"]')?.content || "管理员");
}
$("#panel-logout")?.addEventListener("click", async (event) => {
  await withBusy(event.currentTarget, async () => {
    await request("/api/auth/logout", { method: "POST", body: "{}" });
    requirePanelLogin();
  }).catch((error) => showToast(error.message, true));
});
window.addEventListener("pageshow", (event) => {
  if (event.persisted && panelCsrfToken) {
    request("/api/auth/session").then((session) => { if (!session.authenticated) requirePanelLogin(); }).catch(() => {});
  }
});
showView(["overview", "connection", "services", "home", "data", "health", "chat", "preferences"].includes(initialView) ? initialView : "overview");
refreshAll();
pollXiaomiLogin();
startHubStream();
setInterval(() => {
  if (!document.hidden) refreshAll(true);
}, 3500);
