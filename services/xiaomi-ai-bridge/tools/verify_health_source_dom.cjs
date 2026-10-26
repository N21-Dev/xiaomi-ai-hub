// Dedicated health-view checks. All data and commands are mocked by default.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {JSDOM} = require(process.env.HUB_JSDOM_MODULE || 'jsdom');
const root = path.resolve(__dirname, '../src/xiaomi_ai_bridge/web');
const at = '2026-09-27T04:30:00+08:00';
const field = (key, label, unit='', value) => ({key, path:[key], label, unit, value, type:typeof value==='boolean'?'boolean':'number', readable:true, source:'mi-health-hub', status:value==null?'unavailable':'ok', message:value==null?'健康中心没有该字段记录':'已读取'});
const resource = (kind, name, values) => ({resource_id:`health.${kind}`, kind:'health', name, date:'2026-09-26', synced_at:1790440000, available:true, state:Object.fromEntries(values.filter(item=>item[3]!=null).map(item=>[item[0],item[3]])), fields:values.map(item=>field(...item))});
const fixture = (date='2026-09-26', days=7) => ({service:{name:'健康中心',source:'mi-health-hub',web_url:'http://nas.test:8321/',read_only:true}, available:true,stale:false,message:'从健康中心读取账号记录',fetched_at:at,checked_at:at,selected_date:date,latest_data_date:'2026-09-26',days,start_date:'2026-09-20',end_date:date,
  resources:[
    resource('activity','每日活动',[['steps','步数','步',0],['distance_m','距离','米',4200],['calories','活动消耗','kcal',321],['active_min','活动时长','分钟',70],['standing_count','站立次数','次',10],['vigorous_min','较高强度活动','分钟',null]]),
    resource('sleep','睡眠记录',[['total_min','睡眠时长','分钟',450],['score','睡眠评分','分',83],['efficiency_percent','睡眠效率','%',90],['deep_min','深睡','分钟',80],['light_min','浅睡','分钟',280],['rem_min','REM 睡眠','分钟',90],['awake_min','清醒','分钟',20],['nap_min','小睡','分钟',null],['start_time','入睡时间','',1790390000],['end_time','起床时间','',1790418000]]),
    resource('heart','心率与监测',[['resting_hr','静息心率','次/分',57],['average_hr','平均心率','次/分',68],['min_spo2','最低血氧','%',96],['stress_average','平均压力','',null]]),
    resource('training','运动汇总',[['count','运动次数','次',1],['duration_min','运动时长','分钟',35]]),
  ],
  series:[{metric:'steps',label:'步数',unit:'步',status:'ok',points:[{date:'2026-09-20',value:1000},{date:'2026-09-22',value:2000},{date:'2026-09-26',value:0}]}, {metric:'sleep_total',label:'睡眠时长',unit:'分钟',status:'ok',points:[{date:'2026-09-25',value:null},{date:'2026-09-26',value:450}]}],
  workouts:[{date, label:'户外步行', start_time:1790420000,duration_min:35,distance_km:2.4,calories:120,avg_hr:108}],coverage:{start_date:'2026-09-20',end_date:date,data_days:3,source_count:2},
});
const settle = () => new Promise(resolve=>setTimeout(resolve,30));

