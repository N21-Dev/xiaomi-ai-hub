// DOM-only regression checks for the model console. No network, no uploads.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { JSDOM, VirtualConsole } = require(process.env.HUB_JSDOM_MODULE || 'jsdom');

const root = path.resolve(__dirname, '../src/xiaomi_ai_bridge/web');
const html = fs.readFileSync(path.join(root, 'index.html'), 'utf8');
const script = fs.readFileSync(path.join(root, 'app.js'), 'utf8');

const now = Date.now();
const at = (minutes) => new Date(now - minutes * 60000).toISOString();

const registry = {
  revision: 7,
  providers: [
    {
      id: 'env-default', name: '当前环境配置', protocol: 'openai', base_url: 'https://api.example.com/v1',
      enabled: true, last_discovery_at: null, api_key_set: true,
      models: ['alpha', 'vision-model', 'beta'],
      model_settings: [
        { model: 'alpha', reasoning_level: 'auto', context_window: 128000, max_output_tokens: 4096, temperature: 0.3, multimodal: false },
        { model: 'vision-model', reasoning_level: 'high', context_window: 200000, max_output_tokens: 16000, temperature: 0.2, multimodal: true },
        { model: 'beta', reasoning_level: 'off', context_window: 32000, max_output_tokens: 2048, temperature: 0.7, multimodal: false },
      ],
    },
    {
      id: 'anthropic-main', name: 'Anthropic 主账号', protocol: 'anthropic', base_url: 'https://api.anthropic.com/v1',
      enabled: false, last_discovery_at: null, api_key_set: false,
      models: ['claude-sonnet'],
      model_settings: [
        { model: 'claude-sonnet', reasoning_level: 'medium', context_window: 200000, max_output_tokens: 8192, temperature: 0.5, multimodal: true },
      ],
    },
  ],
  active: { provider_id: 'env-default', model: 'alpha' },
  // Roles start out following the default model; the panel lets each
  // assistant be pointed at a model of its own.
  roles: {
    speaker: { provider_id: '', model: '' },
  },
  role_catalog: [
    { id: 'speaker', label: '小爱音箱', alias: 'speaker-active' },
  ],
  gateway: {
    // Mirrors the live keyless deployment.  Both fields must agree:
    // `token_required` is the legacy name, `auth_required` is authoritative.
    token_required: false, auth_required: false,
    active_alias: 'hub-active', openai_path: '/v1/chat/completions',
    responses_path: '/v1/responses', anthropic_path: '/v1/messages', compatibility_note: '',
  },
};

const probes = {
  probes: {
    'env-default\u0000alpha': { provider_id: 'env-default', model: 'alpha', tested_at: at(3), ok: true, status: 200, latency_ms: 88, message: '测试通过' },
    'anthropic-main\u0000claude-sonnet': { provider_id: 'anthropic-main', model: 'claude-sonnet', tested_at: at(9), ok: false, status: 503, latency_ms: 12, message: '上游 HTTP 503' },
  },
};

const series = Array.from({ length: 24 }, (_, index) => ({
  at: new Date(now - (23 - index) * 3600000).toISOString(),
  total: index === 23 ? 4 : 1,
  failed: index === 23 ? 1 : 0,
}));

const usage = {
  hours: 24, total: 12, ok: 11, failed: 1, success_rate: 91.7,
  avg_latency_ms: 320, p95_latency_ms: 900, tokens: 4096, series,
  models: [
    { key: 'alpha', label: 'alpha', requests: 8, failed: 0, avg_latency_ms: 300, tokens: 3000 },
    { key: 'beta', label: 'beta', requests: 4, failed: 1, avg_latency_ms: 360, tokens: 1096 },
  ],
  providers: [{ key: 'env-default', label: '当前环境配置', requests: 12, failed: 1, avg_latency_ms: 320, tokens: 4096 }],
  errors: [{ message: '上游 HTTP 429', count: 1 }],
};

const logs = {
  total: 2, limit: 50, offset: 0,
  items: [
    { id: 2, at: at(1), provider_id: 'env-default', provider_name: '当前环境配置', model: 'nvidia/nemotron', protocol: 'openai', surface: 'openai', status: 429, ok: false, latency_ms: 1200, prompt_tokens: 0, completion_tokens: 0, total_tokens: 0, error: '上游 HTTP 429' },
    { id: 1, at: at(9), provider_id: 'env-default', provider_name: '当前环境配置', model: 'alpha', protocol: 'openai', surface: 'responses', status: 200, ok: true, latency_ms: 240, prompt_tokens: 20, completion_tokens: 40, total_tokens: 60, error: '' },
  ],
};

