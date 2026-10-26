// Hierarchy/read-frequency regression using fixture data only; no real requests.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {JSDOM} = require(process.env.HUB_JSDOM_MODULE || 'jsdom');
const root = path.resolve(__dirname, '../src/xiaomi_ai_bridge/web');
const now = new Date().toISOString();
const previous = new Date(Date.now()-8*3600000).toISOString();
const makeField = (key,label,role,priority,seconds,value,unit='') => ({key,path:[key],label,role,priority,type:typeof value==='boolean'?'boolean':typeof value,readable:true,writable:role==='setting',status:'ok',source:'miot.cloud',unit,chartable:role==='measurement',comparable:role==='measurement',refresh_seconds:seconds,refresh_policy:seconds===60?'每60秒更新':seconds===300?'每5分钟更新':seconds===86400?'每日更新':'每6小时更新',observed_at:priority==='detail'?previous:now,cached:priority==='detail'});
const purifierFields = [makeField('temperature','温度','measurement','primary',60,26,'℃'),makeField('on','电源','state','primary',60,true),makeField('target','目标设置','setting','secondary',300,3),makeField('serial_no','序列号','identity','detail',86400,123456789),makeField('firmware_version','固件版本','metadata','detail',21600,'2.1.0')];
const purifier = {key:'air',name:'空气净化器',alias:'空气净化器',category:'air-purifier',category_label:'空气净化器',enabled:true,cloud_online:true,observed_at:now,online:true,stale:false,state:{temperature:26,on:true,target:3,serial_no:123456789,firmware_version:'2.1.0'},controls:[],properties:purifierFields.map(field=>({...field,service:'设备',read_status:{status:field.status,observed_at:field.observed_at,refresh_policy:field.refresh_policy}}))};
const resources = [
  {resource_id:'home.air-purifier.air',name:'空气净化器',kind:'sensor',location:'客厅',observed_at:now,received_at:now,stale:false,online:true,state:purifier.state,fields:purifierFields},
  {resource_id:'service.lamp',name:'书桌灯状态',kind:'service',location:'客厅',observed_at:now,received_at:now,stale:false,online:true,state:{on:true},fields:[makeField('on','电源','state','primary',60,true)]},
  {resource_id:'sensor.balcony',name:'阳台温度计',kind:'sensor',location:'阳台',observed_at:now,received_at:now,stale:false,online:true,state:{temperature:20},fields:[makeField('temperature','温度','measurement','primary',60,20,'℃')]},
];
const snapshot = {resources,events:[],sampling:{interval_seconds:60}};
const settle = () => new Promise(resolve=>setTimeout(resolve,25));

async function run(){
  const dom=new JSDOM(fs.readFileSync(path.join(root,'index.html'),'utf8'),{url:'http://hub.test/#data',runScripts:'outside-only',pretendToBeVisual:true});
  const w=dom.window;
  w.EventSource=class{close(){}};
  w.fetch=async(url,options={})=>{
    assert.ok(!options.method||options.method==='GET','Hierarchy navigation must not write');
    let data={};
    if(url==='/api/home')data={devices:[purifier],audit:[]};
    else if(url==='/api/hub')data=snapshot;
    else if(url==='/api/status')data={ready:true,started_at:now};
    else if(url.includes('/history'))data={events:[]};
    else if(url.includes('/auth/'))data={state:'idle'};
    else if(url.endsWith('/state'))data={state:purifier.state,observed_at:now};
    return{ok:true,status:200,json:async()=>structuredClone(data)};
  };
  const query=selector=>w.document.querySelector(selector);
  const change=(selector,value)=>{const node=query(selector);node.value=value;node.dispatchEvent(new w.Event('change'));};
  try{
    w.eval(fs.readFileSync(path.join(root,'app.js'),'utf8'));await settle();
    assert.equal(query('#hub-primary-list'),null,'No duplicate primary-metric strip');
    assert.equal(query('#hub-detail').hidden,true,'Details remain closed until explicit selection');
    assert.equal(w.document.querySelectorAll('#hub-resource-list [data-layer-field]').length,2);
    assert.doesNotMatch(query('#hub-resource-list').textContent,/序列号|123456789|2\.1\.0|目标设置/);
    assert.equal(query('.hub-browse-advanced').open,false);
    assert.equal(w.document.querySelectorAll('.hub-room-group').length,2);
    assert.match(query('.hub-room-heading').textContent,/客厅.*2 个数据源/);
    query('.hub-resource-card').click();await settle();
    assert.equal(query('#hub-detail').hidden,false);
    assert.equal(query('#hub-field-advanced').open,false,'Advanced dictionary is collapsed by default');
    assert.equal(query('#hub-identity-section').open,false,'Device archives are collapsed by default');
    assert.equal(query('#hub-settings-section').open,false);
    assert.equal(query('.hub-activity-details').open,false,'Event stream is secondary');
    assert.match(query('#hub-state-grid').textContent,/温度26℃.*每60秒更新/);
    assert.doesNotMatch(query('#hub-state-grid').textContent,/序列号|目标设置/);
    assert.match(query('#hub-operating-states').textContent,/运行状态.*电源.*开启/);
    assert.match(query('#hub-settings-grid').textContent,/目标设置.*每5分钟更新/);
    assert.match(query('#hub-identity-grid').textContent,/序列号.*123456789.*每日更新/);
    assert.match(query('#hub-identity-grid').textContent,new RegExp(w.formatTime(previous).replace(/[.*+?^${}()|[\]\\]/g,'\\$&')));
    assert.match(query('#hub-layer-counts').textContent,/1 项已有读数.*1 项运行状态/);
    query('#hub-identity-section').open=true;
    query('#hub-field-advanced').open=true;
    w.renderHub(structuredClone(snapshot));
    assert.equal(query('#hub-identity-section').open,true,'Polling preserves expanded archives');
    assert.equal(query('#hub-field-advanced').open,true);
    change('#hub-filter-room','客厅');
    assert.equal(w.document.querySelectorAll('.hub-resource-card').length,2);
    assert.equal(w.document.querySelectorAll('#hub-resource-list [data-layer-field]').length,1);
    change('#hub-filter-purpose','identity');
    assert.equal(w.document.querySelectorAll('.hub-resource-card').length,1);
    change('#hub-filter-room','阳台');
    assert.equal(w.document.querySelectorAll('.hub-resource-card').length,0);
    assert.equal(query('#hub-resource-filter-empty').hidden,false);
    change('#hub-filter-purpose','telemetry');
    assert.equal(w.document.querySelectorAll('.hub-resource-card').length,1);
    assert.match(query('.hub-resource-head').textContent,/阳台温度计/);
    w.openHomeDevice('air');await settle();
    assert.equal(query('[data-home-layer="identity"]').open,false);
    assert.equal(query('[data-home-layer="diagnostics"]').open,false);
    assert.ok(query('[data-property-key="serial_no"]').closest('[data-home-layer="identity"]'));
    assert.equal(w.document.querySelectorAll('#home-capabilities [data-property-key]').length,5,'All capabilities remain accessible once expanded');
    console.log('PASS: primary telemetry; room/purpose filters; operating status; collapsed settings/archive/diagnostics/events; identity absent from primary views; all fields expandable; actual field cadence and cached last-read timestamps; polling preserves expansion; home detail layers; no writes.');
  }finally{w.close();}
}
run().catch(error=>{console.error(error);process.exitCode=1;});
