// DOM-only regression checks; no browser automation and no device requests.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { JSDOM } = require(process.env.HUB_JSDOM_MODULE || 'jsdom');
const root = path.resolve(__dirname, '../src/xiaomi_ai_bridge/web');
const html = fs.readFileSync(path.join(root, 'index.html'), 'utf8');
const script = fs.readFileSync(path.join(root, 'app.js'), 'utf8');
const now = Date.now();
const at = minutes => new Date(now - minutes * 60000).toISOString();
const resource = {
  resource_id: 'home.air-purifier.fixture', name: '空气净化器', kind: 'sensor',
  observed_at: at(0), received_at: at(0), expires_at: new Date(now + 180000).toISOString(),
  stale: false, online: true, state: {temperature: 27, 'relative-humidity': 55, 'pm2.5-density': 0, on: '开启', mode: 'Favorite'},
};
const snapshot = {resources:[resource], events:[], sampling:{interval_seconds:60,last_sample_at:at(0)}};
const events = [
  {observed_at:at(6), state:{temperature:25}},
  {observed_at:at(5), state:{temperature:26}},
  {observed_at:at(4), state:{}},
  {observed_at:at(3), state:{temperature:27}},
  {observed_at:at(2), state:{temperature:27}},
];
const settle = () => new Promise(resolve => setTimeout(resolve, 20));

async function run() {
  const dom = new JSDOM(html, {url:'http://hub.test/#data', runScripts:'outside-only', pretendToBeVisual:true});
  const w = dom.window;
  const sources=[];
  let historyReads=0;
  w.EventSource=class { constructor(){sources.push(this)} close(){} };
  w.fetch=async target => {
    const url=String(target);
    let data={};
    if (url==='/api/hub') data=snapshot;
    else if (url.includes('/history')) {historyReads++; data={events};}
    else if (url==='/api/status') data={started_at:at(0), ready:true};
    else if (url==='/api/home') data={devices:[], audit:[]};
    else if (url.includes('/auth/')) data={state:'idle'};
    return {ok:true,status:200,json:async()=>structuredClone(data)};
  };
  try {
    w.eval(script);
    await settle();
    assert.equal(w.document.querySelector('#overview-online-count').textContent, '1');
    assert.equal(w.document.querySelector('#overview-stale-count').textContent, '0');
    assert.equal(w.document.querySelector('#overview-environment-count').textContent, '1');
    assert.match(w.document.querySelector('#overview-data-note').textContent, /1 个数据源/);
    assert.equal(w.document.querySelector('#hub-detail').hidden,true,'Details wait for an explicit selection');
    await w.selectHubResource(resource.resource_id);
    const card=w.document.querySelector('.hub-resource-card');
    assert.ok(card);
    const text=card.textContent;
    assert.match(text,/温度27℃/);
    assert.match(text,/湿度55%/);
    assert.match(text,/PM2.50μg\/m³/); // Zero is a valid reading, not missing data.
    assert.match(text,/最爱模式/);
    assert.doesNotMatch(text,/relative-humidity|Favorite|\{"/);
    assert.ok(w.document.querySelector('#overview-environment .metric-card'));
    assert.equal(w.document.querySelectorAll('#hub-chart circle').length,4);
    assert.equal((w.document.querySelector('#hub-chart path').getAttribute('d').match(/M/g)||[]).length,2);
    assert.equal(w.document.querySelector('#hub-detail').hidden,false);
    const oldReads=historyReads;
    w.renderHub(structuredClone(snapshot));
    await settle();
    assert.equal(historyReads,oldReads,'Same sample should not reload history');
    const newer={...resource,received_at:new Date(now+1000).toISOString(),state:{temperature:28,'relative-humidity':56}};
    sources[0].onmessage({data:JSON.stringify({type:'hub.update',resource:newer})});
    await settle();
    assert.match(w.document.querySelector('.hub-resource-card').textContent,/温度28℃/);
    assert.doesNotMatch(w.document.querySelector('.hub-resource-card').textContent,/PM2.5/,'Missing metric must not reuse old value');
    w.renderHub({...snapshot,resources:[{...resource,stale:true,online:false}]});
    assert.ok(w.document.querySelector('.hub-resource-card.is-stale'));
    assert.match(w.document.querySelector('#hub-detail-state-pill').textContent,/过期/);
    const metric=w.document.querySelector('#hub-history-metric');
    metric.value='humidity';metric.dispatchEvent(new w.Event('change'));
    assert.match(w.document.querySelector('#hub-chart').textContent,/暂无|尚无/);
    w.renderHub({...snapshot,resources:[]});
    assert.equal(w.document.querySelector('#hub-detail').hidden,true);
    assert.equal(w.document.querySelectorAll('.hub-resource-card').length,0);
    console.log('PASS: Chinese metric cards, zero/missing values, overview consistency, real chart gaps, live update, stale state, history deduplication, removed-resource detail.');
  } finally {w.close();}
}

async function checkDeployedMarkup(base) {
  const [page, code, state] = await Promise.all([
    fetch(base + '/').then(r=>r.text()),
    fetch(base + '/app.js').then(r=>r.text()),
    fetch(base + '/api/hub').then(r=>r.json()),
  ]);
  const purifier = state.resources.find(r=>r.resource_id.startsWith('home.air-purifier.'));
  assert.ok(purifier, 'Real purifier sample must exist');
  const dom = new JSDOM(page,{url:base+'/#data',runScripts:'outside-only',pretendToBeVisual:true});
  const w=dom.window;
  // Exercise the deployed view against real read-only HTTP responses in a DOM.
  w.EventSource=class { close(){} };
  const closeHarness=require('./readonly_dom_harness.cjs')(w,base);
  try {
    w.eval(code);
    const deadline=Date.now()+10000;
    while(!w.document.querySelector('.hub-resource-card') && Date.now()<deadline) await settle();
    await w.selectHubResource(purifier.resource_id);
    const card=[...w.document.querySelectorAll('.hub-resource-card')].find(el=>el.dataset.resourceId===purifier.resource_id);
    assert.ok(card);
    assert.match(card.textContent,/温度.*℃/);
    assert.match(card.textContent,/湿度.*%/);
    assert.match(card.textContent,/PM2.5.*μg\/m³/);
    assert.doesNotMatch(card.textContent,/relative-humidity|Favorite|\{"/);
    assert.ok(w.document.querySelector('#hub-chart svg'));
    assert.ok(w.document.querySelector('#overview-environment .metric-card'));
    assert.notEqual(w.document.querySelector('#overview-online-count').textContent, '—');
    assert.notEqual(w.document.querySelector('#overview-last-sample').textContent, '—');
    assert.match(w.document.querySelector('#overview-data-note').textContent, /数据源/);
    console.log('PASS: deployed HTML and JavaScript render actual NAS purifier readings as Chinese cards, with overview and history chart.');
  } finally {await closeHarness();}
}
run().then(()=>process.argv[2] ? checkDeployedMarkup(process.argv[2]) : null).catch(error=>{console.error(error);process.exitCode=1;});
