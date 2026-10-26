// Mixed-ecosystem field dictionary regression. Fetch is mocked; no device writes.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {JSDOM} = require(process.env.HUB_JSDOM_MODULE || 'jsdom');
const root = path.resolve(__dirname, '../src/xiaomi_ai_bridge/web');
const at = new Date().toISOString();
const source = 'mqtt';
const resource = {resource_id: 'sensor.balcony', name: '阳台传感器', kind: 'sensor', observed_at: at, received_at: at, online: true, stale: false,
  state: {environment: {temperature: 26.5, humidity: 0}, 'indicator-light.on': false, secret: 'must-never-display', attempts: 12, label: '舒适', mode: 1, serial_no: 1234567890, device_id: 987654321, firmware_version: 2216, internal_accuracy: 99},
  fields: [
    {key: 'environment', path: ['environment'], label: '阳台环境', type: 'object', source, status: 'ok', readable: true},
    {key: '/environment/temperature', path: ['environment','temperature'], label: '阳台温度', type: 'number', unit: '℃', semantic: 'temperature', source, status: 'ok', readable: true, source_updated_at: '2026-09-23T14:31:16+00:00', source_sample_cached: false},
    {key: '/environment/humidity', path: ['environment','humidity'], label: '阳台湿度', type: 'number', unit: '%', source, status: 'ok', readable: true},
    {key: 'indicator-light.on', path: ['indicator-light.on'], label: '指示灯', type: 'boolean', source: 'miot.cloud', status: 'ok', readable: true},
    {key: 'secret', path: ['secret'], label: '访问密钥', type: 'string', source, status: 'sensitive', readable: false},
    {key: 'power', path: ['power'], label: '功率', type: 'number', unit: 'W', source: 'miot.cloud', status: 'unavailable', message: '云端未返回该属性', code: -704002000, attempted_at: at, readable: true},
    {key: 'serial', path: ['serial'], label: '设备序列号', type: 'string', source: 'miot.cloud', status: 'unsupported', message: '该接口不支持此属性', readable: true},
    {key: 'motion', path: ['motion'], label: '运动检测', type: 'boolean', source, status: 'not_readable', message: '仅能通过事件上报', readable: false},
    {key: 'attempts', path: ['attempts'], label: '累计次数', type: 'number', unit: '次', source, status: 'stale', observed_at: at, readable: true},
    {key: 'label', path: ['label'], label: '舒适度', type: 'string', source, status: 'ok', readable: true},
    {key: 'mode', path: ['mode'], label: '模式', type: 'enum', source, status: 'ok', readable: true, options: [{value: 1, label: '自动'}]},
    {key: 'serial_no', path: ['serial_no'], label: '序列号', type: 'number', role: 'identity', chartable: false, comparable: false, source, status: 'ok', readable: true},
    {key: 'device_id', path: ['device_id'], label: '设备编号', type: 'number', source, status: 'ok', readable: true},
    {key: 'firmware_version', path: ['firmware_version'], label: '固件版本', type: 'number', source, status: 'ok', readable: true},
    {key: 'internal_accuracy', path: ['internal_accuracy'], label: '内部精度', type: 'number', role: 'measurement', chartable: false, source, status: 'ok', readable: true},
    {key: 'vibration_text', path: ['vibration_text'], label: '振动文案', type: 'string', role: 'parameter', priority: 'detail', source, status: 'unavailable', readable: false, message: '动作输入参数，在执行时填写，不是可采样读数'},
    {key: 'health_event', path: ['health_event'], label: '健康事件返回', type: 'number', role: 'event', priority: 'detail', source, status: 'unknown', readable: false},
  ]};
const device = {key: 'watch', name: '手表', alias: '手表', category: 'watch', enabled: true, cloud_online: true, state: {count: 4}, controls: [],
  properties: [{key: 'health', label: '健康数据', service: '传感器', readable: false, notifiable: true, read_status: {status: 'not_readable', message: '由事件或动作返回', source: 'miot.cloud'}},
    {key: 'count', label: '次数', service: '传感器', readable: true, read_status: {status: 'unavailable', message: '本次读取失败', observed_at: at, attempted_at: at, source: 'miot.cloud'}}]};
const settle = () => new Promise(resolve => setTimeout(resolve, 25));

