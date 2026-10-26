// Mocked commands only. This script never contacts real devices or cloud services.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { JSDOM } = require(process.env.HUB_JSDOM_MODULE || 'jsdom');
const root = path.resolve(__dirname, '../src/xiaomi_ai_bridge/web');
const at = new Date().toISOString();
const command = (action, label, type = 'button', extra = {}) => ({action, label, type, available: true, ...extra});
const speaker = {kind: 'speaker', available: true, message: '专属服务已连接', observed_at: at,
  state: {volume: 30, playback: 'playing', title: '测试歌曲', artist: '歌手', loop: 1},
  controls: [command('speaker_play', '播放'), command('speaker_pause', '暂停'), command('speaker_stop', '停止'),
    command('speaker_next', '下一首', 'button', {available: false, reason: '请使用完整能力面板'}),
    command('action_s5_a2', '上一首', 'button', {requires_confirmation: true}),
    command('speaker_volume', '音量', 'number', {min: 0, max: 100, step: 1}),
    command('speaker_loop', '循环方式', 'select', {options: [{value: 0, label: '单曲循环'}, {value: 1, label: '列表循环'}]}),
    command('speaker_tts', '文字播报', 'text', {requires_confirmation: true}),
    command('speaker_url', '音频链接', 'url', {requires_confirmation: true})]};
const infrared = {kind: 'infrared', available: true, message: '遥控目录已连接',
  remotes: [{key: 'tv', name: '客厅电视', category: '电视', buttons: [{key: 'power', label: '电源'}, {key: 'up', label: '音量加'}]}],
  learned_buttons: [{key: 'learned-one', label: '空调电源'}, {key: 'learned-two', label: '空调温度'}],
  learning: {supported: true, active: false, ready: false, message: '尚未开始学习'},
  controls: ['ir_send', 'ir_send_learned', 'ir_learn_start', 'ir_learn_read', 'ir_learn_save', 'ir_learn_rename', 'ir_learn_delete'].map(action =>
    command(action, {ir_send: '发送遥控按键', ir_learn_start: '开始学习', ir_learn_read: '检查学习结果', ir_learn_save: '保存按键', ir_learn_rename: '修改名称', ir_learn_delete: '删除按键', ir_send_learned: '发送学习按键'}[action], 'button', {requires_confirmation: ['ir_send','ir_send_learned','ir_learn_start','ir_learn_delete'].includes(action)}))};
const devices = [
  {key: 'speaker', alias: '客厅小爱', name: '客厅小爱', category: 'speaker', category_label: '音箱', enabled: true, cloud_online: true, observed_at: at, online: true, state: {}, controls: [], capabilities_actions: [{action: 'action_s5_a2', label: '上一首', service: '播放器', enabled: true, requires_confirmation: true, inputs: []}]},
  // Real-world IR gateways may be cloud-online without readable MIoT properties.
  {key: 'infrared', alias: '遥控器', name: '遥控器', category: 'ir-remote-control', category_label: '红外遥控器', enabled: true, cloud_online: true, state: {}, controls: []},
];
const settle = () => new Promise(resolve => setTimeout(resolve, 25));

