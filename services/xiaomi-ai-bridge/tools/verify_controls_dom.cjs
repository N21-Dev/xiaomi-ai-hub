// DOM-only interaction checks using simulated devices. Never sends a real command.
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const {JSDOM}=require(process.env.HUB_JSDOM_MODULE || 'jsdom');
const root=process.env.HUB_CONTROLS_WEB_ROOT || path.resolve(__dirname,'../src/xiaomi_ai_bridge/web');
const toggle={kind:'toggle',property:'on',label:'电源',action:'turn_on',off_action:'turn_off'};
const devices=[
  {key:'lamp',alias:'书桌台灯',name:'书桌台灯',category:'light',category_label:'灯',enabled:true,online:true,stale:false,
   state:{on:'关闭',brightness:50,'color-temperature':4700,mode:'Reading'},
   controls:[toggle,{kind:'range',property:'brightness',label:'亮度',action:'set_brightness',min:1,max:100,step:1,unit:'%'},
     {kind:'range',property:'color-temperature',label:'色温',action:'set_color_temperature',min:2600,max:5100,step:1,unit:'K'},
     {kind:'select',property:'mode',label:'模式',action:'set_mode',options:[{value:'Reading',label:'阅读'},{value:'Free',label:'自由调节'}]}]},
  {key:'air',alias:'空气净化器',name:'空气净化器',category:'air-purifier',category_label:'空气净化器',enabled:true,online:true,stale:false,
   state:{on:'开启',mode:'Favorite',temperature:27,'relative-humidity':55,'pm2.5-density':1},
   controls:[toggle,{kind:'select',property:'mode',label:'运行模式',action:'set_mode',options:[{value:'Auto',label:'自动'},{value:'Sleep',label:'睡眠'},{value:'Favorite',label:'最爱'}]}]},
];
devices.forEach(d=>{d.observed_at=new Date().toISOString();d.resource_id=`home.${d.category}.${d.key}`;d.actions=d.controls.flatMap(c=>[c.action,c.off_action].filter(Boolean));});
const resource=d=>({resource_id:d.resource_id,name:d.name,kind:d.category==='light'?'service':'sensor',state:d.state,observed_at:d.observed_at,received_at:d.observed_at,expires_at:new Date(Date.now()+180000).toISOString(),online:true,stale:false});
const snapshot=()=>({resources:devices.map(resource),events:[],sampling:{interval_seconds:60}});
const commands=[];
let releaseControl=null;
let controlGate=null;
let outcome='confirmed';
const wait=()=>new Promise(r=>setTimeout(r,20));

