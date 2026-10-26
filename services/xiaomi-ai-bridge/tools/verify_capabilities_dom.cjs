// Simulated capability controls only. Never contacts or controls a real device.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { JSDOM } = require(process.env.HUB_JSDOM_MODULE || 'jsdom');
const root = path.resolve(__dirname, '../src/xiaomi_ai_bridge/web');
const at = new Date().toISOString();
const device = {
  key: 'router', name: '路由器', alias: '路由器', enabled: true, cloud_online: true,
  category: 'router', category_label: '路由器', state: {rate: 40},
  observed_at: at, online: true, stale: false, controls: [],
  properties: [
    {key: 'rate', label: '频率', service: '运行设置', unit: '%', readable: true, writable: true},
    {key: 'secret', label: '密码', service: '运行设置', readable: false, sensitive: true},
  ],
  extra_controls: [{action: 'prop_s1_p1', property: 'rate', label: '频率', service: '运行设置',
    kind: 'range', min: 1, max: 100, step: 1, unit: '%', enabled: true}],
  capabilities_actions: [{action: 'action_s1_a1', label: '重启', service: '运行设置',
    requires_confirmation: true, enabled: true,
    inputs: [{key: 'p2', label: '等待秒数', kind: 'range', min: 1, max: 30, step: 1}]}],
  capabilities_events: [{key: 'event_s1_e1', label: '状态变化', service: '运行设置',
    arguments: [], status: '事件订阅未接入'}],
};
const settle = () => new Promise(resolve => setTimeout(resolve, 30));

async function run() {
  const commands = [];
  let allow = false;
  const dom = new JSDOM(fs.readFileSync(path.join(root, 'index.html'), 'utf8'), {
    url: 'http://hub.test/#home', runScripts: 'outside-only', pretendToBeVisual: true,
  });
  const w = dom.window;
  w.EventSource = class { close() {} };
  w.confirm = () => allow;
  w.fetch = async (url, options = {}) => {
    let data = {};
    if (url === '/api/home') data = {devices: [device], audit: []};
    else if (url === '/api/hub') data = {resources: [], events: []};
    else if (url === '/api/status') data = {ready: true, started_at: at};
    else if (url.includes('/auth/')) data = {state: 'idle'};
    else if (url.endsWith('/state')) data = {state: device.state, observed_at: at};
    else if (url.endsWith('/control')) {
      commands.push(JSON.parse(options.body));
      data = {ok: true, outcome: 'accepted', message: '设备已接收指令，执行结果需在设备上确认'};
    }
    return {ok: true, status: 200, json: async () => structuredClone(data)};
  };
  try {
    w.eval(fs.readFileSync(path.join(root, 'app.js'), 'utf8'));
    await settle();
    w.openHomeDevice('router');
    await settle();
    assert.equal(w.document.querySelectorAll('[data-home-category]').length, 2);
    assert.equal(w.document.querySelectorAll('[data-property-key]').length, 2);
    assert.match(w.document.querySelector('#home-capabilities').textContent, /敏感信息已隐藏/);

    const form = w.document.querySelector('[data-capability-action="prop_s1_p1"]');
    const input = form.querySelector('[data-capability-input]');
    const slider = form.querySelector('[data-capability-slider]');
    assert.equal(input.value, '40');
    assert.equal(slider.value, '40');
    slider.value = 70;
    slider.dispatchEvent(new w.Event('input', {bubbles: true}));
    assert.equal(input.value, '70');
    assert.equal(commands.length, 0, 'Dragging must not send commands');
    w.renderHome({devices: [structuredClone(device)], audit: []});
    assert.equal(input.value, '70', 'Polling must retain drafts');
    assert.equal(slider.value, '70');
    input.value = 65;
    input.dispatchEvent(new w.Event('input', {bubbles: true}));
    assert.equal(slider.value, '65');
    form.querySelector('button').click();
    await settle();
    assert.equal(commands.length, 1);
    assert.equal(commands[0].value, 65);
    assert.ok(commands[0].request_id);
    assert.match(w.document.querySelector('#home-detail-message').textContent, /已接收/);
    assert.equal(w.document.querySelector('#home-detail-message').classList.contains('bad'), false);

    const action = w.document.querySelector('[data-capability-action="action_s1_a1"]');
    const actionSlider = action.querySelector('[data-capability-slider]');
    actionSlider.value = 9;
    actionSlider.dispatchEvent(new w.Event('input', {bubbles: true}));
    assert.equal(action.querySelectorAll('[data-capability-input]').length, 1);
    action.querySelector('button').click();
    await settle();
    assert.equal(commands.length, 1, 'Canceling confirmation must not send');
    allow = true;
    action.querySelector('button').click();
    await settle();
    assert.equal(commands.length, 2);
    assert.equal(commands[1].confirmed, true);
    assert.deepEqual(commands[1].value, {p2: 9});
    device.cloud_online = false;
    w.renderHome({devices: [structuredClone(device)], audit: []});
    assert.ok([...w.document.querySelectorAll('#home-capabilities input,#home-capabilities select,#home-capabilities button')].every(node => node.disabled));
    console.log('PASS: complete catalog; dynamic categories; sensitive fields; linked sliders and precise inputs; one value per action parameter; polling drafts; no writes on drag; confirmation cancellation; accepted results; offline controls disabled.');
  } finally {
    w.close();
  }
}
run().catch(error => { console.error(error); process.exitCode = 1; });