async function run() {
  const writes = [];
  let allow = false;
  let stream;
  let releaseSpeaker;
  let delaySpeaker = false;
  const dom = new JSDOM(fs.readFileSync(path.join(root, 'index.html'), 'utf8'), {url: 'http://hub.test/#home', runScripts: 'outside-only', pretendToBeVisual: true});
  const w = dom.window;
  w.EventSource = class {constructor() {stream = this;} close() {}};
  w.confirm = () => allow;
  w.fetch = async (url, options = {}) => {
    let data = {};
    if (url === '/api/home') data = {devices, audit: []};
    else if (url === '/api/hub') data = {resources: [], events: []};
    else if (url === '/api/status') data = {ready: true, started_at: at};
    else if (url.includes('/auth/')) data = {state: 'idle'};
    else if (url.startsWith('/api/home/layout')) data = {available: true, homes: [{key: 'house', name: '我的家', rooms: [{key: 'living', name: '客厅', device_keys: ['speaker']}, {key: 'bed', name: '卧室', device_keys: ['infrared']}]}], assignments: {speaker: {home_key: 'house', home_name: '我的家', room_key: 'living', room_name: '客厅'}, infrared: {home_key: 'house', home_name: '我的家', room_key: 'bed', room_name: '卧室'}}};
    else if (url.endsWith('/state')) data = {state: {}, observed_at: at};
    else if (url.includes('/extensions')) {
      if (url.includes('/speaker/') && delaySpeaker) await new Promise(resolve => {releaseSpeaker = resolve;});
      data = url.includes('/speaker/') ? speaker : infrared;
    } else if (url.endsWith('/control')) {
      const body = JSON.parse(options.body);
      writes.push(body);
      if (body.action === 'speaker_volume') speaker.state.volume = body.value;
      if (body.action === 'ir_learn_start') Object.assign(infrared.learning, {active: true, ready: false});
      if (body.action === 'ir_learn_read') Object.assign(infrared.learning, {active: false, ready: true});
      if (body.action === 'ir_learn_save') {
        infrared.learned_buttons.push({key: 'new-key', label: body.value.label});
        Object.assign(infrared.learning, {active: false, ready: false});
      }
      data = {ok: true, outcome: 'accepted', message: '指令已接收，实际结果尚未确认', extensions: url.includes('/speaker/') ? speaker : infrared};
    }
    return {ok: true, status: 200, json: async () => structuredClone(data)};
  };
  const query = selector => w.document.querySelector(selector);
  const input = (element, value) => {element.value = value; element.dispatchEvent(new w.Event('input', {bubbles: true}));};
  try {
    w.eval(fs.readFileSync(path.join(root, 'app.js'), 'utf8'));
    await settle();
    assert.equal(query('#home-room-filter').options.length, 4);
    const filter = query('#home-room-filter');
    filter.value = JSON.stringify(['house', 'living']);
    filter.dispatchEvent(new w.Event('change', {bubbles: true}));
    assert.equal(w.document.querySelectorAll('.home-device-card').length, 1);
    assert.match(query('.home-card-identity small').textContent, /客厅/);
    await w.loadHomeLayout(true);
    assert.equal(filter.value, JSON.stringify(['house', 'living']), 'Room selection persists after refresh');

    w.openHomeDevice('speaker');
    await settle();
    assert.equal(query('.speaker-track').textContent, '测试歌曲');
    assert.equal(query('[data-extension-command="speaker_next"]').disabled, true);
    assert.equal(query('[data-extension-command="speaker_previous"]'), null);
    const volume = query('[data-extension-action="speaker_volume"]');
    const slider = volume.querySelector('[data-capability-slider]');
    const precise = volume.querySelector('[data-extension-input]');
    assert.equal(precise.value, '30');
    input(slider, 67);
    assert.equal(precise.value, '67');
    assert.equal(writes.length, 0, 'Dragging never controls the speaker');
    stream.onmessage({data: JSON.stringify({type: 'home.extensions', device_key: 'speaker', extensions: {...speaker, observed_at: new Date(Date.now() + 1000).toISOString(), state: {...speaker.state, volume: 40}}})});
    assert.equal(precise.value, '67', 'SSE does not erase edits');
    input(precise, 58);
    assert.equal(slider.value, '58');
    volume.querySelector('button').click();
    await settle();
    assert.equal(writes.length, 1);
    assert.equal(writes[0].value, 58);
    assert.ok(writes[0].request_id);
    assert.match(query('#home-detail-message').textContent, /实际结果尚未确认/);

    const tts = query('[data-extension-action="speaker_tts"]');
    input(tts.querySelector('[data-extension-input]'), '你好，家中中枢');
    tts.querySelector('button').click();
    await settle();
    assert.equal(writes.length, 1, 'Canceling TTS confirmation sends nothing');
    allow = true;
    tts.querySelector('button').click();
    await settle();
    assert.equal(writes[1].action, 'speaker_tts');
    assert.equal(writes[1].confirmed, true);

    w.openHomeDevice('infrared');
    await settle();
    assert.equal(query('[data-button-key="power"]').disabled, false, 'MIoT readings are not required for cloud-online IR');
    query('[data-button-key="power"]').click();
    await settle();
    assert.deepEqual(writes[2].value, {remote_key: 'tv', button_key: 'power'});
    assert.equal(writes[2].confirmed, true);
    query('[data-extension-command="ir_learn_start"]').click();
    await settle();
    assert.deepEqual(writes[3].value, {}, 'Server chooses reserved learning slot');
    query('[data-extension-command="ir_learn_read"]').click();
    await settle();
    const save = query('[data-extension-action="ir_learn_save"]');
    assert.equal(save.querySelector('button').disabled, false);
    input(save.querySelector('[data-extension-input]'), '风扇开关');
    save.querySelector('button').click();
    await settle();
    assert.equal(writes[5].value.label, '风扇开关');
    assert.match(query('#home-extensions').textContent, /风扇开关/);

    const names = [...w.document.querySelectorAll('[data-extension-action="ir_learn_rename"] [data-extension-input]')];
    input(names[0], '保留草稿一');
    input(names[1], '保留草稿二');
    await w.loadHomeExtensions('infrared', true);
    assert.equal(names[0].value, '保留草稿一');
    assert.equal(names[1].value, '保留草稿二');
    names[0].closest('form').querySelector('button').click();
    await settle();
    assert.equal(names[1].value, '保留草稿二', 'Renaming one key preserves another key draft');

    delaySpeaker = true;
    w.openHomeDevice('speaker');
    await settle();
    w.openHomeDevice('infrared');
    await settle();
    releaseSpeaker();
    delaySpeaker = false;
    await settle();
    assert.match(query('#home-detail-name').textContent, /遥控器/);
    assert.equal(query('.speaker-track'), null, 'Late speaker response cannot replace infrared panel');
    devices[1].cloud_online = false;
    w.renderHome({devices: structuredClone(devices), audit: []});
    assert.ok([...w.document.querySelectorAll('#home-extensions [data-extension-command]')].every(button => button.disabled));
    assert.equal(writes.length, 7, 'Opening panels, reading and polling do not execute commands');
    w.openHomeDevice('speaker');
    await settle();
    allow = false;
    query('[data-extension-command="action_s5_a2"]').click();
    await settle();
    assert.equal(writes.length, 7, 'Generic MIoT shortcuts preserve cancellation');
    allow = true;
    query('[data-extension-command="action_s5_a2"]').click();
    await settle();
    assert.equal(writes[7].action, 'action_s5_a2');
    assert.equal(writes[7].confirmed, true);
    assert.equal(query('.speaker-track').textContent, '测试歌曲');
    console.log('PASS: speaker forms and linked volume slider; explicit confirmation; IR keypads without MIoT reads; learn/save/rename flow; per-key draft preservation; accepted result labels; room filters; SSE sync; late responses isolated; offline gating; no writes on render or drag.');
  } finally {w.close();}
}