const settle = (ms = 60) => new Promise((resolve) => setTimeout(resolve, ms));

/**
 * Boot the panel against a fixture. `gatewayOverride` lets the auth-label
 * scenarios below swap only the gateway document.
 */
function boot(gatewayOverride) {
  const errors = [];
  const virtualConsole = new VirtualConsole();
  virtualConsole.on('jsdomError', (error) => errors.push(error));

  const dom = new JSDOM(html, {
    url: 'http://hub.test/#services', runScripts: 'outside-only', pretendToBeVisual: true, virtualConsole,
  });
  const w = dom.window;
  const q = (selector) => w.document.querySelector(selector);
  const qa = (selector) => [...w.document.querySelectorAll(selector)];
  const payload = gatewayOverride ? { ...structuredClone(registry), gateway: gatewayOverride } : registry;

  w.EventSource = class { constructor() {} close() {} };
  w.HTMLDialogElement.prototype.showModal = function showModal() { this.open = true; };
  w.HTMLDialogElement.prototype.close = function close() { this.open = false; };
  w.confirm = () => true;

  w.fetch = async (target, options) => {
    const url = String(target);
    const method = String(options?.method || 'GET').toUpperCase();
    if (method === 'POST' && url === '/api/models/actions/role') {
      const body = JSON.parse(String(options.body || '{}'));
      if (body.provider_id && body.model) payload.roles[body.role] = { provider_id: body.provider_id, model: body.model };
      else delete payload.roles[body.role];
      payload.revision += 1;
      return { ok: true, status: 200, json: async () => structuredClone(payload) };
    }
    let data = {};
    if (url === '/api/models') data = payload;
    else if (url === '/api/models/actions/probes') data = probes;
    else if (url.startsWith('/api/models/actions/usage')) data = usage;
    else if (url.startsWith('/api/models/actions/logs')) data = logs;
    else if (url === '/api/status') data = { started_at: at(5), ready: true };
    else if (url === '/api/config') data = { ai_model: 'alpha', ai_base_url: 'http://127.0.0.1:18765/v1' };
    else if (url === '/api/home') data = { devices: [], audit: [] };
    else if (url === '/api/hub') data = { resources: [], events: [], sampling: { interval_seconds: 60, last_sample_at: at(0) } };
    else if (url.includes('/auth/')) data = { state: 'idle' };
    return { ok: true, status: 200, json: async () => structuredClone(data) };
  };
  return { dom, w, q, qa, errors };
}

