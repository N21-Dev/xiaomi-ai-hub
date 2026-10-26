// Browser integration checks against synthetic fixtures only.
const fs = require('node:fs/promises');
const vm = require('node:vm');
const path = require('node:path');
const assert = require('node:assert/strict');
const {chromium} = require(process.env.HUB_PLAYWRIGHT_MODULE || 'playwright');
const {default:AxeBuilder} = require(process.env.HUB_AXE_MODULE || '@axe-core/playwright');
const project = process.env.HUB_UI_ROOT || path.resolve(__dirname, '..');
const out = process.env.HUB_UI_OUTPUT || '/tmp/model-routing-preview';
(async()=>{
 await fs.mkdir(out,{recursive:true});
 const source=await fs.readFile(project+'/tools/verify_model_console_dom.cjs','utf8');
 const fixture=JSON.parse(JSON.stringify(vm.runInNewContext(source.slice(source.indexOf('const now ='),source.indexOf('const settle ='))+'; ({registry,probes,usage,logs});')));
 // 夹具必须与后端 public() 的真实形状一致。以前这里手改 token_required 来模拟免密，
 // 结果永远测不到「后端发 true、真实状态是免密」这一类不一致。
 assert.deepEqual(Object.keys(fixture.registry.gateway).filter(k=>k==='auth_required'||k==='token_required').sort(),['auth_required','token_required'],'夹具缺少 gateway 鉴权字段');
 assert.equal(fixture.registry.gateway.auth_required,fixture.registry.gateway.token_required,'夹具里两个鉴权字段必须一致');
 assert.equal(fixture.registry.gateway.auth_required,false,'本套浏览器检查只覆盖免密部署');
 const browser=await chromium.launch({headless:true,args:['--no-sandbox']});
 const report=[];
 try {
  for(const size of [{name:'desktop',width:1536,height:1000},{name:'tablet',width:1024,height:900},{name:'mobile',width:390,height:844}]){
   const data=structuredClone(fixture);
   data.registry.providers[1].enabled=true;
   data.registry.providers[1].api_key_set=true;
   data.registry.providers.push({id:'free',name:'OpenRouter · 免费模型',enabled:true,api_key_set:true,protocol:'openai',base_url:'https://openrouter.ai/api/v1',models:['liquid/lfm-2.5-2.6b:free','nvidia/nemotron-3-super-120b-a12b:free'],model_settings:[]});
   const context=await browser.newContext({viewport:size,locale:'zh-CN',reducedMotion:'reduce'});
   const page=await context.newPage();
   const errors=[],writes=[],a11y=[];let rejectActivation=false;let clipboard='';
   page.on('pageerror',e=>errors.push(e.message));
   await page.addInitScript(()=>{
    window.EventSource=class{close(){}};
    window.__copies=[];
    document.execCommand=(action)=>{if(action==='copy'){window.__copies.push(document.activeElement.value);return true;}return false;};
   });
   await page.route('**/*',async route=>{
    const req=route.request(),url=new URL(req.url());
    const file={'/':'index.html','/app.js':'app.js','/app.css':'app.css','/speaker.png':'speaker.png'}[url.pathname];
    if(file)return route.fulfill({body:await fs.readFile(project+'/src/xiaomi_ai_bridge/web/'+file),contentType:file.endsWith('.css')?'text/css':file.endsWith('.js')?'text/javascript':file.endsWith('.png')?'image/png':'text/html'});
    let body={};
    if(req.method()!=='GET'){
     const input=req.postDataJSON();writes.push({method:req.method(),path:url.pathname,body:input});
     if(url.pathname.endsWith('/activate')){
      if(rejectActivation)return route.fulfill({status:409,json:{ok:false,error:'配置已更新，请刷新后重试'}});
      assert.equal(input.revision,data.registry.revision);
      data.registry.active={provider_id:decodeURIComponent(url.pathname.split('/')[3]),model:input.model};data.registry.revision++;
      return route.fulfill({json:{ok:true,...data.registry}});
     }
     if(url.pathname.endsWith('/settings')){
      const provider=data.registry.providers.find(p=>p.id===url.pathname.split('/')[3]);
      provider.model_settings=provider.model_settings.filter(v=>v.model!==input.model);
      provider.model_settings.push(input);data.registry.revision++;
      return route.fulfill({json:{ok:true,...data.registry}});
     }
     return route.fulfill({status:400,json:{ok:false,error:'Unexpected fixture mutation'}});
    }
    if(url.pathname==='/api/models')body=data.registry;
    else if(url.pathname.endsWith('/probes'))body=data.probes;
    else if(url.pathname.endsWith('/usage'))body={...data.usage,hours:Number(url.searchParams.get('hours')||24)};
    else if(url.pathname.endsWith('/logs'))body=data.logs;
    else if(url.pathname==='/api/status')body={ready:true,started_at:new Date().toISOString()};
    else if(url.pathname==='/api/hub')body={resources:[],events:[]};
    else if(url.pathname==='/api/home')body={devices:[],audit:[]};
    else if(url.pathname.includes('/auth/'))body={state:'idle'};
    return route.fulfill({json:body});
   });
   const overflow=()=>page.evaluate(()=>document.documentElement.scrollWidth>innerWidth);
   const audit=async(name,selector)=>{
    const result=await new AxeBuilder({page}).include(selector).withTags(['wcag2a','wcag2aa','wcag21aa']).analyze();
    a11y.push({name,violations:result.violations.map(v=>({id:v.id,impact:v.impact,nodes:v.nodes.map(n=>n.target)}))});
   };
   await page.goto('http://preview.test/#services');
   await page.waitForSelector('.route-config');
   await page.waitForFunction(()=>document.querySelector('#console-request-count').textContent==='12');
   assert.equal(await page.locator('#model-gateway-status').innerText(),'网关就绪');
   assert.match(await page.locator('#gateway-token-state').innerText(),/免密访问/);
   assert.equal(await page.locator('#route-activate').isDisabled(),true);
   assert.equal(writes.length,0);
   await page.screenshot({path:out+'/routes-'+size.name+'.png',fullPage:true});
   assert.equal(await overflow(),false,'initial overflow');
   await audit('routes','#view-services');
   // Picking a configuration/model is local; only the explicit button sends a mutation.
   await page.locator('input[name=route-provider][value=anthropic-main]').check();
   assert.equal(writes.length,0);
   assert.equal(await page.locator('#gateway-active-model').innerText(),'alpha');
   assert.equal(await page.locator('#route-preview-label').innerText(),'待启用路由');
   await page.locator('#route-activate').click();
   await page.waitForFunction(()=>document.querySelector('#gateway-active-model').textContent==='claude-sonnet');
   assert.equal(writes.length,1);assert.equal(writes[0].body.model,'claude-sonnet');
   assert.match(await page.locator('#route-result').innerText(),/已启用 Anthropic 主账号/);
   assert.equal(await page.locator('#route-activate').isDisabled(),true);
   // A failed switch must preserve the current route and expose the error.
   await page.locator('select[data-route-model=free]').selectOption('nvidia/nemotron-3-super-120b-a12b:free');
   rejectActivation=true;
   await page.locator('#route-activate').click();
   await page.waitForFunction(()=>document.querySelector('#route-result').textContent.startsWith('切换失败'));
   assert.equal(await page.locator('#gateway-active-model').innerText(),'claude-sonnet');
   assert.equal(await page.locator('#route-activate').isEnabled(),true);
   rejectActivation=false;
   await page.locator('#route-activate').click();
   await page.waitForFunction(()=>document.querySelector('#gateway-active-model').textContent==='nvidia/nemotron-3-super-120b-a12b:free');
   // Read-only guide, keyboard tabs and HTTP clipboard fallback inside a native dialog.
   await page.locator('#gateway-connect').click();
   await page.locator('#gateway-connect-dialog').waitFor({state:'visible'});
   assert.equal(await page.locator('#connect-model-id').innerText(),'hub-active');
   assert.equal(await page.locator('#connect-key').innerText(),'local');
   await page.locator('#connect-tab-openai').focus();await page.keyboard.press('ArrowRight');
   assert.equal(await page.locator('#connect-tab-anthropic').getAttribute('aria-selected'),'true');
   assert.match(await page.locator('#connect-code').innerText(),/\/v1\/messages/);
   assert.match(await page.locator('#connect-code').innerText(),/hub-active/);
   await page.locator('[data-copy=connect-code]').click();
   const copied=await page.evaluate(()=>window.__copies.at(-1));
   assert.equal(copied,await page.locator('#connect-code').innerText());
   assert.ok(copied.includes(' \\\n'), 'Bash line continuations must contain a literal newline');
   await page.locator('#connect-model-select').selectOption('beta');
   assert.equal(await page.locator('#connect-model-id').innerText(),'beta');
   await audit('guide','#gateway-connect-dialog');
   await page.screenshot({path:out+'/connection-'+size.name+'.png'});
   await page.keyboard.press('Escape');
   assert.equal(await page.locator('#gateway-connect-dialog').isVisible(),false);
   // Overview shortcut jumps to the channel list; the mobile-safe settings drawer opens from the model catalog.
   await page.locator('#gateway-locate-active').click();
   assert.equal(await page.locator('#csview-channels').isVisible(),true);
   assert.equal(await page.locator('#csview-overview').isVisible(),false);
   await page.screenshot({path:out+'/channels-'+size.name+'.png',fullPage:true});
   await audit('channels','#view-services');
   await page.locator('#csnav-models').click();
   await page.locator('#model-channel').selectOption('env-default');
   await page.locator('#model-filter').selectOption('all');
   await page.locator('[data-mc-act=settings][data-mc-model=beta]').click();
   assert.equal(await page.locator('#console-drawer').isVisible(),true);
   await page.locator('#model-settings-context').fill('200000');
   await page.locator('#model-settings-form button[type=submit]').click();
   await page.waitForFunction(()=>document.querySelector('#model-settings-result').textContent==='设置已保存');
   assert.equal(writes.at(-1).body.context_window,200000);
   await audit('settings','#console-drawer');
   await page.screenshot({path:out+'/settings-'+size.name+'.png'});
   await page.keyboard.press('Escape');
   assert.equal(await page.locator('#console-drawer').isVisible(),false);
   assert.equal(await overflow(),false,'management overflow');
   await audit('management','#view-services');
   await page.screenshot({path:out+'/management-'+size.name+'.png',fullPage:true});
   // Disabled configurations remain unavailable, without a misleading "enabled" state.
   await page.locator('#csnav-overview').click();
   data.registry.providers[1].enabled=false;
   await page.locator('#console-refresh').click();
   await page.waitForFunction(()=>document.querySelector('select[data-route-model="anthropic-main"]').disabled);
   await page.locator('input[name=route-provider][value=anthropic-main]').check();
   assert.equal(await page.locator('#route-activate').isDisabled(),true);
   // Empty state and long external names should be safe and stay within viewport.
   data.registry.providers=[];data.registry.active={};
   await page.locator('#console-refresh').click();
   await page.locator('#route-config-empty').waitFor({state:'visible'});
   assert.equal(await page.locator('#model-gateway-status').innerText(),'等待接入');
   assert.equal(await page.locator('#route-activate').isDisabled(),true);
   assert.equal(await overflow(),false,'empty state overflow');
   report.push({size:size.name,errors,writes:writes.map(w=>({method:w.method,path:w.path})),a11y});
   console.log(JSON.stringify(report.at(-1)));
   assert.deepEqual(errors,[]);
   assert.ok(a11y.every(a=>a.violations.length===0),'Accessibility violations');
   await context.close();
  }
 }finally{await fs.writeFile(out+'/browser-results.json',JSON.stringify(report,null,2));await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