async function run() {
  const dom = new JSDOM(fs.readFileSync(path.join(root, 'index.html'), 'utf8'), {url: 'http://hub.test/#data', runScripts: 'outside-only', pretendToBeVisual: true});
  const w = dom.window;
  w.EventSource = class {close(){}};
  w.fetch = async (url, options={}) => {
    assert.ok(!options.method || options.method === 'GET', 'Dictionary must never write');
    let data = {};
    if (url === '/api/home') data = {devices: [device], audit: []};
    else if (url === '/api/hub') data = {resources: [resource], events: []};
    else if (url === '/api/status') data = {ready: true, started_at: at};
    else if (url.includes('/history')) data = {events: [
      {observed_at: new Date(Date.now()-60000).toISOString(), state: {environment: {temperature: 25.1, humidity: 1}}},
      {observed_at: new Date(Date.now()-30000).toISOString(), state: {environment: {temperature: 25.1}}, fields: [{key: '/environment/temperature', path: ['environment','temperature'], type: 'number', status: 'unavailable', observed_at: new Date(Date.now()-60000).toISOString()}]},
      {observed_at: at, state: resource.state, fields: resource.fields.map(field => ({...field,observed_at:field.observed_at || at}))},
    ]};
    else if (url.includes('/auth/')) data = {state: 'idle'};
    else if (url.endsWith('/state')) data = {state: device.state, observed_at: at};
    return {ok: true, status: 200, json: async()=>structuredClone(data)};
  };
  const query = selector => w.document.querySelector(selector);
  const change = (selector, value) => {const el=query(selector);el.value=value;el.dispatchEvent(new w.Event('change',{bubbles:true}));};
  try {
    w.eval(fs.readFileSync(path.join(root, 'app.js'), 'utf8'));
    await settle();
    await w.selectHubResource(resource.resource_id);
    assert.equal(w.document.querySelectorAll('.hub-field-row').length, 17);
    assert.match(query('[data-field-key="vibration_text"]').textContent,/执行时填写.*动作输入参数/);
    assert.equal(query('[data-field-key="vibration_text"] .status-pill').classList.contains('bad'),false);
    assert.match(query('[data-field-key="health_event"]').textContent,/由事件或动作返回/);
    assert.doesNotMatch(query('#hub-identity-grid').textContent,/振动文案|健康事件返回/);
    assert.match(query('[data-field-key="/environment/temperature"]').textContent, /26.5 ℃/);
    assert.match(query('[data-field-key="/environment/temperature"]').textContent, /云端属性更新.*非采样时间/);
    assert.match(query('#hub-state-grid').textContent, /云端属性更新.*实时性未核实/);
    assert.doesNotMatch(query('#view-data').textContent, /设备上次上报/);
    assert.match(query('[data-field-key="/environment/humidity"]').textContent, /0 %/);
    assert.match(query('[data-field-key="indicator-light.on"]').textContent, /关闭/);
    assert.match(query('[data-field-key="power"]').textContent, /读取失败.*云端未返回该属性/);
    assert.match(query('[data-field-key="serial"]').textContent, /接口不支持/);
    assert.match(query('[data-field-key="attempts"]').textContent, /12 次.*上次读数/);
    assert.doesNotMatch(query('#view-data').textContent, /must-never-display/);
    assert.match(query('[data-field-key="secret"]').textContent, /敏感信息已隐藏/);

    const metrics = [...query('#hub-history-metric').options];
    assert.ok(metrics.some(option => option.value === '/environment/temperature' && option.textContent === '阳台温度（℃）'));
    assert.ok(!metrics.some(option => option.value === 'indicator-light.on'), 'Boolean fields never become numeric charts');
    assert.ok(!metrics.some(option => option.value === 'mode'), 'Enum codes never become numeric charts');
    assert.ok(!metrics.some(option => ['serial_no','device_id','firmware_version','internal_accuracy'].includes(option.value)), 'Identities, versions and fields with chartable:false never enter chart options');
    assert.match(query('[data-field-key="serial_no"]').textContent, /1234567890/);
    assert.match(query('#hub-field-list').textContent, /设备资料.*用于识别设备，不参与趋势或数值比较/);
    change('#hub-field-role', 'identity');
    assert.equal(w.document.querySelectorAll('.hub-field-row').length, 4);
    change('#hub-field-role', 'all');
    change('#hub-history-metric', '/environment/temperature');
    assert.equal(w.document.querySelectorAll('#hub-chart circle').length, 2);
    assert.equal((query('#hub-chart path').getAttribute('d').match(/M/g) || []).length, 2, 'A retained stale value leaves a gap rather than a fake sample');
    assert.match(query('#hub-chart svg').getAttribute('aria-label'), /阳台温度.*℃/);
    const repeatedPoints = w.historyMetricPoints([
      {observed_at: at, state: {temperature: 27}, fields: [{key: 'temperature', type: 'number', status: 'ok', observed_at: at, source_sample_cached: false}]},
      {observed_at: new Date(Date.now()+1000).toISOString(), state: {temperature: 27}, fields: [{key: 'temperature', type: 'number', status: 'ok', observed_at: new Date(Date.now()+1000).toISOString(), source_sample_cached: true}]},
      {observed_at: new Date(Date.now()+2000).toISOString(), state: {temperature: 28}, fields: [{key: 'temperature', type: 'number', status: 'ok', observed_at: new Date(Date.now()+2000).toISOString(), source_sample_cached: false}]},
    ], {key: 'temperature', aliases: ['temperature']});
    assert.equal(repeatedPoints.length, 2, 'Repeated upstream timestamp and value are not a new sensor sample');
    assert.deepEqual(Array.from(repeatedPoints, point=>point.value), [27, 28]);

    change('#hub-field-status', 'unavailable');
    assert.equal(w.document.querySelectorAll('.hub-field-row').length, 5);
    change('#hub-field-source', 'miot.cloud');
    assert.equal(w.document.querySelectorAll('.hub-field-row').length, 2);
    change('#hub-field-type', 'number');
    assert.equal(w.document.querySelectorAll('.hub-field-row').length, 1);
    w.renderHub({resources: [structuredClone(resource)], events: []});
    assert.equal(query('#hub-field-source').value, 'miot.cloud');
    assert.equal(query('#hub-field-status').value, 'unavailable');
    assert.equal(w.document.querySelectorAll('.hub-field-row').length, 1, 'Polling preserves all field filters');
    change('#hub-field-status', 'all');
    change('#hub-field-type', 'all');
    change('#hub-field-source', 'all');
    const search = query('#hub-field-search');
    search.value = '阳台温度';
    search.dispatchEvent(new w.Event('input', {bubbles: true}));
    assert.equal(w.document.querySelectorAll('.hub-field-row').length, 1);

    w.openHomeDevice('watch');
    await settle();
    assert.match(query('[data-property-key="health"]').textContent, /仅事件或动作.*由事件或动作返回/);
    assert.match(query('[data-property-key="count"]').textContent, /读取失败.*显示上次读数.*本次读取失败/);
    const partialDevice={...device,state:{temperature:22,'relative-humidity':50},observed_at:at,online:true,stale:false,properties:[{key:'temperature',label:'温度',unit:'℃',readable:true,read_status:{status:'unavailable',message:'温度本次未读到'}},{key:'relative-humidity',label:'湿度',unit:'%',readable:true,read_status:{status:'ok'}}]};
    const partialResource={resource_id:'home.watch.watch',name:'部分采样设备',kind:'sensor',state:partialDevice.state,observed_at:at,received_at:at,stale:false,online:true,fields:[{key:'temperature',path:['temperature'],label:'温度',type:'number',unit:'℃',readable:true,status:'unavailable'},{key:'relative-humidity',path:['relative-humidity'],label:'湿度',type:'number',unit:'%',readable:true,status:'ok'}]};
    w.renderHome({devices:[partialDevice],audit:[]});
    w.renderHub({resources:[partialResource],events:[]});
    assert.match(query('.home-device-card').textContent,/上次读数/,'Home cards label retained old values');
    assert.match(query('#home-detail-metrics .has-stale-field').textContent,/上次读数/);
    assert.match(query('#overview-environment').textContent,/湿度50%/);
    assert.doesNotMatch(query('#overview-environment').textContent,/温度22/,'Fresh humidity must not make stale temperature appear fresh in overview');
    const synthetic = {resource_id: 'home.watch.empty', name: '尚无读数设备', kind: 'service', state: {}, observed_at: null, received_at: null, expires_at: null, stale: true, online: false, fields: [{key: 'unknown', path: ['unknown'], label: '待确认字段', type: 'unknown', status: 'unknown', readable: true, source: 'miot.cloud'}]};
    w.renderHub({resources: [synthetic], events: []});
    await w.selectHubResource(synthetic.resource_id);
    assert.match(query('#hub-detail-time').textContent, /尚无有效读数/);
    assert.match(query('#hub-field-list').textContent, /类型待确认.*尚未取得读数/);
    assert.doesNotMatch(query('#view-data').textContent, /Invalid Date|NaN|1970/);
    console.log('PASS: complete mixed-source field dictionary; nested paths and literal dotted keys; zero/boolean values; unavailable/unsupported/event/stale diagnostics; sensitive values masked; source/type/status/search filters persist; custom numeric field history labels/units; no writes.');
  } finally {w.close();}
}
run().catch(error => {console.error(error); process.exitCode=1;});