async function run() {
  const { w, q, qa, errors } = boot();

  try {
    assert.doesNotMatch(html, /style="/, 'CSP 禁止内联 style 属性');
    assert.doesNotMatch(html, /<script(?![^>]*\ssrc=)/, 'CSP 禁止内联脚本');

    const known = new Set([...html.matchAll(/id="([^"]+)"/g)].map((match) => match[1]));
    const referenced = new Set([...script.matchAll(/\$\("#([A-Za-z0-9_-]+)"/g)].map((match) => match[1]));
    const missing = [...referenced].filter((id) => !known.has(id));
    assert.deepEqual(missing, [], `app.js 引用了不存在的元素：${missing.join('、')}`);

    w.eval(script);
    await settle(120);

    // ── 子导航：默认停在概览，其余分区隐藏 ───────────────────────────────
    assert.equal(qa('[data-cs-view]').length, 5, '应有 5 个分区导航');
    assert.equal(q('#csnav-overview').getAttribute('aria-selected'), 'true');
    assert.equal(q('#csnav-channel-count').textContent, '2');
    assert.equal(q('#csnav-model-count').textContent, '4');
    assert.equal(q('#csview-overview').hidden, false);
    for (const view of ['channels', 'models', 'logs', 'usage']) {
      assert.equal(q(`#csview-${view}`).hidden, true, `${view} 分区应默认隐藏`);
    }

    // ── 概览：指标卡、统一入口、当前路由、渠道健康、切换板、趋势图 ──────
    assert.equal(q('#console-provider-count').textContent, '2');
    assert.equal(q('#console-model-count').textContent, '4');
    assert.equal(q('#console-request-count').textContent, '12');
    assert.match(q('#console-request-note').textContent, /成功 11 · 失败 1/);
    assert.equal(q('#overview-success').textContent, '91.7%');
    assert.match(q('#overview-success-note').textContent, /平均 320ms/);

    assert.equal(q('#gateway-openai-base').textContent, 'http://hub.test/v1');
    assert.equal(q('#gateway-anthropic-base').textContent, 'http://hub.test');
    assert.equal(q('#cs-endpoint-key').textContent, 'local（免密，任意非空值均可）');
    assert.match(q('#gateway-token-state').textContent, /免密访问/);
    assert.equal(q('#model-gateway-status').textContent, '网关就绪');

    assert.match(q('#gateway-active-model').textContent, /alpha/);
    assert.match(q('#gateway-active-provider').textContent, /当前环境配置/);

    const overviewRows = qa('#overview-channel-body tr');
    assert.equal(overviewRows.length, 2);
    assert.match(overviewRows[0].textContent, /使用中/);
    assert.match(overviewRows[1].textContent, /已停用/);
    assert.equal(qa('#overview-channel-body .cs-dot').length, 2);

    assert.equal(qa('#route-configs .route-config').length, 2);
    assert.equal(q('#route-activate').disabled, true);
    assert.match(q('#route-activate').textContent, /当前已启用/);
    assert.ok(q('#overview-chart svg'), '概览应渲染请求趋势 SVG');

    // ── 上游渠道分区 ────────────────────────────────────────────────────
    q('#csnav-channels').click();
    await settle();
    assert.equal(q('#csview-channels').hidden, false);
    assert.equal(q('#csview-overview').hidden, true);

    const channelRows = qa('#channel-table-body tr');
    assert.equal(channelRows.length, 2);
    assert.match(channelRows[0].textContent, /当前环境配置/);
    assert.match(channelRows[0].textContent, /OpenAI 兼容/);
    assert.match(channelRows[0].textContent, /api\.example\.com/);
    assert.match(channelRows[0].children[4].textContent, /^1\/1 通过/, '渠道行应汇总探测结果');
    assert.match(channelRows[1].textContent, /Anthropic Messages/);
    assert.equal(qa('#channel-table-body .cs-dot').length, 2);
    assert.equal(q('#channel-table-body button[data-cs-act="discover"]').dataset.csProvider, 'env-default');
    assert.equal(q('#channel-table-body button[data-cs-act="toggle"]').textContent, '停用');

    q('#channel-search').value = 'anthropic';
    q('#channel-search').dispatchEvent(new w.Event('input', { bubbles: true }));
    await settle();
    assert.equal(qa('#channel-table-body tr').length, 1);
    q('#channel-search').value = '';
    q('#channel-search').dispatchEvent(new w.Event('input', { bubbles: true }));
    await settle();
    assert.equal(qa('#channel-table-body tr').length, 2);

    // 渠道行的「编辑」应打开带原值的对话框
    q('#channel-table-body tr button[data-cs-act="edit"]').click();
    await settle();
    assert.equal(q('#provider-dialog').open, true);
    assert.equal(q('#provider-dialog-title').textContent, '编辑提供方');
    assert.equal(q('#model-provider-name').value, '当前环境配置');
    assert.match(q('#model-provider-url').value, /api\.example\.com/);
    q('#provider-dialog-cancel').click();
    assert.equal(q('#provider-dialog').open, false);

    // ── 模型广场：跨渠道 ────────────────────────────────────────────────
    q('#csnav-models').click();
    await settle();
    assert.equal(q('#csview-models').hidden, false);

    const rows = qa('#model-table-body tr');
    assert.equal(rows.length, 4, '模型广场应跨渠道列出全部模型');
    assert.equal(q('#model-channel').value, '', '默认应为全部渠道');
    assert.equal(qa('#model-channel option').length, 3);

    q('#model-channel').value = 'anthropic-main';
    q('#model-channel').dispatchEvent(new w.Event('change', { bubbles: true }));
    await settle();
    assert.equal(qa('#model-table-body tr').length, 1);
    assert.match(q('#model-table-body tr').textContent, /失败 503/);
    assert.match(q('#model-table-body tr').textContent, /Anthropic 主账号/);
    q('#model-channel').value = '';
    q('#model-channel').dispatchEvent(new w.Event('change', { bubbles: true }));
    await settle();
    assert.equal(qa('#model-table-body tr').length, 4);

    const pick = (needle) => qa('#model-table-body tr').find((row) => row.querySelector('.model-id').textContent === needle);
    const alphaRow = pick('alpha');
    const visionRow = pick('vision-model');
    const betaRow = pick('beta');
    assert.ok(alphaRow && visionRow && betaRow);
    assert.equal(alphaRow.classList.contains('is-active'), true);
    assert.match(alphaRow.textContent, /当前使用/);
    assert.match(alphaRow.textContent, /通过 88ms/);
    assert.match(alphaRow.textContent, /自动 · 128K\/4K · T=0\.3/);
    assert.match(alphaRow.textContent, /当前环境配置/, '模型行应标明所属渠道');
    assert.match(visionRow.textContent, /高 · 200K\/16K · T=0\.2/);
    assert.match(visionRow.textContent, /多模态/);
    assert.match(betaRow.textContent, /关闭 · 32K\/2K · T=0\.7/);
    assert.match(betaRow.textContent, /未测试/);
    assert.doesNotMatch(betaRow.textContent, /多模态/);
    assert.equal(alphaRow.querySelector('button[data-mc-act="remove"]'), null, '当前使用的模型不提供删除按钮');
    assert.equal(alphaRow.querySelector('button[data-mc-channel-jump]').dataset.mcChannelJump, 'env-default');

    // 点渠道名应把筛选切到该渠道
    alphaRow.querySelector('button[data-mc-channel-jump]').click();
    await settle();
    assert.equal(q('#model-channel').value, 'env-default');
    assert.equal(qa('#model-table-body tr').length, 3);
    q('#model-channel').value = '';
    q('#model-channel').dispatchEvent(new w.Event('change', { bubbles: true }));
    await settle();

    // 不回显密钥与原始字段名
    const consoleText = q('#view-services').textContent;
    assert.doesNotMatch(consoleText, /reasoning_level|context_window|max_output_tokens|\{"|api_key_set/);

    // ── 设置抽屉 ────────────────────────────────────────────────────────
    pick('beta').querySelector('button[data-mc-act="settings"]').click();
    await settle();
    assert.equal(q('#console-drawer').hidden, false);
    assert.equal(q('#drawer-model-name').textContent, 'beta');
    assert.match(q('#drawer-model-provider').textContent, /当前环境配置/);
    assert.equal(q('#model-settings-reasoning').value, 'off');
    assert.equal(q('#model-settings-context').value, '32000');
    assert.equal(q('#model-settings-output').value, '2048');
    assert.equal(q('#model-settings-temperature').value, '0.7');
    assert.equal(q('#model-settings-multimodal').checked, false);
    assert.equal(q('#drawer-model-usage').textContent.includes('4 次'), true);
    // 步长必须允许任意 token 数：step=1024 会让 200000 判定非法，浏览器直接拦下提交、handler 根本不触发。
    assert.equal(q('#model-settings-context').step, '1', '上下文窗口步长必须为 1');
    assert.equal(q('#model-settings-output').step, '1', '最大输出步长必须为 1');
    q('#model-settings-context').value = '200000';
    assert.equal(q('#model-settings-context').validity.stepMismatch, false, '200000 不应触发步长校验失败');
    q('#drawer-close').click();
    await settle();
    assert.equal(q('#console-drawer').hidden, true);

    // ── 搜索、空态、状态筛选 ────────────────────────────────────────────
    q('#model-search').value = 'vision';
    q('#model-search').dispatchEvent(new w.Event('input', { bubbles: true }));
    await settle();
    assert.equal(qa('#model-table-body tr').length, 1);
    assert.equal(qa('#model-table-body mark').length, 1);
    assert.equal(q('#model-table-empty').hidden, true);

    q('#model-search').value = 'nothing-matches';
    q('#model-search').dispatchEvent(new w.Event('input', { bubbles: true }));
    await settle();
    assert.equal(qa('#model-table-body tr').length, 0);
    assert.equal(q('#model-table-empty').hidden, false);
    q('#model-search').value = '';
    q('#model-search').dispatchEvent(new w.Event('input', { bubbles: true }));
    await settle();

    q('#model-filter').value = 'untested';
    q('#model-filter').dispatchEvent(new w.Event('change', { bubbles: true }));
    await settle();
    assert.equal(qa('#model-table-body tr').length, 2);
    q('#model-filter').value = 'passed';
    q('#model-filter').dispatchEvent(new w.Event('change', { bubbles: true }));
    await settle();
    assert.equal(qa('#model-table-body tr').length, 1);
    q('#model-filter').value = 'failed';
    q('#model-filter').dispatchEvent(new w.Event('change', { bubbles: true }));
    await settle();
    assert.equal(qa('#model-table-body tr').length, 1);
    assert.match(q('#model-table-body tr').textContent, /claude-sonnet/);
    q('#model-filter').value = 'all';
    q('#model-filter').dispatchEvent(new w.Event('change', { bubbles: true }));
    await settle();

    // ── 全选与批量按钮（跨渠道） ────────────────────────────────────────
    q('#model-select-all').checked = true;
    q('#model-select-all').dispatchEvent(new w.Event('change', { bubbles: true }));
    await settle();
    assert.match(q('#model-selection').textContent, /已选中 4 个模型/);
    assert.equal(q('#model-remove-selected').disabled, false);
    assert.equal(q('#model-test-selected').disabled, false);
    q('#model-select-all').checked = false;
    q('#model-select-all').dispatchEvent(new w.Event('change', { bubbles: true }));
    await settle();
    assert.match(q('#model-selection').textContent, /未选中模型/);
    assert.equal(q('#model-remove-selected').disabled, true);

    // ── 新增渠道对话框与添加模型对话框 ──────────────────────────────────
    q('#provider-new').click();
    await settle();
    assert.equal(q('#provider-dialog').open, true);
    assert.equal(q('#provider-dialog-title').textContent, '新增提供方');
    assert.equal(q('#model-provider-enabled').checked, true);
    q('#provider-dialog-cancel').click();
    assert.equal(q('#provider-dialog').open, false);

    q('#model-add').click();
    await settle();
    assert.equal(q('#model-add-dialog').open, true);
    assert.equal(qa('#model-add-channel option').length, 2);
    assert.equal(q('#model-add-channel').value, 'env-default');
    assert.match(q('#model-add-target').textContent, /当前环境配置/);
    q('#model-add-cancel').click();
    assert.equal(q('#model-add-dialog').open, false);

    // ── 请求日志：进入分区才加载 ────────────────────────────────────────
    assert.equal(qa('#log-table-body tr').length, 0, '未进入日志分区时不应请求日志');
    q('#csnav-logs').click();
    await settle(160);
    assert.equal(q('#csview-logs').hidden, false);
    const logRows = qa('#log-table-body tr');
    assert.equal(logRows.length, 2);
    assert.match(logRows[0].textContent, /429/);
    assert.match(logRows[0].textContent, /上游 HTTP 429/);
    assert.match(logRows[1].textContent, /Responses/);
    assert.match(q('#log-page').textContent, /共 2 条/);
    assert.equal(q('#log-prev').disabled, true);

    // ── 用量统计 ────────────────────────────────────────────────────────
    q('#csnav-usage').click();
    await settle(120);
    assert.equal(q('#csview-usage').hidden, false);
    assert.equal(q('#csview-logs').hidden, true);
    assert.equal(q('#usage-total').textContent, '12');
    assert.equal(q('#usage-success').textContent, '91.7%');
    assert.equal(q('#usage-latency').textContent, '320ms');
    assert.equal(q('#usage-p95').textContent, '900ms');
    assert.equal(q('#usage-tokens').textContent, '4,096');
    assert.ok(q('#usage-chart svg'), '用量图表应渲染内联 SVG');
    assert.equal(qa('#usage-models .usage-row').length, 2);
    assert.equal(qa('#usage-providers .usage-row').length, 1);
    assert.equal(q('#usage-errors .usage-bar i').classList.contains('bad'), true);
    // 失败原因的次数曾经渲染成 undefined（映射后 describe 取了已删除的字段）。
    assert.match(q('#usage-errors .usage-row').textContent, /1 次/, '失败原因应显示次数');
    assert.doesNotMatch(q('#usage-errors').textContent, /undefined/);

    // 概览的「用量统计 →」快捷入口
    q('#csnav-overview').click();
    await settle(80);
    assert.equal(q('#csview-overview').hidden, false);
    q('[data-cs-goto="usage"]').click();
    await settle(80);
    assert.equal(q('#csview-usage').hidden, false);

    // Use the actual backend role catalog; health is now a read-only tool.
    q('#csnav-overview').click();
    await settle(80);
    const roleRows = qa('#role-list .cs-role-row');
    assert.equal(roleRows.length, 1, '当前后端只公开小爱音箱角色');
    assert.match(roleRows[0].textContent, /小爱音箱/);
    assert.equal(roleRows[0].classList.contains('is-following'), true, '初始应跟随默认模型');
    assert.match(roleRows[0].textContent, /跟随默认模型/);
    assert.equal(roleRows[0].querySelector('[data-role-apply]').disabled, true, '未改动时应用按钮应禁用');

    // Give the assistant beta while keeping the default route unchanged.
    const defaultModelBeforeRoleChange = q('#gateway-active-model').textContent;
    const speakerSelect = roleRows[0].querySelector('[data-role-select]');
    speakerSelect.value = 'env-default\u0000beta';
    speakerSelect.dispatchEvent(new w.Event('change', { bubbles: true }));
    await settle(40);
    assert.equal(roleRows[0].querySelector('[data-role-apply]').disabled, false, '改成别的模型后可应用');
    roleRows[0].querySelector('[data-role-apply]').click();
    await settle(160);
    const speakerRow = q('#role-list .cs-role-row:nth-child(1)');
    assert.equal(speakerRow.classList.contains('is-following'), false, '小爱音箱应已单独指定');
    assert.match(speakerRow.textContent, /beta/);
    assert.equal(q('#gateway-active-model').textContent, defaultModelBeforeRoleChange, '助手覆盖不能修改默认路由');

    // 再改回「跟随默认模型」
    const speakerSelect2 = speakerRow.querySelector('[data-role-select]');
    speakerSelect2.value = '';
    speakerSelect2.dispatchEvent(new w.Event('change', { bubbles: true }));
    await settle(40);
    speakerRow.querySelector('[data-role-apply]').click();
    await settle(160);
    assert.equal(q('#role-list .cs-role-row:nth-child(1)').classList.contains('is-following'), true, '应能改回跟随默认');

    const real = errors.filter((error) => !/Not implemented/.test(String(error && error.message)));
    assert.deepEqual(real.map((error) => String(error && error.message)), [], '页面运行期出现未捕获异常');
    console.log('模型中枢 DOM 校验通过：5 个分区、概览指标与统一入口、渠道表格、跨渠道模型广场、抽屉、对话框、日志与用量。');
  } finally {
    w.close();
  }
}

/* 网关鉴权文案必须跟随 auth_required。
 *
 * 回归背景：后端 public() 里的 token_required 曾写死为 true，准确的
 * auth_required 却没人读，于是一个免密部署在页面上被显示成「密钥校验已启用」，
 * 接入指南还让人去 NAS 读令牌文件。这里把四种组合都钉住，尤其是
 * 「两字段不一致时以 auth_required 为准」——那正是当初漏掉的分支。
 */
async function runGatewayAuthScenarios() {
  const base = {
    active_alias: 'hub-active', openai_path: '/v1/chat/completions',
    responses_path: '/v1/responses', anthropic_path: '/v1/messages', compatibility_note: '',
  };
  const cases = [
    { name: '免密部署（与线上一致）', fields: { ...base, auth_required: false, token_required: false }, expect: /免密访问/ },
    { name: '开启校验', fields: { ...base, auth_required: true, token_required: true }, expect: /密钥校验已启用/ },
    { name: '两字段不一致时以 auth_required 为准', fields: { ...base, auth_required: false, token_required: true }, expect: /免密访问/ },
    { name: '旧后端只发 token_required 时回退', fields: { ...base, token_required: false }, expect: /免密访问/ },
  ];
  for (const item of cases) {
    const { w, q } = boot(item.fields);
    try {
      w.eval(script);
      await settle(120);
      assert.match(q('#gateway-token-state').textContent, item.expect, `网关鉴权文案错误：${item.name}`);
    } finally {
      w.close();
    }
  }
  console.log(`网关鉴权文案：${cases.length} 种状态全部符合预期。`);
}

run()
  .then(runGatewayAuthScenarios)
  .catch((error) => {
    console.error(error);
    process.exitCode = 1;
  });
