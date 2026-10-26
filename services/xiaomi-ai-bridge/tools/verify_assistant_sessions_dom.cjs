// Synthetic requests only: never connects to a service, model, or device.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {JSDOM} = require(process.env.HUB_JSDOM_MODULE || 'jsdom');
const root = path.resolve(__dirname, '../src/xiaomi_ai_bridge/web');
const html = fs.readFileSync(path.join(root, 'index.html'), 'utf8');
const source = fs.readFileSync(path.join(root, 'app.js'), 'utf8');
const settle = () => new Promise(resolve => setTimeout(resolve, 25));

function terminal() {
  const dom = new JSDOM(html, {url:'http://hub.test/#assistant', runScripts:'outside-only', pretendToBeVisual:true});
  const window = dom.window;
  const state = {calls:[], hold:false, failClear:false, release:null};
  window.EventSource = class {close(){}};
  window.matchMedia = () => ({matches:true, addEventListener(){}, removeEventListener(){}});
  window.HTMLElement.prototype.scrollIntoView = function(){};
  window.fetch = async (url, options={}) => {
    let payload = {};
    const name = String(url);
    if (options.method && options.method !== 'GET') {
      const body = JSON.parse(options.body || '{}');
      state.calls.push({url:name, body});
      if (name === '/api/actions/chat') {
        if (state.hold) await new Promise(resolve => {state.release = resolve;});
        payload = {ok:true, answer:state.hold ? 'STALE-ANSWER-MUST-NOT-APPEAR' : 'synthetic reply', conversation_id:body.conversation_id};
      } else if (name === '/api/actions/clear-context' && state.failClear) {
        return {ok:false, status:502, json:async()=>({error:'synthetic clear failure'})};
      } else payload = {ok:true};
    } else if (name === '/api/hub') payload = {resources:[],events:[]};
    else if (name === '/api/home') payload = {devices:[],audit:[]};
    else if (name === '/api/models') payload = {providers:[],roles:{},active:{},gateway:{auth_required:false}};
    else if (name === '/api/automations') payload = {tasks:[]};
    else if (name.includes('/auth/')) payload = {state:'idle'};
    return {ok:true,status:200,json:async()=>structuredClone(payload)};
  };
  window.eval(source);
  state.window = window;
  state.send = async text => {
    window.document.querySelector('#chat-prompt').value = text;
    window.document.querySelector('#chat-form').dispatchEvent(new window.Event('submit',{bubbles:true,cancelable:true}));
    await settle();
  };
  state.clear = async () => {window.document.querySelector('#clear-chat').click(); await settle();};
  state.chatCalls = () => state.calls.filter(call=>call.url==='/api/actions/chat');
  return state;
}

(async()=>{
  const a = terminal(), b = terminal();
  try {
    await settle();
    await a.send('terminal A first');
    await b.send('terminal B first');
    const aid = a.chatCalls()[0].body.conversation_id;
    const bid = b.chatCalls()[0].body.conversation_id;
    assert.match(aid,/^[A-Za-z0-9_-]{16,128}$/);
    assert.notEqual(aid,bid,'Different terminals must have different conversation IDs');
    await a.send('terminal A second');
    assert.equal(a.chatCalls()[1].body.conversation_id,aid);
    await b.clear();
    assert.equal(b.calls.find(call=>call.url==='/api/actions/clear-context').body.conversation_id,bid);
    await b.send('terminal B after clear');
    assert.notEqual(b.chatCalls()[1].body.conversation_id,bid);
    assert.equal(a.chatCalls()[1].body.conversation_id,aid);

    b.failClear = true;
    const before = b.chatCalls().at(-1).body.conversation_id;
    await b.clear();
    assert.match(b.window.document.querySelector('#chat-log').textContent,/terminal B after clear/);
    assert.match(b.window.document.querySelector('#chat-log').textContent,/清空失败/);
    await b.send('after failed clear');
    assert.equal(b.chatCalls().at(-1).body.conversation_id,before);

    a.hold = true;
    await a.send('old in-flight question');
    assert.ok(a.release);
    await a.clear();
    a.release();
    await settle();
    assert.doesNotMatch(a.window.document.querySelector('#chat-log').textContent,/STALE-ANSWER/);

    a.window.renderAutomations([{id:'auto_synthetic',status:'unknown',target:{device:'synthetic AC'},action:'set_settings',
      schedule:{type:'once',at:'2026-10-17 09:00'},last_outcome:'unconfirmed',last_message:'synthetic interrupted send'}]);
    assert.match(a.window.document.querySelector('#automation-list').textContent,/结果待核对|结果未确认/);
    a.window.renderAutomations([{id:'auto_synthetic',status:'pending',target:{device:'synthetic AC'},action:'set_settings',
      schedule:{type:'daily',at:'09:00'},last_outcome:'accepted'}]);
    assert.match(a.window.document.querySelector('#automation-list').textContent,/设备状态未确认/);
    assert.doesNotMatch(a.window.document.querySelector('#automation-list').textContent,/已回读确认/);
    console.log('PASS: terminal isolation, clear isolation, clear failure, late reply suppression, and truthful automation outcomes.');
  } finally {a.window.close();b.window.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
