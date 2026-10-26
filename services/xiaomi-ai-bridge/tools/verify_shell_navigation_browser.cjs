// Offline route/detail scrolling contracts. No production requests or mutations.
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const {execFileSync} = require('node:child_process');
const engines = require(process.env.HUB_PLAYWRIGHT_MODULE || 'playwright');
const root = process.env.HUB_PLATFORM_ROOT || path.resolve(__dirname, '../../..');
const output = process.env.HUB_SCROLL_OUTPUT || '/tmp/shell-navigation-check';
const baseline = process.env.HUB_SCROLL_BASELINE_REF;
const read = async file => baseline
  ? execFileSync('git', ['show', `${baseline}:${file}`], {cwd: root, encoding: 'utf8'})
  : fs.readFile(path.join(root, file), 'utf8');
const bridgePath = 'services/xiaomi-ai-bridge/src/xiaomi_ai_bridge/web';
const healthPath = 'services/mi-health-hub/src/mi_health_hub/web';
const now = new Date().toISOString();
const devices = Array.from({length: 16}, (_, i) => ({
  key: `fixture-${i}`, alias: `测试台灯 ${i + 1}`, name: `测试台灯 ${i + 1}`,
  category: 'light', category_label: '灯', enabled: false, cloud_online: true,
  online: true, stale: false, observed_at: now, state: {on: '关闭', brightness: 50},
  properties: [], controls: [], actions: ['get_state'],
}));
const resources = devices.map(d => ({
  resource_id: `home.light.${d.key}`, name: d.name, kind: 'service',
  state: d.state, observed_at: now, received_at: now,
  expires_at: new Date(Date.now() + 3600000).toISOString(), online: true, stale: false,
}));
const current = Object.fromEntries([
  'calories_total', 'calories_avg', 'steps_total', 'steps_avg', 'active_min_total',
  'active_min_avg', 'heart_rate_observed_min', 'sleep_min_avg', 'nap_min_total',
  'vigorous_min_total', 'vigorous_min_avg', 'workout_count', 'workout_min_total',
  'workout_distance_m', 'rhr_avg',
].map(key => [key, 0]));
current.days = 7;
const series = Object.fromEntries([
  'labels', 'hr_avg', 'steps', 'vigorous_min', 'calories', 'active_min',
  'sleep_deep', 'sleep_rem', 'sleep_light', 'nap_min',
].map(key => [key, []]));
const stats = {
  current, previous: {...current}, series, series_prev: {hr_avg: []},
  start: '2026-10-12', end: '2026-10-18', as_of: '2026-10-18', days: 7,
  previous_range: {start: '2026-10-05', end: '2026-10-11', comparison_end: '2026-10-11'},
  hr_curve: {labels: [], bpm: []}, workouts: [],
};

async function fixturePage(browser, viewport, surface, files, reducedMotion) {
  const page = await browser.newPage({viewport, reducedMotion, locale: 'zh-CN'});
  page.setDefaultTimeout(5000);
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.addInitScript(() => {window.EventSource = class {close() {}};});
  await page.route('**/*', route => {
    const request = route.request(), url = new URL(request.url());
    assert.equal(url.origin, 'http://scroll.test', 'Never contact real services');
    assert.equal(request.method(), 'GET', 'Only simulated read requests are allowed');
    if (surface === 'health') {
      if (url.pathname === '/') return route.fulfill({contentType: 'text/html', body: files.health});
      if (url.pathname === '/vendor/chart.umd.min.js') return route.fulfill({contentType: 'application/javascript', body: files.chart});
      let data = {};
      if (url.pathname === '/api/admin/status') data = {version: 'synthetic'};
      else if (url.pathname === '/api/stats/period') data = stats;
      else if (url.pathname.startsWith('/api/series/')) data = {points: []};
      else if (url.pathname === '/api/summary') data = {sleep: null, sleep_stages: []};
      return route.fulfill({json: data});
    }
    const file = {'/': 'index.html', '/app.css': 'app.css', '/app.js': 'app.js'}[url.pathname];
    if (file) return route.fulfill({
      contentType: file.endsWith('.css') ? 'text/css' : file.endsWith('.js') ? 'application/javascript' : 'text/html',
      body: files.bridge[file].replace('__PANEL_USER__', 'fixture-admin').replace('__PANEL_CSRF__', 'synthetic-csrf'),
    });
    let data = {};
    if (url.pathname === '/api/home') data = {devices, audit: []};
    else if (url.pathname === '/api/hub') data = {resources, events: [], sampling: {interval_seconds: 60}};
    else if (url.pathname.includes('/history')) data = {events: [], query: {has_more: false}};
    else if (url.pathname === '/api/status') data = {ready: true, started_at: now};
    else if (url.pathname === '/api/automations') data = {tasks: []};
    else if (url.pathname.includes('/auth/')) data = {state: 'idle'};
    return route.fulfill({json: data});
  });
  return {page, errors};
}

