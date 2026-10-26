// Synthetic devices and transports only. No credentials or real IR commands.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {JSDOM} = require(process.env.HUB_JSDOM_MODULE || 'jsdom');
const fixture = require('./verify_infrared_ac_dom.cjs');

async function run(root = path.resolve(__dirname, '../src/xiaomi_ai_bridge/web')) {
  const data = structuredClone(fixture.infrared), home = structuredClone(fixture.devices), writes = [];
  data.controls.find(control => control.action === 'ir_ac_send').requires_confirmation = false;
  data.ac_profiles.push({...structuredClone(data.ac_profiles[0]), key:'second-ac', name:'卧室空调'});
  const dom = new JSDOM(fs.readFileSync(path.join(root,'index.html'),'utf8'), {
    url:'http://synthetic.test/#home', runScripts:'outside-only', pretendToBeVisual:true,
  });
  const w = dom.window;
  let stream, fail = false, confirmations = 0, release;
  w.EventSource = class {constructor(){stream=this;} close(){}};
  w.confirm = () => {confirmations++; return false;};
  w.fetch = async (url, options={}) => {
    let body = {}, ok = true;
    if (url === '/api/home') body = {devices:home, audit:[]};
    else if (url === '/api/hub') body = {resources:[], events:[]};
    else if (url === '/api/status') body = {ready:true};
    else if (url.includes('/auth/')) body = {state:'idle'};
    else if (url.includes('/extensions')) body = data;
    else if (url.endsWith('/control')) {
      const input = JSON.parse(options.body); writes.push(input);
      if (release) await new Promise(resolve => {release = resolve;});
      const profile = data.ac_profiles.find(item => item.key === input.value.profile_key);
      if (fail) {ok=false; body={ok:false, outcome:'unconfirmed', message:'合成超时'};}
      else {
        if (input.action === 'ir_ac_send') {
          for (const key of ['temperature','fan','swing_vertical']) {
            if (key in input.value.settings) assert.equal(Number.isInteger(input.value.settings[key]), true);
          }
          Object.assign(profile.settings,input.value.settings);
          profile.settings_source='last_sent'; profile.last_attempt={outcome:'accepted'};
        } else profile.ai_control_enabled=input.value.enabled;
        body={ok:true, outcome:input.action === 'ir_ac_send' ? 'accepted' : 'confirmed', extensions:data};
      }
    }
    return {ok, status:ok ? 200 : 502, json:async()=>structuredClone(body)};
  };
  const wait = (ms=40) => new Promise(resolve=>setTimeout(resolve,ms));
  const card = (key='synthetic-ac') => w.document.querySelector(`[data-ac-profile="${key}"]`);
  const field = (key, profile='synthetic-ac') => card(profile).querySelector(`[data-ac-field="${key}"]`);
  const edit = (key,value,profile='synthetic-ac') => {
    const input=field(key,profile);
    if (input.type==='checkbox') input.checked=value; else input.value=value;
    input.dispatchEvent(new w.Event('input',{bubbles:true}));
  };
  const event = () => stream.onmessage({data:JSON.stringify({type:'home.extensions', device_key:'infrared', extensions:structuredClone(data)})});
  try {
    w.eval(fs.readFileSync(path.join(root,'app.js'),'utf8'));
    await wait(); w.openHomeDevice('infrared'); await wait();
    assert.equal(writes.length,0,'Rendering/loading never emits');
    assert.equal(card().querySelector('button[type="submit"]').hidden,true,'No apply button in normal operation');
    assert.match(card().textContent,/自动发送/);
    assert.match(card().textContent,/不是空调当前状态/);
    edit('fan','1'); await wait(100); edit('fan','2'); await wait(100); edit('fan','3');
    await wait(250); assert.equal(writes.length,0,'Wait for the final edit');
    await wait(260);
    assert.equal(writes.length,1,'Rapid changes coalesce into one request');
    assert.deepEqual(writes[0].value.settings,{fan:3,turbo:false,quiet:false});
    assert.equal(writes[0].confirmed,undefined);
    assert.equal(confirmations,0);
    assert.equal(field('fan').value,'3');
    event(); await wait(); assert.equal(field('fan').value,'3','Saved speed persists across SSE');

    let before=writes.length;
    for (let i=0;i<3;i++) {
      card().querySelector('[aria-label="提高目标温度"]').click();
      assert.equal(writes.length,before+i+1,'Each temperature button click submits during its event handler');
      assert.equal(writes.at(-1).value.settings.temperature,25+i);
      assert.equal(card().querySelector('[aria-label="提高目标温度"]').disabled,true);
      await wait();
    }
    edit('light',false); edit('swing_vertical','9'); edit('swing_horizontal','false');
    await wait(520);
    assert.equal(writes.length,before+4);
    assert.equal('temperature' in writes.at(-1).value.settings,false,'Unchanged temperature is not submitted again');
    assert.equal(writes.at(-1).value.settings.light,false);
    assert.equal(writes.at(-1).value.settings.swing_vertical,9);
    assert.equal(writes.at(-1).value.settings.swing_horizontal,false);
    assert.equal(field('temperature').value,'27');
    card().querySelector('[aria-label="降低目标温度"]').click();
    assert.equal(writes.at(-1).value.settings.temperature,26,'Minus submits immediately too');
    await wait();

    edit('temperature',30); await wait(500);
    before=writes.length;
    card().querySelector('[aria-label="提高目标温度"]').click();
    await wait(500);
    assert.equal(writes.length,before,'Plus at the upper bound does not duplicate an unchanged command');

    for (const speed of [0,1,2,3,4,5]) {
      edit(speed%2 ? 'turbo' : 'quiet',true); edit('fan',String(speed));
      assert.equal(field('turbo').checked,false); assert.equal(field('quiet').checked,false);
      await wait(500); assert.equal(writes.at(-1).value.settings.fan,speed);
      assert.equal(field('fan').value,String(speed));
    }
    edit('turbo',true); edit('quiet',true); await wait(500);
    assert.equal(writes.at(-1).value.settings.quiet,true);
    assert.equal(writes.at(-1).value.settings.turbo,false);
    edit('mode','dry'); await wait(500);
    assert.equal(writes.at(-1).value.settings.temperature,25);
    assert.equal(field('temperature').disabled,true);
    edit('mode','heat'); await wait(500);
    assert.equal(writes.at(-1).value.settings.xfan,false);
    edit('mode','cool'); await wait(500);

    before=writes.length;
    card().querySelector('.ir-ac-off').click();
    assert.equal(writes.length,before+1,'Power buttons also submit immediately');
    await wait(500);
    assert.deepEqual(writes.at(-1).value.settings,{power:false});
    assert.equal(writes.length,before+1);
    [...card().querySelectorAll('.ir-ac-quick button')].find(button=>button.textContent==='开机').click();
    await wait(500); assert.deepEqual(writes.at(-1).value.settings,{power:true});

    before=writes.length;
    fail=true; edit('fan','3'); await wait(520);
    assert.equal(writes.length,before+1); assert.equal(field('fan').value,'3');
    assert.equal(card().querySelector('button[type="submit"]').hidden,false);
    await wait(650); assert.equal(writes.length,before+1,'Uncertain sends never auto retry');
    fail=false; card().querySelector('button[type="submit"]').click(); await wait(90);
    assert.equal(writes.length,before+2); assert.equal(field('fan').value,'3');
    assert.equal(card().querySelector('button[type="submit"]').hidden,true);

    before=writes.length;
    edit('temperature',28,'second-ac'); edit('fan','4');
    card().querySelector('[data-ac-discard]').click(); await wait(520);
    assert.equal(writes.length,before+1,'Discard cancels only this profile');
    assert.equal(writes.at(-1).value.profile_key,'second-ac');
    assert.equal(field('fan').value,'3','Discard restores saved setting without transmitting');

    before=writes.length;
    edit('fan','1'); w.showView('data'); await wait(520);
    assert.equal(writes.length,before,'Leaving the view cancels unsent commands');
    w.showView('home'); w.renderHomeDetail();
    assert.equal(field('fan').value,'1');
    assert.equal(card().querySelector('button[type="submit"]').hidden,false,'Retained edits can be retried explicitly');
    card().querySelector('[data-ac-discard]').click();
    edit('fan','2'); w.openHomeDevice('missing'); await wait(520);
    assert.equal(writes.length,before,'Switching devices cancels unsent commands');
    w.openHomeDevice('infrared'); await wait(); card().querySelector('[data-ac-discard]').click();

    before=writes.length;
    edit('fan','1'); home[0].cloud_online=false;
    w.renderHome({devices:structuredClone(home),audit:[]}); await wait(520);
    assert.equal(writes.length,before,'Offline gating is rechecked at send time');
    assert.equal(field('fan').disabled,true);
    home[0].cloud_online=true; w.renderHome({devices:structuredClone(home),audit:[]});
    card().querySelector('[data-ac-discard]').click();
    edit('temperature',28); edit('temperature',''); await wait(520);
    assert.equal(writes.length,before,'Invalid input cancels a previously scheduled valid command');
    card().querySelector('[data-ac-discard]').click();

    release=true; edit('fan','4'); await wait(500);
    assert.equal(field('fan').disabled,true,'Single in-flight request prevents concurrent writes');
    assert.match(card().querySelector('.ir-ac-auto-status').textContent,/正在发送/);
    const resume=release; release=null; resume(); await wait(90);
    assert.equal(field('fan').value,'4'); assert.equal(field('fan').disabled,false);
    before=writes.length;
    const permission=card().querySelector('[data-ac-ai-permission]');
    permission.checked=true; permission.dispatchEvent(new w.Event('input',{bubbles:true}));
    await wait(520); assert.equal(writes.length,before,'AI permission still requires explicit save');
    card().querySelector('[data-extension-command="ir_ac_ai_permission"]').click(); await wait(70);
    assert.equal(writes.at(-1).action,'ir_ac_ai_permission');
    assert.equal(confirmations,0);
    console.log('PASS: immediate temperature/power button submission, range boundary, input coalescing, partial settings, all fan speeds, exclusive modes, failure/retry, profile isolation, cancellation, offline/invalid gating, pending lock, SSE display, explicit AI permission; no real writes.');
  } finally {w.cancelAcAdjustments(); w.close();}
}
module.exports={run};
if (require.main===module) run().catch(error=>{console.error(error); process.exitCode=1;});
