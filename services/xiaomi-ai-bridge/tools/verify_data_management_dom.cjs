// Offline storage status and history pagination regression; no live device requests.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { JSDOM } = require(process.env.HUB_JSDOM_MODULE || 'jsdom');
const root = path.resolve(__dirname, '../src/xiaomi_ai_bridge/web');
const now = Date.now();
const at = minutes => new Date(now - minutes * 60000).toISOString();
const resource = { resource_id: 'test.sensor', kind: 'sensor', name: '测试温度计', state: { temperature: 25 }, observed_at: at(0), received_at: at(0), expires_at: at(-5), fields: [{ key: 'temperature', role: 'measurement', type: 'number', unit: '℃', status: 'ok', observed_at: at(0) }] };
const snapshot = { resources: [resource], events: [], sampling: { interval_seconds: 60, last_sample_at: at(0) } };
const makeEvent = (id, minute, value, status = 'ok') => ({ id, observed_at: at(minute), received_at: at(minute), state: { temperature: value }, fields: [{ key: 'temperature', role: 'measurement', type: 'number', status, observed_at: at(minute), attempted_at: at(minute) }] });
const status = { database: { bytes: 1048576, wal_bytes: 0, free_bytes: 4096 }, history: { event_count: 40, resource_count: 2, enabled_resource_count: 1, oldest_observed_at: at(1400), newest_observed_at: at(0) }, policy: { max_events_per_resource: 5000, max_age_days: 30, backup_interval_hours: 24, backup_keep_daily: 7 }, backup: { last_success_at: at(15), last_size_bytes: 800000 }, maintenance: { last_success_at: at(10) } };
const settle = () => new Promise(resolve => setTimeout(resolve, 30));