async function healthCheck(browser, viewport, files, reducedMotion) {
  const {page, errors} = await fixturePage(browser, viewport, 'health', files, reducedMotion);
  try {
    await page.goto('http://scroll.test/');
    await page.waitForSelector('#daily-kpis .kpi');
    await page.evaluate(() => scrollTo(0, 700));
    await page.locator('#tabs a[data-page="weekly"]').click();
    await page.waitForSelector('#weekly-kpis .kpi');
    const changed = await page.evaluate(() => ({
      y: scrollY,
      headingTop: document.querySelector('section.page.on h1').getBoundingClientRect().top,
      headerBottom: document.querySelector('.topbar').getBoundingClientRect().bottom,
      banner: document.querySelector('#banner-slot').textContent,
    }));
    assert.equal(changed.banner, '');
    assert.ok(changed.y <= 1, 'Changing sections must leave the previous reading position');
    assert.ok(changed.headingTop >= changed.headerBottom, 'New section starts below the sticky header');
    await page.evaluate(() => scrollTo(0, 500));
    const beforeRefresh = await page.evaluate(() => scrollY);
    await page.evaluate(() => renderPage('weekly'));
    assert.equal(await page.evaluate(() => scrollY), beforeRefresh, 'Refreshing the same section preserves reading position');
    await page.evaluate(() => shiftPeriod(-1));
    await page.waitForFunction(() => document.querySelector('#weekly-kpis .kpi'));
    assert.equal(await page.evaluate(() => scrollY), beforeRefresh, 'Changing the date does not jump the page');
    // Exercise the hashchange path separately from clicking the navigation.
    await page.evaluate(() => {location.hash = '#/monthly';});
    await page.waitForSelector('#monthly-kpis .kpi');
    assert.ok(await page.evaluate(() => scrollY <= 1), 'Hash navigation resets section scroll');
    await page.screenshot({path: path.join(output, `${files.engine}-${viewport.width}-${reducedMotion}-health.png`)});
    assert.deepEqual(errors, []);
    return {changed, sameSectionScroll: beforeRefresh};
  } finally {await page.close();}
}