async function checkLive(base) {
  const boundedFetch = (url, options = {}) => {
    assert.ok(!options.method || options.method === 'GET', 'Live verification strictly prohibits POST and device writes');
    return fetch(new URL(url, base), {...options, signal: AbortSignal.timeout(18000)});
  };
  const [page, script, home, hub] = await Promise.all([
    boundedFetch('/').then(response => response.text()), boundedFetch('/app.js').then(response => response.text()),
    boundedFetch('/api/home').then(response => response.json()), boundedFetch('/api/hub').then(response => response.json()),
  ]);
  assert.ok(home.devices?.length, 'Live home inventory must not be empty');
  const dom = new JSDOM(page, {url: new URL('/#home', base).href, runScripts: 'outside-only', pretendToBeVisual: true});
  const w = dom.window;
  w.EventSource = class {close(){}};
  w.confirm = () => {throw new Error('Live verification must not request physical controls');};
  const closeHarness = require('./readonly_dom_harness.cjs')(w, base);
  const bounded = async (work, label) => {
    let timer;
    try {return await Promise.race([work, new Promise((_, reject) => {timer=setTimeout(() => reject(new Error(`${label} exceeded 20 seconds`)), 20000);})]);}
    finally {clearTimeout(timer);}
  };
  try {
    w.eval(script);
    const deadline = Date.now()+20000;
    while (w.document.querySelectorAll('.home-device-card').length !== home.devices.length && Date.now()<deadline) await settle();
    assert.equal(w.document.querySelectorAll('.home-device-card').length, home.devices.length);
    for (const device of home.devices) {
      w.openHomeDevice(device.key);
      const kind = w.homeExtensionKind(device);
      if (kind) await bounded(w.loadHomeExtensions(device.key), `${device.name} extensions`);
      await settle();
      assert.equal(w.document.querySelector('#home-detail-name').textContent, device.alias || device.name);
      assert.equal(w.document.querySelectorAll('#home-capabilities [data-property-key]').length, (device.properties || []).length);
      if (kind) {
        assert.equal(w.document.querySelector('#home-extensions').hidden, false);
        assert.ok(w.document.querySelector('.extension-connection-message').textContent);
      }
      const resourceId = device.resource_id || `home.${device.category}.${device.key}`;
      const resource = hub.resources.find(item => item.resource_id === resourceId);
      if (resource) {
        await bounded(w.selectHubResource(resourceId), `${device.name} fields`);
        assert.ok(w.document.querySelectorAll('.hub-field-row').length >= (resource.fields || []).length);
        assert.doesNotMatch(w.document.querySelector('#hub-detail').textContent, /Invalid Date|NaN/);
      }
      console.log(`PASS live GET-only: ${device.alias || device.name}; ${(device.properties || []).length} declared properties; ${kind || 'generic'} controls; ${(resource?.fields || []).length} fields.`);
    }
    console.log(`PASS: deployed UI opened all ${home.devices.length} devices and field dictionaries using GET only. No controls, refresh POST, or real writes were executed.`);
  } finally {await closeHarness();}
}
const liveBase = process.argv.slice(2).find(arg => /^https?:\/\//.test(arg));
run().then(() => liveBase ? checkLive(liveBase) : null).catch(error => {console.error(error); process.exitCode = 1;});
