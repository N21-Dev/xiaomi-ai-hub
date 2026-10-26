// Router-local UI and mixed-provider history regression; no real devices contacted.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {JSDOM} = require(process.env.HUB_JSDOM_MODULE || 'jsdom');
const root = path.resolve(__dirname,'../src/xiaomi_ai_bridge/web');
const now = Date.now();
const at = offset => new Date(now+offset).toISOString();
const f = (key,label,unit='',extra={}) => ({key:`local.${key}`,path:[`local.${key}`],label,unit,source:'xiaomi.router.local',type:'number',role:'measurement',priority:'primary',readable:true,writable:false,chartable:true,refresh_policy:'每60秒更新',status:'ok',observed_at:at(-60000),...extra});
const fields = [f('cpu-percent','CPU 使用率','%'),f('memory-percent','内存使用率','%'),f('wan-download-bytes-per-second','WAN 下载速率','B/s',{status:'unsupported',message:'有线中继模式不提供 WAN 速率'}),f('wan-upload-bytes-per-second','WAN 上传速率','B/s',{status:'unsupported',message:'有线中继模式不提供 WAN 速率'}),f('connected-device-count','本机连接终端','台',{role:'counter'}),f('system-temperature','系统温度','℃',{status:'unsupported',message:'固件未提供有效温度'}),f('uptime-seconds','运行时长','秒',{role:'counter'}),f('hardware-model','硬件型号','',{role:'identity',priority:'detail',type:'string',chartable:false})];
const local={kind:'router',source:'xiaomi.router.local',available:true,identity_verified:true,message:'本地状态读取正常；当前为有线中继',observed_at:at(-60000),stale:false,state:{'local.cpu-percent':12.5,'local.memory-percent':47,'local.connected-device-count':0,'local.uptime-seconds':90061,'local.hardware-model':'RA67'},fields};
const device={key:'router',name:'RouterX',alias:'RouterX',model:'xiaomi.router.ra67',category:'router',enabled:true,cloud_online:false,online:true,stale:false,observed_at:at(-60000),state:{},properties:[],controls:[],local_source:local};
const resource={resource_id:'home.router.router',name:'RouterX',kind:'service',state:local.state,fields,observed_at:at(-60000),received_at:at(-60000),online:true,stale:false};
const cpuMetric={key:'local.cpu-percent',path:['local.cpu-percent'],aliases:['local.cpu-percent'],label:'CPU 使用率',unit:'%'};
const events=[
  {observed_at:at(-90000),state:{'local.cpu-percent':10},fields:[{...fields[0],observed_at:at(-120000)}]},
  {observed_at:at(-80000),state:{'local.cpu-percent':10},fields:[{...fields[0],observed_at:at(-120000),cached:true}]},
  {observed_at:at(-70000),state:{'local.cpu-percent':10},fields:[{...fields[0],observed_at:at(-120000)}]},
  {observed_at:at(-50000),state:{'local.cpu-percent':12.5},fields:[{...fields[0],observed_at:at(-60000)}]},
];
const settle=()=>new Promise(resolve=>setTimeout(resolve,25));
async function run(){
  const dom=new JSDOM(fs.readFileSync(path.join(root,'index.html'),'utf8'),{url:'http://hub.test/#home',runScripts:'outside-only',pretendToBeVisual:true});
  const w=dom.window;
  w.EventSource=class{close(){}};
  w.fetch=async(url,options={})=>{
    assert.ok(!options.method||options.method==='GET','Router local UI is read-only');
    let data={};
    if(url==='/api/home')data={devices:[device],audit:[]};
    else if(url==='/api/hub')data={resources:[resource],events:[]};
    else if(url==='/api/status')data={ready:true,started_at:at(-100000)};
    else if(url.includes('/history'))data={events};
    else if(url.includes('/auth/'))data={state:'idle'};
    else if(url.endsWith('/state'))data={state:resource.state,observed_at:resource.observed_at};
    return{ok:true,status:200,json:async()=>structuredClone(data)};
  };
  const query=selector=>w.document.querySelector(selector);
  try{
    w.eval(fs.readFileSync(path.join(root,'app.js'),'utf8'));await settle();
    assert.equal(w.document.querySelectorAll('.home-device-card').length,1);
    assert.equal(w.document.querySelectorAll('[data-router-metric]').length,3);
    assert.match(query('[data-router-metric="local.cpu-percent"]').textContent,/12.5 %/);
    assert.match(query('[data-router-metric="local.connected-device-count"]').textContent,/本机连接终端0 台/);
    assert.equal(query('[data-router-metric="local.wan-download-bytes-per-second"]'),null,'Inapplicable WAN does not crowd the glance card');
    assert.doesNotMatch(query('.home-device-card').textContent,/0 ?℃|RA67|stok|MAC/);
    w.openHomeDevice('router');await settle();
    assert.match(query('#home-detail-metrics [data-layer-field="local.wan-download-bytes-per-second"]').textContent,/有线中继模式不提供/);
    assert.match(query('#home-local-source').textContent,/路由器本地 · 只读/);
    assert.match(query('#home-detail-status').textContent,/本地在线.*米家云离线/);
    assert.equal(query('#home-detail-status').classList.contains('bad'),false);
    assert.equal(query('#home-detail-metrics [data-layer-field="local.system-temperature"]'),null);
    assert.match(query('#home-detail-metrics').textContent,/1天 1小时/);
    assert.equal(query('#home-local-source').querySelectorAll('button,input,select').length,0,'Local source adds no router settings controls');
    assert.equal(w.document.querySelectorAll('.hub-resource-card').length,1,'Local source merges into existing router resource');
    assert.equal(query('#hub-detail').hidden,true);
    await w.selectHubResource(resource.resource_id);
    assert.equal(query('#hub-identity-section').open,false);
    assert.match(query('#hub-identity-grid').textContent,/RA67/);
    const points=w.historyMetricPoints(events,cpuMetric);
    assert.equal(points.length,2,'Cached and repeated provider carryover never add points');
    assert.deepEqual(Array.from(points,point=>point.at),[now-120000,now-60000],'Source-field actual observation times are used');
    assert.equal(w.document.querySelectorAll('#hub-chart circle').length,2);
    const legacy=w.historyMetricPoints([{observed_at:at(-30000),state:{'local.cpu-percent':20}}],cpuMetric);
    assert.equal(legacy[0].at,now-30000,'Only legacy events without field metadata use event time');
    const missingTime=w.historyMetricPoints([{observed_at:at(-30000),state:{'local.cpu-percent':20},fields:[{key:'local.cpu-percent',path:['local.cpu-percent'],status:'ok'}]}],cpuMetric);
    assert.equal(missingTime.length,0);
    local.state['local.wan-download-bytes-per-second']=1250000;
    local.state['local.wan-upload-bytes-per-second']=0;
    fields[2].status='ok';fields[3].status='ok';
    w.renderHome({devices:[structuredClone(device)],audit:[]});
    assert.match(query('[data-router-metric="local.wan-download-bytes-per-second"]').textContent,/1.25 MB\/s/);
    assert.match(query('#home-detail-metrics [data-layer-field="local.wan-upload-bytes-per-second"]').textContent,/0B\/s/);
    assert.equal(w.metricPresentation({unit:'bps'},1250000).unit,'Mbps','Bit and byte rates are never conflated');
    local.available=false;local.stale=true;local.message='本地接口暂不可用，保留上次读数';
    w.renderHome({devices:[structuredClone(device)],audit:[]});
    assert.match(query('[data-router-metric="local.cpu-percent"]').textContent,/上次读数/);
    assert.match(query('#home-local-source').textContent,/读数待更新.*保留上次读数/);
    console.log('PASS: one router resource; CPU/memory/rates/client widgets; AP-mode unavailable WAN; zero clients/rates valid; optional temperature absent; readonly local badge; identity folded; correct byte/bit rate units; stale cache; per-field history timestamps and cached/duplicate dedup; no writes.');
  }finally{w.close();}
}
run().catch(error=>{console.error(error);process.exitCode=1;});