async function run() {
  const requests=[];
  let releaseOlder;
  let stale=false;
  let invalidUrl=false;
  const dom=new JSDOM(fs.readFileSync(path.join(root,'index.html'),'utf8'),{url:'http://hub.test/#home',runScripts:'outside-only',pretendToBeVisual:true});
  const w=dom.window;
  w.EventSource=class{close(){}};
  w.fetch=async(url,options={})=>{
    requests.push({url,method:options.method||'GET',body:options.body});
    let data={};
    if(url==='/api/status')data={ready:true,started_at:at};
    else if(url==='/api/home')data={devices:[{key:'watch',name:'手表',category:'watch',enabled:false,controls:[],properties:[]}],audit:[]};
    else if(url==='/api/hub')data={resources:[],events:[]};
    else if(url.includes('/auth/'))data={state:'idle'};
    else if(url.startsWith('/api/health-source')){
      const query=new URL(url,'http://hub.test').searchParams;
      const body=options.body?JSON.parse(options.body):{};
      const date=body.date||query.get('date')||'2026-09-26';
      const days=body.days||Number(query.get('days'))||7;
      if(date==='2026-09-24')await new Promise(resolve=>{releaseOlder=resolve;});
      data=fixture(date,days);
      if(stale)Object.assign(data,{available:false,stale:true,message:'健康中心暂时离线，显示上次读取的同范围记录',checked_at:'2026-09-27T05:00:00+08:00'});
      if(invalidUrl)data.service.web_url='javascript:alert(1)';
    }
    return{ok:true,status:200,json:async()=>structuredClone(data)};
  };
  const query=selector=>w.document.querySelector(selector);
  const healthRequests=()=>requests.filter(item=>item.url.startsWith('/api/health-source'));
  try{
    w.eval(fs.readFileSync(path.join(root,'app.js'),'utf8'));
    await settle();
    assert.equal(healthRequests().length,0,'Normal hub startup never reads health data');
    w.openHomeDevice('watch');
    assert.equal(query('#home-detail-health').hidden,false);
    assert.match(query('#home-detail-health').textContent,/账号健康数据/);
    query('#home-detail-health').click();
    await settle();
    assert.equal(healthRequests().length,1);
    assert.equal(healthRequests()[0].method,'GET');
    assert.equal(w.location.hash,'#health');
    assert.match(query('[data-health-metric="health.activity:steps"]').textContent,/0 步/);
    assert.match(query('[data-health-metric="health.sleep:total_min"]').textContent,/7 小时 30 分/);
    assert.match(query('[data-health-metric="health.heart:resting_hr"]').textContent,/57 次\/分/);
    assert.match(query('#health-workouts').textContent,/户外步行.*35 分钟.*2.4 km/);
    assert.match(query('#health-upstream-time').textContent,/源端最新记录：2026-09-26.*源端更新/);
    assert.match(query('#health-fetched-time').textContent,/中枢读取时间/);
    assert.equal(query('#health-origin-link').href,'http://nas.test:8321/');
    assert.equal(query('#health-origin-link').rel,'noopener noreferrer');
    assert.equal(w.document.querySelectorAll('#health-trend-chart circle').length,3,'Zero is valid, missing dates not fabricated');
    assert.equal((query('#health-trend-chart path').getAttribute('d').match(/M/g)||[]).length,3,'Missing days create chart gaps');
    assert.equal(w.document.querySelectorAll('#health-trend-records .health-trend-record').length,7);
    assert.match(query('#health-trend-records').textContent,/未记录/);
    assert.equal(w.document.querySelectorAll('#hub-resource-list .hub-resource-card').length,0,'Health remains out of general hub resources');
    assert.doesNotMatch(query('#overview-environment').textContent,/静息心率|步数/);

    w.showView('overview');w.showView('health');
    await w.refreshAll(true);
    assert.equal(healthRequests().length,1,'Tab revisits and global hub refresh do not silently reread health');
    query('#health-days').value='30';query('#health-days').dispatchEvent(new w.Event('change'));
    await settle();
    assert.match(healthRequests().at(-1).url,/days=30/);
    const date=query('#health-date');
    date.value='2026-09-24';date.dispatchEvent(new w.Event('change'));
    await settle();
    date.value='2026-09-25';date.dispatchEvent(new w.Event('change'));
    await settle();
    releaseOlder();await settle();
    assert.equal(query('#health-daily-date').textContent,'2026-09-25','Late date response cannot overwrite selected date');
    stale=true;
    query('#health-refresh').click();
    await settle();
    const refreshed=healthRequests().at(-1);
    assert.equal(refreshed.method,'POST');
    assert.equal(refreshed.url,'/api/health-source/refresh');
    assert.deepEqual(JSON.parse(refreshed.body),{days:30,date:'2026-09-25'});
    assert.match(query('#health-source-pill').textContent,/显示缓存/);
    assert.equal(query('#health-content').hidden,false);
    assert.match(query('#health-fetched-time').textContent,/最近尝试/);
    invalidUrl=true;
    query('#health-latest').click();await settle();
    assert.equal(query('#health-origin-link').hidden,true,'Invalid origin URLs never become links');
    assert.equal(requests.filter(item=>item.method!=='GET').length,1,'Only explicit refresh POST; no upstream sync or AI requests');
    assert.ok(requests.every(item=>!item.url.includes('/api/chat')&&!item.url.includes('/sync')));
    console.log('PASS: health loads only on explicit page visit; account-level watch link; distinct source/fetch timestamps; activity/sleep/heart/workout cards; health stays out of hub/environment; missing-day chart gaps; zero values; dates/range; latest-response isolation; stale cache; explicit refresh only; safe origin link.');
  }finally{w.close();}
}

async function live(base){
  const get=async(url,options={})=>{assert.ok(!options.method||options.method==='GET','Live health check is GET-only');return fetch(new URL(url,base),{...options,signal:AbortSignal.timeout(18000)});};
  const [html,script]=await Promise.all([get('/').then(r=>r.text()),get('/app.js').then(r=>r.text())]);
  const dom=new JSDOM(html,{url:new URL('/#health',base).href,runScripts:'outside-only',pretendToBeVisual:true});
  const w=dom.window;
  w.EventSource=class{close(){}};
  const closeHarness=require('./readonly_dom_harness.cjs')(w,base);
  try{
    w.eval(script);
    const deadline=Date.now()+20000;
    while(queryLoading()&&Date.now()<deadline)await settle();
    function queryLoading(){return w.document.querySelector('#health-refresh').disabled;}
    assert.equal(queryLoading(),false,'Health read completes within20s');
    assert.ok(w.document.querySelector('#health-source-message').textContent);
    assert.doesNotMatch(w.document.querySelector('#view-health').textContent,/Invalid Date|NaN/);
    assert.equal(w.document.querySelectorAll('#hub-resource-list [data-resource-id^="health."]').length,0);
    console.log(`PASS live GET-only: dedicated health view rendered; ${w.document.querySelectorAll('[data-health-metric]').length} metric cards, ${w.document.querySelectorAll('[data-health-field]').length} source fields. No actual controls, upstream sync or refresh POST.`);
  }finally{await closeHarness();}
}
const base=process.argv.slice(2).find(arg=>/^https?:\/\//.test(arg));
run().then(()=>base?live(base):null).catch(error=>{console.error(error);process.exitCode=1;});
