// Synthetic browser checks for root panning and nested horizontal scrollers.
const fs = require('node:fs/promises');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const engines = require(process.env.HUB_PLAYWRIGHT_MODULE || 'playwright');
const root = path.resolve(__dirname, '..');
const output = process.env.HUB_SCROLL_OUTPUT || '/tmp/panel-mobile-scroll';
const observe = process.env.HUB_SCROLL_OBSERVE === '1';

(async () => {
  await fs.mkdir(output,{recursive:true});
  const source = await fs.readFile(path.join(root,'tools/verify_model_console_dom.cjs'),'utf8');
  const fixture = JSON.parse(JSON.stringify(vm.runInNewContext(source.slice(source.indexOf('const now ='),source.indexOf('const settle ='))+';({registry,usage,probes,logs});')));
  fixture.registry.providers[0].name = 'Provider'+'UnbrokenName'.repeat(10);
  fixture.registry.fallback = [
    {provider_id:'env-default',model:'vendor/'+ 'UnbrokenModelIdentifier'.repeat(9),available:true},
    {provider_id:'env-default',model:'vendor/reasoning-model-with-a-long-context-window',available:true},
  ];
  const files = Object.fromEntries(await Promise.all(['index.html','app.css','app.js','login.html','login.js'].map(async name => [name,await fs.readFile(path.join(root,'src/xiaomi_ai_bridge/web',name),'utf8')])));
  const report = [];
  for (const engineName of (process.env.HUB_SCROLL_ENGINES || 'chromium,webkit').split(',')) {
    const browser = await engines[engineName].launch({headless:true,...(engineName==='chromium'?{args:['--no-sandbox']} : process.env.HUB_WEBKIT_EXECUTABLE ? {executablePath:process.env.HUB_WEBKIT_EXECUTABLE}: {})});
    try {
      for (const width of [320,390,768,1536]) {
        const page = await browser.newPage({viewport:{width,height:844},isMobile:width<1000,hasTouch:width<1000,locale:'zh-CN',reducedMotion:'reduce'});
        const errors = [];
        page.on('pageerror',error=>errors.push(error.message));
        await page.addInitScript(()=>{window.EventSource=class{close(){}};});
        await page.route('**/*',async route=>{
          const request=route.request(),url=new URL(request.url());
          assert.equal(url.origin,'http://scroll.test');
          assert.equal(request.method(),'GET','No device, model, or account mutations');
          const name={'/':'index.html','/app.css':'app.css','/app.js':'app.js','/login':'login.html','/login.js':'login.js'}[url.pathname];
          if (name) return route.fulfill({contentType:name.endsWith('.html')?'text/html':name.endsWith('.css')?'text/css':'application/javascript',body:files[name].replace('__PANEL_CSRF__','synthetic-csrf').replace('__PANEL_USER__','mobile-admin')});
          let data={};
          if(url.pathname==='/api/models')data=fixture.registry;
          else if(url.pathname.endsWith('/usage'))data=fixture.usage;
          else if(url.pathname.endsWith('/logs'))data=fixture.logs;
          else if(url.pathname.endsWith('/probes'))data=fixture.probes;
          else if(url.pathname==='/api/home')data={devices:[],audit:[]};
          else if(url.pathname==='/api/hub')data={resources:[],events:[]};
          else if(url.pathname==='/api/status')data={ready:true,started_at:new Date().toISOString()};
          return route.fulfill({json:data});
        });
        await page.goto('http://scroll.test/#services');
        await page.waitForSelector('.route-config');
        await page.waitForTimeout(200);
        const geometry = await page.evaluate(()=>({
          viewport:innerWidth,documentWidth:document.documentElement.scrollWidth,
          rootPolicy:getComputedStyle(document.documentElement).overscrollBehaviorX,
          bodyPolicy:getComputedStyle(document.body).overscrollBehaviorX,
          cardRight:document.querySelector('.cs-card-accent').getBoundingClientRect().right,
          providerFits:document.querySelector('#gateway-active-provider').scrollWidth<=document.querySelector('#gateway-active-provider').clientWidth+1,
          fallbackFits:document.querySelector('#gateway-fallback-chain').scrollWidth<=document.querySelector('#gateway-fallback-chain').clientWidth+1,
          accountVisible:!document.querySelector('#panel-account').hidden,
          navPolicy:getComputedStyle(document.querySelector('.tabs')).overscrollBehaviorX,
        }));
        if(!observe){
          assert.ok(geometry.documentWidth<=width+1,'Root must fit even with unbroken provider and model identifiers');
          assert.ok(geometry.cardRight<=width+1,'Cards must shrink inside the layout, rather than be clipped');
          assert.ok(geometry.providerFits && geometry.fallbackFits,'Long identifiers wrap inside their cards');
          assert.equal(geometry.rootPolicy,'none','The viewport must suppress horizontal elastic overscroll');
          assert.equal(geometry.bodyPolicy,'none');
          assert.equal(geometry.navPolicy,'contain','Navigation must not chain its edge gesture to the page');
          assert.equal(geometry.accountVisible,true);
        }
        const scrollState = await page.evaluate(()=>{
          const nav=document.querySelector('.tabs');
          nav.scrollLeft=nav.scrollWidth;
          const navOffset=nav.scrollLeft;
          const table=document.querySelector('#csview-overview .cs-table-wrap');
          table.scrollLeft=120;
          return {navOffset,navNeedsScroll:nav.scrollWidth>nav.clientWidth,tableOffset:table.scrollLeft,tableNeedsScroll:table.scrollWidth>table.clientWidth};
        });
        const nativeWheel = engineName !== 'webkit' || width>=1000;
        if(nativeWheel){
          await page.mouse.move(Math.min(width-25,180),450);
          await page.mouse.wheel(120,220);
          await page.waitForTimeout(120);
          await page.mouse.wheel(-120,220);
        }else{
          // Playwright mobile WebKit cannot synthesize a native wheel/swipe.
          // Check its layout/edge policy and that vertical scrolling works.
          await page.evaluate(()=>scrollBy(0,440));
        }
        await page.waitForTimeout(120);
        const gestures = await page.evaluate(()=>({x:scrollX,y:scrollY,headerLeft:document.querySelector('.topbar').getBoundingClientRect().left,headerTop:document.querySelector('.topbar').getBoundingClientRect().top}));
        if(!observe){
          if(scrollState.navNeedsScroll)assert.ok(scrollState.navOffset>0,'Navigation still scrolls horizontally');
          if(scrollState.tableNeedsScroll)assert.ok(scrollState.tableOffset>0,'Tables still scroll horizontally');
          assert.equal(gestures.x,0,'Diagonal gestures must not pan the document');
          assert.equal(gestures.headerLeft,0,'The sticky header remains aligned');
          assert.equal(gestures.headerTop,0,'Vertical scrolling must preserve the sticky header');
          assert.ok(gestures.y>0,'Vertical scrolling remains available');
        }
        for(const view of ['channels','models','logs','usage','overview']){
          await page.evaluate(name=>showConsoleView(name),view);
          const widthNow=await page.evaluate(()=>document.documentElement.scrollWidth);
          if(!observe)assert.ok(widthNow<=width+1,`${view} stays inside the viewport`);
        }
        await page.evaluate(()=>{scrollTo(0,0);});
        await page.screenshot({path:path.join(output,`${engineName}-${width}.png`),fullPage:width<1000});
        await page.goto('http://scroll.test/login');
        const loginWidth=await page.evaluate(()=>document.documentElement.scrollWidth);
        if(!observe){assert.ok(loginWidth<=width+1);assert.deepEqual(errors,[]);}
        const result={engine:engineName,width,geometry,scrollState,gestures,nativeWheel,loginWidth,pageErrors:errors.length};
        report.push(result);console.log(JSON.stringify(result));
        await page.close();
      }
    } finally {await browser.close();}
  }
  await fs.writeFile(path.join(output,'results.json'),JSON.stringify(report,null,2));
})().catch(error=>{console.error(error);process.exitCode=1;});
