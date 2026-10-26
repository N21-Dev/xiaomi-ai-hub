// Synthetic inventory, signals and transport only; never connects to appliances.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {JSDOM} = require(process.env.HUB_JSDOM_MODULE || 'jsdom');
const fixture = require('./verify_infrared_ac_dom.cjs');
const root = path.resolve(__dirname, '../src/xiaomi_ai_bridge/web');
const parent = {...structuredClone(fixture.devices[0]), home_key:'test-home', room_key:'study', home_name:'测试家庭', room_name:'书房'};
const pending = {key:'ir-ac:infrared:pending', name:'空调', alias:'空调', category:'air-conditioner', category_label:'空调',
  model:'待选择空调协议', enabled:true, cloud_online:true, state:{}, online:false, stale:false, controls:[], properties:[],
  infrared_ac:{parent_key:'infrared', parent_name:'万能遥控器', profile_key:'', ready:false}, ac_profile:null};
module.exports = {parent, pending};
async function run() {
  const data = {...structuredClone(fixture.infrared), ac_profiles:[], learned_buttons:[]};
  const home = {devices:[structuredClone(parent)], infrared_devices:[structuredClone(pending)], audit:[]};
  const dom = new JSDOM(fs.readFileSync(path.join(root,'index.html'),'utf8'), {url:'http://synthetic.test/#home', runScripts:'outside-only', pretendToBeVisual:true});
  const w = dom.window, requests = [], writes = []; let stream;
  w.EventSource = class {constructor(){stream=this;} close(){}};
  w.confirm = () => true;
  w.fetch = async (url, options={}) => {
    requests.push({url, method:options.method || 'GET'});
    let body = {};
    if (url === '/api/home') body = home;
    else if (url === '/api/home/layout') body = {available:true, assignments:{infrared:{home_key:'test-home',room_key:'study',home_name:'测试家庭',room_name:'书房'}}, homes:[{key:'test-home',name:'测试家庭',rooms:[{key:'study',name:'书房'}]}]};
    else if (url === '/api/hub') body = {resources:[], events:[]};
    else if (url === '/api/status') body = {ready:true};
    else if (url.includes('/auth/')) body = {state:'idle'};
    else if (url.includes('/extensions')) body = data;
    else if (url.endsWith('/control')) {
      const input = JSON.parse(options.body); writes.push({url, input});
      const profile = data.ac_profiles.find(p => p.key === input.value.profile_key);
      if (input.action === 'ir_ac_send') Object.assign(profile.settings, input.value.settings);
      else profile.ai_control_enabled = input.value.enabled;
      body = {ok:true, outcome:'accepted', message:'合成发送回执，无状态回读', device_key:'infrared', extensions:data};
    }
    return {ok:true, status:200, json:async()=>structuredClone(body)};
  };
  const settle = () => new Promise(resolve=>setTimeout(resolve,40));
  const cards = () => [...w.document.querySelectorAll('.home-device-card')];
  const event = () => stream.onmessage({data:JSON.stringify({type:'home.extensions',device_key:'infrared',extensions:structuredClone(data)})});
  try {
    w.eval(fs.readFileSync(path.join(root,'app.js'),'utf8')); await settle();
    assert.equal(cards().length,2);
    assert.equal(w.document.querySelectorAll('#home-management-list [data-infrared-ac-device]').length,1);
    const setup = cards().find(card=>card.dataset.deviceKey===pending.key);
    assert.match(setup.textContent,/空调.*待配置/);
    assert.match(setup.textContent,/书房/);
    setup.querySelector('.home-device-open').click(); await settle();
    assert.equal(w.document.querySelector('#home-detail-name').textContent,'空调');
    assert.match(w.document.querySelector('#home-extensions').textContent,/尚未选择协议/);
    assert.equal(writes.length,0);
    assert.ok(requests.some(r=>r.url==='/api/home/devices/infrared/extensions'));
    assert.ok(!requests.some(r=>r.url.includes('/devices/ir-ac')));
    w.document.querySelector('[data-ac-parent]').click(); await settle();
    assert.equal(w.document.querySelector('#home-detail-name').textContent,'万能遥控器');
    w.openHomeDevice(pending.key); await settle();
    data.ac_profiles = [structuredClone(fixture.infrared.ac_profiles[0]), {...structuredClone(fixture.infrared.ac_profiles[0]), key:'second-ac', name:'卧室空调'}];
    event(); await settle();
    assert.equal(cards().length,3);
    assert.equal(w.document.querySelectorAll('.ir-ac-panel').length,1,'Learning transitions selected setup entry into its own AC');
    assert.equal(w.document.querySelector('#home-detail-name').textContent,'书房空调');
    assert.equal(w.document.querySelector('#home-detail-status').classList.contains('bad'),false);
    w.document.querySelector('[data-home-category="air-conditioner"]').click();
    assert.equal(cards().length,2);
    assert.equal(w.document.querySelectorAll('#home-management-list .device-row').length,3);
    assert.equal(w.document.querySelectorAll('#home-management-list [data-infrared-ac-device]').length,2);
    assert.equal(w.document.querySelector('#home-test-device').options.length,1,'Physical-device tools exclude view aliases');
    const bedroom = cards().find(card=>card.dataset.deviceKey==='ir-ac:infrared:second-ac');
    bedroom.querySelector('.home-device-open').click(); await settle();
    assert.equal(w.document.querySelectorAll('.ir-ac-panel').length,1);
    assert.equal(w.document.querySelector('.ir-ac-panel').dataset.acProfile,'second-ac');
    assert.equal(w.document.querySelector('.ir-learning-status'),null,'Learning UI belongs to associated gateway');
    const temperature = w.document.querySelector('[data-ac-field="temperature"]');
    temperature.value='27'; temperature.dispatchEvent(new w.Event('input',{bubbles:true}));
    await new Promise(resolve=>setTimeout(resolve,520));
    assert.equal(writes.length,1);
    assert.equal(writes[0].url,'/api/home/devices/infrared/control');
    assert.equal(writes[0].input.value.profile_key,'second-ac');
    assert.equal(writes[0].input.value.settings.temperature,27);
    await w.runHomeControl('ir-ac:infrared:second-ac','ir_ac_send',{profile_key:'synthetic-ac',settings:{power:true}});
    assert.equal(writes.length,1,'Cannot address another profile through this AC view');
    data.ac_profiles=[]; event(); await settle();
    assert.equal(cards().length,1,'AC category keeps a setup entry after deleting the source');
    assert.equal(cards()[0].dataset.deviceKey,pending.key);
    assert.equal(w.document.querySelectorAll('#home-management-list [data-infrared-ac-device]').length,1);
    assert.match(w.document.querySelector('#home-management-list [data-infrared-ac-device]').textContent,/待配置/);
    assert.equal(w.document.querySelectorAll('.ir-ac-panel').length,0);
    assert.match(w.document.querySelector('#home-extensions').textContent,/尚未选择协议/);
    // Independent protocol configuration works with zero learned buttons.
    data.learned_buttons=[];
    data.ac_profiles=[{...structuredClone(fixture.infrared.ac_profiles[0]),key:'protocol-fixture',name:'空调',
      source_kind:'protocol',settings_source:'protocol_defaults',requires_learning:false}];
    event(); await settle();
    assert.equal(cards().length,1);
    assert.doesNotMatch(cards()[0].textContent,/待配置|学习信号温度/);
    assert.match(cards()[0].textContent,/默认目标温度/);
    cards()[0].querySelector('.home-device-open').click(); await settle();
    assert.equal(w.document.querySelectorAll('.ir-ac-panel').length,1);
    assert.match(w.document.querySelector('.ir-ac-panel').textContent,/无需学习或绑定按键/);
    assert.match(w.document.querySelector('.ir-ac-last-command').textContent,/显示协议默认设置/);
    assert.equal(w.document.querySelector('.ir-ac-panel button[type="submit"]').disabled,false);
    const fan = w.document.querySelector('[data-ac-field="fan"]');
    fan.value='3'; fan.dispatchEvent(new w.Event('input',{bubbles:true}));
    await new Promise(resolve=>setTimeout(resolve,520));
    assert.equal(writes.length,2);
    assert.equal(writes[1].input.value.profile_key,'protocol-fixture');
    home.devices[0].enabled=false; w.renderHome(structuredClone(home));
    assert.match(cards()[0].textContent,/未授权/);
    assert.equal(writes.length,2);
    console.log('PASS: Independent AC inventory, pending binding, room/category filters, SSE add/delete, single-profile details, gateway routing, profile isolation, inherited authorization, no implicit writes.');
  } finally {w.close();}
}
if(require.main===module) run().catch(error=>{console.error(error);process.exitCode=1;});