async function run() {
  const dom = new JSDOM(fs.readFileSync(path.join(root, 'index.html'), 'utf8'), { url: 'http://hub.test/#data', runScripts: 'outside-only', pretendToBeVisual: true });
  const w = dom.window;
  const query = selector => w.document.querySelector(selector);
  const requests = [];
  const sources = [];
  let storageFailure = false;
  let holdHistory = false;
  let releaseHistory = null;
  w.EventSource = class { constructor() { sources.push(this); } close() {} };
  w.fetch = async target => {
    const url = String(target);
    requests.push(url);
    let data = {};
    if (url === '/api/hub/storage') {
      if (storageFailure) throw new Error('连接中断');
      data = status;
    } else if (url === '/api/hub') data = snapshot;
    else if (url === '/api/status') data = { ready: true, started_at: at(20) };
    else if (url === '/api/home') data = { devices: [], audit: [] };
    else if (url.includes('/auth/')) data = { state: 'idle' };
    else if (url.includes('/history')) {
      assert.match(url, /projection=chart/);
      if (holdHistory) {
        holdHistory = false;
        await new Promise(resolve => { releaseHistory = resolve; });
      }
      const earlier = url.includes('before=cursor1');
      data = earlier
        ? { events: [makeEvent(1, 4, 20), makeEvent(2, 3, 21)], query: { has_more: false, next_cursor: null } }
        : { events: [makeEvent(3, 2, 22, 'unavailable'), makeEvent(4, 1, 24), makeEvent(5, 0, 25)], query: { has_more: true, truncated: true, next_cursor: 'cursor1' } };
    }
    return { ok: true, status: 200, json: async () => structuredClone(data) };
  };
  try {
    w.eval(fs.readFileSync(path.join(root, 'app.js'), 'utf8'));
    await settle();
    assert.equal(query('#hub-storage-events').textContent, '40');
    assert.match(query('#hub-storage-size').textContent, /1 MB/);
    assert.match(query('#hub-storage-retention').textContent, /5,000.*30 天/);
    assert.match(query('#hub-storage-backup-policy').textContent, /24 小时.*7 个/);
    assert.equal(query('#hub-storage-pill').textContent, '已备份');
    const count = () => requests.filter(url => url === '/api/hub/storage').length;
    assert.equal(count(), 1);
    w.showView('overview');
    w.showView('data');
    sources[0].onmessage({ data: JSON.stringify({ type: 'hub.snapshot', hub: snapshot }) });
    sources[0].onmessage({ data: JSON.stringify({ type: 'hub.update', resource }) });
    await settle();
    assert.equal(count(), 1, 'Cached navigation and SSE never repeat storage statistics');

    await w.selectHubResource(resource.resource_id);
    assert.equal(query('#hub-history-more').hidden, false);
    assert.match(query('#hub-history-meta').textContent, /还有更早记录/);
    query('#hub-history-more').click();
    await settle();
    assert.ok(requests.some(url => url.includes('before=cursor1')));
    assert.equal(query('#hub-history-more').hidden, true);
    assert.equal(query('#hub-history-list').children.length, 5);
    assert.equal((query('#hub-chart path').getAttribute('d').match(/M/g) || []).length, 2, 'Failed reading still leaves a gap after pagination');
    w.renderHub({ ...snapshot, resources: [{ ...resource, received_at: at(-1) }] });
    await settle();
    assert.equal(query('#hub-history-list').children.length, 5, 'Live updates preserve loaded earlier records without duplicates');
    assert.equal(query('#hub-history-more').hidden, true);

    w.renderHub({ ...snapshot, resources: [{ ...resource, received_at: at(-1), history_revision: 6 }] });
    await settle();
    assert.equal(query('#hub-history-list').children.length, 3, 'Backfill discards stale cached earlier pages before using the refreshed page');
    assert.equal(query('#hub-history-more').hidden, false, 'Backfill restores the refreshed page cursor so earlier history can be fetched again');
    assert.match(query('#hub-history-meta').textContent, /历史有补录，已重新读取最近记录/);
    query('#hub-history-metric').dispatchEvent(new w.Event('change'));
    assert.doesNotMatch(query('#hub-history-meta').textContent, /历史有补录/);
    query('#hub-history-more').click();
    await settle();
    assert.equal(query('#hub-history-list').children.length, 5, 'Earlier pages can be read anew after a backfill reset');
    assert.doesNotMatch(query('#hub-history-meta').textContent, /历史有补录/);

    query('#hub-history-hours').value = '168';
    query('#hub-history-hours').dispatchEvent(new w.Event('change'));
    await settle();
    assert.ok(requests.some(url => url.includes('hours=168')));
    assert.equal(query('#hub-history-list').children.length, 3, 'A new range resets pagination');
    assert.doesNotMatch(query('#hub-history-meta').textContent, /历史有补录/);

    w.renderHub(snapshot);
    await settle();
    const readsBeforeBackfill = requests.filter(url => url.includes('/history')).length;
    w.renderHub({ ...snapshot, resources: [{ ...resource, history_revision: 12 }] });
    await settle();
    assert.equal(requests.filter(url => url.includes('/history')).length, readsBeforeBackfill + 1, 'Historical backfill invalidates history even when current timestamps stay unchanged');

    holdHistory = true;
    const pending = w.loadHubHistory(resource, true);
    await settle();
    query('#hub-detail-close').click();
    const readsBeforeReopen = requests.filter(url => url.includes('/history')).length;
    await w.selectHubResource(resource.resource_id);
    assert.equal(requests.filter(url => url.includes('/history')).length, readsBeforeReopen + 1, 'Closing a pending request must allow reopening the same resource');
    releaseHistory();
    await pending;
    assert.doesNotMatch(query('#hub-history-meta').textContent, /正在更新/);
    assert.equal(query('#hub-history-list').children.length, 3);

    holdHistory = true;
    const removedPending = w.loadHubHistory(resource, true);
    await settle();
    w.renderHub({ ...snapshot, resources: [] });
    w.renderHub(snapshot);
    const readsBeforeRestore = requests.filter(url => url.includes('/history')).length;
    await w.selectHubResource(resource.resource_id);
    assert.equal(requests.filter(url => url.includes('/history')).length, readsBeforeRestore + 1, 'Removing then restoring a resource must clear abandoned loading state');
    releaseHistory();
    await removedPending;
    assert.doesNotMatch(query('#hub-history-meta').textContent, /正在更新/);

    storageFailure = true;
    query('#hub-storage-refresh').click();
    await settle();
    assert.equal(count(), 2);
    assert.equal(query('#hub-storage-events').textContent, '40', 'A status error preserves the previous readable snapshot');
    assert.match(query('#hub-storage-message').textContent, /连接中断.*保留上次/);
    assert.equal(query('#hub-storage-pill').textContent, '需要关注');
    assert.ok(requests.every(url => !/delete|purge|restore|control/.test(url)), 'Management view is read-only');
    console.log('PASS: storage visibility/cache/errors; read-only management; cursor pagination/deduplication; 7-day queries; preserved missing-data gaps; history backfill refresh; close/reopen and removal races.');
  } finally { w.close(); }
}
run().catch(error => { console.error(error); process.exitCode = 1; });
