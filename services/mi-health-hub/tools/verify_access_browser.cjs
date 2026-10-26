// All health data and access keys in this test are synthetic; no network access.
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const {chromium} = require(process.env.HUB_PLAYWRIGHT_MODULE || 'playwright');
const root = path.resolve(__dirname, '../src/mi_health_hub/web');

(async () => {
  const html = await fs.readFile(path.join(root, 'index.html'), 'utf8');
  const chart = await fs.readFile(path.join(root, 'vendor/chart.umd.min.js'));
  const browser = await chromium.launch({headless: true, args: ['--no-sandbox']});
  const results = [];
  try {
    for (const width of [390, 1440]) {
      const context = await browser.newContext({viewport: {width, height: 900}, locale: 'zh-CN'});
      const page = await context.newPage();
      const errors = [];
      page.on('pageerror', error => errors.push(error.message));
      let authorized = false;
      await context.route('**/*', route => {
        const request = route.request();
        const url = new URL(request.url());
        assert.equal(url.origin, 'http://health.test');
        assert.equal(request.method(), 'GET');
        if (url.pathname === '/') return route.fulfill({contentType: 'text/html', body: html});
        if (url.pathname === '/vendor/chart.umd.min.js') return route.fulfill({contentType: 'application/javascript', body: chart});
        assert.equal(url.searchParams.has('key'), false);
        if (request.headers()['x-api-key'] !== 'fixture-health-access') {
          return route.fulfill({status: 401, json: {detail: 'API key required'}});
        }
        authorized = true;
        let data = {};
        if (url.pathname === '/api/stats/period') data = {current: {steps_avg: 1000, calories_avg: 50}, workouts: [], hr_curve: {labels: [], bpm: []}};
        else if (url.pathname.startsWith('/api/series/')) data = {points: []};
        else if (url.pathname === '/api/summary') data = {sleep: null};
        return route.fulfill({json: data});
      });
      await page.goto('http://health.test/');
      await page.locator('#health-access-dialog').waitFor({state: 'visible'});
      await page.locator('#health-access-cancel').click();
      await page.locator('#health-access-open').click();
      await page.locator('#health-access-key').fill('wrong-fixture-key');
      await page.locator('#health-access-form button[type=submit]').click();
      await page.locator('#health-access-dialog').waitFor({state: 'visible'});
      assert.equal(authorized, false);
      await page.locator('#health-access-key').fill('fixture-health-access');
      await page.locator('#health-access-form button[type=submit]').click();
      await page.locator('#daily-kpis .kpi').first().waitFor();
      assert.equal(authorized, true);
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
      assert.equal(await page.evaluate(() => localStorage.length), 0);
      assert.equal(await page.evaluate(() => sessionStorage.getItem('health-api-key')), 'fixture-health-access');
      await page.locator('#health-access-forget').click();
      await page.locator('#health-access-dialog').waitFor({state: 'visible'});
      assert.equal(await page.evaluate(() => sessionStorage.getItem('health-api-key')), null);
      assert.deepEqual(errors, []);
      results.push({width, accessPrompt: true, wrongKeyRejected: true, headerOnly: true, sessionOnly: true, clearKey: true, overflow: false, pageErrors: 0});
      await context.close();
    }
  } finally { await browser.close(); }
  console.log(JSON.stringify({ok: true, results}));
})().catch(error => { console.error(error); process.exitCode = 1; });