async function main(){
 const dom=new JSDOM(fs.readFileSync(path.join(root,'index.html'),'utf8'),{url:'http://hub.test/#home',runScripts:'outside-only',pretendToBeVisual:true});
 const w=dom.window;
 w.EventSource=class{close(){}};
 w.fetch=async(target,options={})=>{
   const url=String(target);let data={};let ok=true;
   if(url==='/api/home')data={devices,audit:[]};
   else if(url==='/api/hub')data=snapshot();
   else if(url.includes('/history'))data={events:[]};
   else if(url==='/api/status')data={ready:true,started_at:new Date().toISOString()};
   else if(url.includes('/auth/'))data={state:'idle'};
   else if(url.endsWith('/control')||url.endsWith('/state')){
      const device=devices.find(d=>url.includes(`/${d.key}/`));assert.ok(device);
      if(url.endsWith('/control')){
         const body=JSON.parse(options.body);commands.push({key:device.key,...body});
         if(controlGate)await controlGate;
         if(outcome==='network')throw new TypeError('network unavailable');
         if(outcome==='confirmed'){
           const prop={set_brightness:'brightness',set_color_temperature:'color-temperature',set_mode:'mode',prop_s8_p1:'favorite-fan-level'}[body.action];
           if(prop)device.state[prop]=body.value;
           else device.state.on=body.action==='turn_on'?'开启':'关闭';
         }
         ok=outcome==='confirmed';
      }
      device.observed_at=new Date(Date.now()+100).toISOString();
      data={ok,outcome:ok?'confirmed':'unconfirmed',message:ok?'操作已生效':'指令已发送，暂未确认',device_key:device.key,state:device.state,observed_at:device.observed_at,resource:resource(device)};
   }
   return {ok,status:ok?200:409,json:async()=>structuredClone(data)};
 };
 try{
  w.eval(fs.readFileSync(path.join(root,'app.js'),'utf8'));await wait();
  assert.equal(w.document.querySelectorAll('.home-device-card').length,2);
  w.openHomeDevice('lamp');await wait();
  let brightness=w.document.querySelector('[data-control-action="set_brightness"] input');
  brightness.value=40;brightness.dispatchEvent(new w.Event('input',{bubbles:true}));
  assert.equal(commands.length,0,'Dragging must not send control');
  devices[0].state.brightness=70;devices[0].observed_at=new Date(Date.now()+50).toISOString();
  w.renderHub(snapshot());await wait();
  assert.equal(brightness.value,'40','SSE updates must preserve preview');
  controlGate=new Promise(r=>{releaseControl=r});
  w.document.querySelector('[data-control-action="set_brightness"] .home-apply').click();
  w.runHomeControl('lamp','set_brightness',40);await wait();
  assert.equal(commands.length,1,'Double click must issue one write');
  assert.equal(commands[0].value,40);assert.ok(commands[0].request_id);
  assert.equal(brightness.disabled,true);
  releaseControl();controlGate=null;await wait();
  assert.equal(brightness.value,'40');assert.equal(brightness.disabled,false);
  assert.match(w.document.querySelector('#home-detail-message').textContent,/已生效/);
  w.openHomeDevice('air');await wait();
  assert.equal(w.document.querySelectorAll('#home-control-list input[type=range]').length,0,'Purifier must not invent a fan slider');
  outcome='unconfirmed';
  w.document.querySelector('.home-mode-choice[data-value="Sleep"]').click();await wait();
  assert.match(w.document.querySelector('#home-detail-message').textContent,/未确认/);
  assert.ok(w.document.querySelector('.home-mode-choice[data-value="Favorite"]').classList.contains('selected'));
  assert.ok(!w.document.querySelector('.home-mode-choice[data-value="Sleep"]').classList.contains('selected'));
  outcome='network';w.document.querySelector('.home-mode-choice[data-value="Auto"]').click();await wait();
  assert.match(w.document.querySelector('#home-detail-message').textContent,/未确认/);
  assert.ok(w.document.querySelector('.home-mode-choice[data-value="Favorite"]').classList.contains('selected'),'Network errors must retain last known state');
  assert.equal(commands.length,3,'Network failure must not retry writes');
  devices[1].enabled=false;w.renderHome({devices:structuredClone(devices),audit:[]});
  assert.ok([...w.document.querySelectorAll('#home-control-list button')].every(b=>b.disabled),'Revoked device must disable controls');
  // Promote only the actual favorite-speed capability; no invented RPC/action.
  const fan={action:'prop_s8_p1',property:'favorite-fan-level',label:'最爱风速',service:'净化器最爱模式',format:'uint8',kind:'range',min:0,max:14,step:1,enabled:true,requires_confirmation:false};
  devices[1].enabled=true;devices[1].extra_controls=[fan];
  devices[1].properties=[{key:'favorite-fan-level',label:'最爱风速',service:'净化器最爱模式',readable:true,writable:true,format:'uint8'}];
  w.renderHome({devices:structuredClone(devices),audit:[]});
  const fanSection=()=>w.document.querySelector('[data-control-action="prop_s8_p1"]');
  const fanSlider=()=>fanSection().querySelector('input[type="range"]');
  assert.equal(fanSlider().min,'0');assert.equal(fanSlider().max,'14');assert.equal(fanSlider().step,'1');
  assert.equal(w.document.querySelectorAll('#home-capabilities [data-capability-action="prop_s8_p1"]').length,0,'Promoted control must not duplicate in advanced settings');
  assert.match(fanSection().querySelector('output').textContent,/待读取/);
  assert.equal(fanSection().querySelector('button').disabled,true,'Unknown speed is not fabricated as the slider midpoint');
  assert.match(fanSection().textContent,/当前为最爱模式/);
  for(let level=0;level<=14;level++){
    fanSlider().value=level;fanSlider().dispatchEvent(new w.Event('input',{bubbles:true}));
    assert.match(fanSection().querySelector('output').textContent,new RegExp(`^${level}级 · 待应用$`));
    assert.match(fanSlider().getAttribute('aria-valuetext'),/待应用/);
  }
  assert.equal(commands.length,3,'All fifteen slider positions only edit drafts');
  devices[1].state['favorite-fan-level']=2;devices[1].state.mode='Sleep';w.renderHub(snapshot());w.renderHome({devices:structuredClone(devices),audit:[]});
  assert.equal(fanSlider().value,'14','Live state must retain the unsent favorite-speed draft');
  assert.match(fanSection().textContent,/仅在最爱模式下生效/);
  assert.match(fanSection().textContent,/不会自动切换模式/);
  outcome='confirmed';controlGate=new Promise(r=>{releaseControl=r});
  fanSection().querySelector('button').click();fanSection().querySelector('button').click();await wait();
  assert.equal(commands.length,4,'Favorite speed applies once despite duplicate clicks');
  assert.equal(commands.at(-1).action,'prop_s8_p1');assert.equal(commands.at(-1).value,14);assert.equal(typeof commands.at(-1).value,'number');
  assert.equal(fanSlider().disabled,true);releaseControl();controlGate=null;await wait();
  assert.equal(devices[1].state.mode,'Sleep','Applying favorite speed never silently changes mode');
  assert.equal(fanSlider().value,'14');assert.doesNotMatch(fanSection().querySelector('output').textContent,/待应用/);
  fanSlider().value=0;fanSlider().dispatchEvent(new w.Event('input',{bubbles:true}));
  outcome='unconfirmed';fanSection().querySelector('button').click();await wait();
  assert.equal(commands.length,5);assert.equal(commands.at(-1).value,0);
  assert.equal(fanSlider().value,'0');assert.match(fanSection().querySelector('output').textContent,/待应用/);
  assert.equal(devices[1].state['favorite-fan-level'],14,'Unconfirmed setting must not overwrite observed speed');
  w.openHomeDevice('lamp');await wait();w.openHomeDevice('air');await wait();assert.equal(fanSlider().value,'0','Reopening preserves unconfirmed draft');
  devices[1].cloud_online=false;w.renderHome({devices:structuredClone(devices),audit:[]});
  assert.equal(fanSlider().disabled,true);assert.equal(fanSection().querySelector('button').disabled,true);
  devices[1].cloud_online=true;devices[1].enabled=false;w.renderHome({devices:structuredClone(devices),audit:[]});assert.equal(fanSlider().disabled,true);
  devices[1].enabled=true;fan.enabled=false;fan.reason='此属性只读';w.renderHome({devices:structuredClone(devices),audit:[]});assert.equal(fanSlider().disabled,true);assert.match(fanSection().textContent,/此属性只读/);
  fan.enabled=true;fan.requires_confirmation=true;w.confirm=()=>false;w.renderHome({devices:structuredClone(devices),audit:[]});fanSection().querySelector('button').click();await wait();assert.equal(commands.length,5,'Existing capability confirmation cannot be bypassed by promotion');
  devices[1].extra_controls=[{...fan,min:0,max:7,step:1}];w.renderHome({devices:structuredClone(devices),audit:[]});assert.equal(fanSlider().max,'7','Bounds come from the capability, not a hardcoded model');
  devices[1].extra_controls=[];w.renderHome({devices:structuredClone(devices),audit:[]});assert.equal(w.document.querySelectorAll('#home-control-list input[type="range"]').length,0,'Capability removal withdraws the promoted slider');
  console.log('PASS: sliders preview/apply, favorite fan 0–14 capability promotion, actual bounds, no implicit mode/write, readback/drafts/live updates/reopen, duplicate protection, unknown state, offline/permission/confirmation, no unsupported slider.');
 }finally{try {w.renderHub({resources:[],events:[]});} finally {w.close();}}
}
async function deployed(base){
 const [page,code,home]=await Promise.all([
   fetch(base+'/').then(r=>r.text()),fetch(base+'/app.js').then(r=>r.text()),fetch(base+'/api/home').then(r=>r.json()),
 ]);
 const dom=new JSDOM(page,{url:base+'/#home',runScripts:'outside-only',pretendToBeVisual:true});
 const w=dom.window;
 w.EventSource=class{close(){}};
 const closeHarness=require('./readonly_dom_harness.cjs')(w,base);
 try{
   w.eval(code);
   const deadline=Date.now()+15000;
   while(!w.document.querySelector('.home-device-card')&&Date.now()<deadline)await wait();
   for(const category of ['light','air-purifier']){
     const d=home.devices.find(item=>item.category===category);assert.ok(d);
     w.openHomeDevice(d.key);
     let limit=Date.now()+10000;
     while(w.document.querySelector('#home-detail-refresh').disabled&&Date.now()<limit)await wait();
     assert.equal(w.document.querySelector('#home-device-detail').hidden,false);
     assert.equal(w.document.querySelector('#home-detail-name').textContent,d.alias);
     if(category==='light'){
       const input=w.document.querySelector('[data-control-action="set_brightness"] input');
       assert.equal(input.min,'1');assert.equal(input.max,'100');
       const color=w.document.querySelector('[data-control-action="set_color_temperature"] input');
       assert.equal(color.min,'2600');assert.equal(color.max,'5100');
     }else{
       const fan=w.document.querySelector('[data-control-action="prop_s8_p1"] input[type="range"]');
       assert.ok(fan);assert.equal(fan.min,'0');assert.equal(fan.max,'14');assert.equal(fan.step,'1');
       assert.equal(w.document.querySelectorAll('.home-mode-choice').length,3);
       assert.match(w.document.querySelector('#home-control-list').textContent,/自动/);
       assert.match(w.document.querySelector('#home-control-list').textContent,/睡眠/);
       assert.match(w.document.querySelector('#home-control-list').textContent,/最爱/);
       assert.match(w.document.querySelector('#home-detail-metrics').textContent,/PM2.5/);
     }
   }
   console.log('PASS: deployed device cards and supported controls use actual NAS device metadata and read-only state refresh.');
 }finally{await closeHarness();}
}
module.exports={devices};
if(require.main===module)main().then(()=>process.argv[2]?deployed(process.argv[2]):null).catch(e=>{console.error(e);process.exitCode=1;});