async function bridgeCheck(browser, viewport, files, reducedMotion) {
  const {page, errors} = await fixturePage(browser, viewport, 'bridge', files, reducedMotion);
  try {
    await page.goto('http://scroll.test/#home');
    await page.waitForSelector('.home-device-card');
    // Extra content below makes start alignment possible even for short fixtures.
    await page.evaluate(() => {
      for (const id of ['home-device-detail', 'hub-detail']) {
        const tail = document.createElement('div');
        tail.style.height = '1400px';
        document.getElementById(id).after(tail);
      }
    });
    const positions = [];
    for (const target of ['home-device-detail', 'hub-detail']) {
      if (target === 'home-device-detail') {
        await page.locator('.home-device-open').first().click();
      } else {
        await page.evaluate(async () => {
          showView('data');
          await selectHubResource('home.light.fixture-0', true);
        });
      }
      await page.waitForFunction(id => {
        const element = document.getElementById(id);
        const header = document.querySelector(innerWidth <= 1100 ? '.topbar' : '.workspace-bar');
        const delta = element.getBoundingClientRect().top - header.getBoundingClientRect().bottom;
        // View entrance animations can shift the final edge by a few pixels.
        return delta >= 16 && delta <= 40;
      }, target);
      const position = await page.evaluate(id => {
        const element = document.getElementById(id);
        const header = document.querySelector(innerWidth <= 1100 ? '.topbar' : '.workspace-bar');
        const headerBottom = header.getBoundingClientRect().bottom;
        const controls = [...element.querySelectorAll('.section-heading button')].filter(button => button.getBoundingClientRect().height);
        return {
          target: id, top: element.getBoundingClientRect().top, headerBottom,
          controlsVisible: controls.every(button => {
            const r = button.getBoundingClientRect();
            return r.top >= headerBottom && button.contains(document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2));
          }),
          width: document.documentElement.scrollWidth,
        };
      }, target);
      assert.ok(position.controlsVisible, 'Detail actions must be visible and receive pointer hits');
      assert.ok(position.width <= viewport.width + 1);
      positions.push(position);
    }
    await page.screenshot({path: path.join(output, `${files.engine}-${viewport.width}-${reducedMotion}-bridge.png`)});
    await page.evaluate(() => {
      showView('connection');
      scrollTo(0, 0);
      document.querySelector('.skip-link').focus();
    });
    await page.keyboard.press('Enter');
    const skip = await page.evaluate(() => ({
      headingTop: document.querySelector('#view-connection .page-head').getBoundingClientRect().top,
      headerBottom: document.querySelector(innerWidth <= 1100 ? '.topbar' : '.workspace-bar').getBoundingClientRect().bottom,
    }));
    assert.ok(skip.headingTop >= skip.headerBottom - 1, 'Skip link does not place the page heading behind the header');
    assert.deepEqual(errors, []);
    return {positions, skip};
  } finally {await page.close();}
}

(async () => {
  await fs.mkdir(output, {recursive: true});
  const files = {
    bridge: Object.fromEntries(await Promise.all(['index.html', 'app.css', 'app.js'].map(async file => [file, await read(`${bridgePath}/${file}`)]))),
    health: await read(`${healthPath}/index.html`),
    chart: await fs.readFile(path.join(root, healthPath, 'vendor/chart.umd.min.js')),
  };
  const results = [];
  for (const engine of (process.env.HUB_SCROLL_ENGINES || 'chromium,webkit').split(',')) {
    files.engine = engine;
    const browser = await engines[engine].launch({
      headless: true,
      ...(engine === 'chromium' ? {args: ['--no-sandbox']} : {}),
      ...(engine === 'webkit' && process.env.HUB_WEBKIT_EXECUTABLE ? {executablePath: process.env.HUB_WEBKIT_EXECUTABLE} : {}),
    });
    try {
      for (const viewport of [{width: 375, height: 844}, {width: 390, height: 844}, {width: 844, height: 390}, {width: 1024, height: 900}, {width: 1100, height: 900}, {width: 1101, height: 900}, {width: 1536, height: 1000}]) {
        if (process.env.HUB_SCROLL_WIDTHS && !process.env.HUB_SCROLL_WIDTHS.split(',').map(Number).includes(viewport.width)) continue;
        const reducedMotion = viewport.width === 1536 ? 'no-preference' : 'reduce';
        for (const [surface, check] of [['health', healthCheck], ['bridge', bridgeCheck]]) {
          try {
            const details = await check(browser, viewport, files, reducedMotion);
            results.push({engine, surface, viewport, passed: true, ...details});
          } catch (error) {
            results.push({engine, surface, viewport, passed: false, error: error.message});
          }
          console.log(JSON.stringify(results.at(-1)));
        }
      }
    } finally {await browser.close();}
  }
  await fs.writeFile(path.join(output, 'results.json'), JSON.stringify(results, null, 2) + '\n');
  assert.ok(results.every(result => result.passed), 'Shell navigation regression checks failed');
})().catch(error => {console.error(error); process.exitCode = 1;});
