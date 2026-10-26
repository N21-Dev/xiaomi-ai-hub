// Offline browser contracts. Every request is fulfilled with synthetic data.
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const {chromium} = require(process.env.HUB_PLAYWRIGHT_MODULE || 'playwright');
const AxeBuilder = process.env.HUB_AXE_MODULE ? require(process.env.HUB_AXE_MODULE).default : null;
const root = path.resolve(__dirname, '../src/xiaomi_ai_bridge/web');
const output = process.env.HUB_AUTH_BROWSER_OUTPUT || '/tmp/panel-auth-browser';
const base = 'http://panel.test';

(async () => {
  await fs.mkdir(output, {recursive: true});
  const files = Object.fromEntries(await Promise.all(['index.html','login.html','login.js','app.js','app.css'].map(async name => [name, await fs.readFile(path.join(root,name),'utf8')])));
  const browser = await chromium.launch({headless:true,args:['--no-sandbox']});
  const results = [];
  try {
    for (const viewport of [{name:'desktop',width:1440,height:1000},{name:'mobile',width:390,height:844}]) {
      const context = await browser.newContext({viewport:{width:viewport.width,height:viewport.height},locale:'zh-CN',reducedMotion:'reduce'});
      await context.addInitScript(() => {window.EventSource = class {close(){}};});
      let loggedIn = false;
      let configured = true;
      let expireNext = false;
      let chatWrites = 0;
      const errors = [];
      const loginBodies = [];
      const page = await context.newPage();
      page.on('pageerror',error => errors.push(error.message));
      await context.route('**/*', async route => {
        const request = route.request();
        const url = new URL(request.url());
        assert.equal(url.origin,base,'Tests never contact production or external services');
        const json = (body,status=200) => route.fulfill({status,contentType:'application/json',body:JSON.stringify(body)});
        if (url.pathname === '/api/auth/login') {
          const body = request.postDataJSON();loginBodies.push(body);
          if (body.username !== 'fixture-admin' || body.password !== 'fixture-password') return json({ok:false,error:'账号或密码不正确'},401);
          loggedIn = true;
          return json({ok:true,next:body.next});
        }
        if (url.pathname === '/api/auth/logout') {
          assert.equal(request.headers()['x-panel-csrf'],'synthetic-csrf');
          loggedIn = false;return json({ok:true});
        }
        if (url.pathname === '/login') return route.fulfill({contentType:'text/html',body:files['login.html']});
        const file = {'/app.css':'app.css','/login.js':'login.js','/app.js':'app.js'}[url.pathname];
        if (file) return route.fulfill({contentType:file.endsWith('.css')?'text/css':'application/javascript',body:files[file]});
        if (url.pathname === '/') {
          if (!loggedIn) return route.fulfill({status:302,headers:{Location:'/login'}});
          return route.fulfill({contentType:'text/html',body:files['index.html'].replace('__PANEL_CSRF__','synthetic-csrf').replace('__PANEL_USER__','fixture-admin')});
        }
        if (url.pathname === '/api/auth/session') return json({enabled:true,configured,authenticated:loggedIn});
        if (!loggedIn || expireNext) {
          expireNext = false;loggedIn = false;
          return json({ok:false,code:'panel_auth_required',error:'请先登录管理面板'},401);
        }
        if (request.method() !== 'GET') assert.equal(request.headers()['x-panel-csrf'],'synthetic-csrf');
        if (url.pathname === '/api/actions/chat') {chatWrites++;return json({ok:true,answer:'模拟回答'});}
        if (url.pathname === '/api/status') return json({started_at:new Date().toISOString(),ready:true});
        if (url.pathname === '/api/hub') return json({resources:[],events:[]});
        if (url.pathname === '/api/home') return json({devices:[],audit:[]});
        if (url.pathname === '/api/automations') return json({tasks:[]});
        if (url.pathname.includes('/auth/')) return json({state:'idle'});
        return json({ok:true});
      });
      configured = false;
      await page.goto(base+'/login');
      await page.locator('#panel-setup-status').waitFor({state:'visible'});
      assert.equal(await page.locator('#panel-login-submit').isDisabled(),true);
      assert.equal(await page.locator('#panel-username').inputValue(),'');
      assert.equal(await page.locator('#panel-password').inputValue(),'');
      configured = true;
      await page.goto(base+'/login?next='+encodeURIComponent('/#chat'));
      await page.screenshot({path:path.join(output,`login-clean-${viewport.name}.png`),fullPage:true});
      await page.locator('#panel-username').fill('fixture-admin');
      await page.locator('#panel-password').fill('wrong-fixture');
      await page.locator('#panel-password-toggle').click();
      assert.equal(await page.locator('#panel-password').getAttribute('type'),'text');
      await page.locator('#panel-password-toggle').click();
      assert.equal(await page.locator('#panel-password').getAttribute('type'),'password');
      await page.locator('#panel-password').press('Enter');
      await page.locator('#panel-login-error').waitFor({state:'visible'});
      assert.match(await page.locator('#panel-login-error').innerText(),/账号或密码/);
      assert.equal(new URL(page.url()).pathname,'/login');
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth>innerWidth),false);
      let accessibility = null;
      if (AxeBuilder) {
        const audit = await new AxeBuilder({page}).withTags(['wcag2a','wcag2aa','wcag21aa']).analyze();
        accessibility = audit.violations.map(issue => ({id:issue.id,count:issue.nodes.length}));
        assert.deepEqual(accessibility,[]);
      }
      await page.screenshot({path:path.join(output,`login-${viewport.name}.png`),fullPage:true});
      await page.locator('#panel-password').fill('fixture-password');
      await page.locator('#panel-password').press('Enter');
      await page.waitForURL(base+'/#chat');
      await page.locator('#panel-account').waitFor({state:'visible'});
      assert.equal(await page.locator('#panel-account-name').innerText(),'fixture-admin');
      await page.locator('#chat-prompt').fill('模拟问题');
      await page.locator('#chat-prompt').press('Enter');
      await page.getByText('模拟回答',{exact:true}).waitFor();
      assert.equal(chatWrites,1);
      assert.equal(await page.evaluate(() => localStorage.length),0);
      await page.locator('#panel-logout').click();
      await page.waitForURL(/\/login/);
      await page.locator('#panel-username').fill('fixture-admin');
      await page.locator('#panel-password').fill('fixture-password');
      await page.locator('#panel-password').press('Enter');
      await page.waitForURL(base+'/#chat');
      expireNext = true;
      await page.evaluate(() => {void refreshAll(true);});
      await page.waitForURL(/\/login\?next=/);
      assert.ok(loginBodies.every(body => !body.next || body.next.startsWith('/')));
      assert.deepEqual(errors,[]);
      results.push({viewport:viewport.name,wrongPassword:true,enterLogin:true,chatCsrf:true,logout:true,expiredSessionRedirect:true,overflow:false,pageErrors:errors.length,accessibility});
      await context.close();
    }
  } finally {await browser.close();}
  await fs.writeFile(path.join(output,'results.json'),JSON.stringify(results,null,2));
  console.log(JSON.stringify({ok:true,results,output}));
})().catch(error=>{console.error(error);process.exitCode=1;});
