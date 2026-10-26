// Keep DOM callbacks alive until native HTTP/body reads have settled on teardown.
const assert = require('node:assert/strict');

module.exports = function readonlyDomHarness(window, base) {
  const pending = new Set();
  const controllers = new Set();
  const timers = new Set();
  const intervals = new Set();
  let closing = false;
  const nativeTimeout = window.setTimeout.bind(window);
  const nativeClearTimeout = window.clearTimeout.bind(window);
  const nativeInterval = window.setInterval.bind(window);
  const nativeClearInterval = window.clearInterval.bind(window);
  const track = promise => {
    pending.add(promise);
    promise.then(() => pending.delete(promise), () => pending.delete(promise));
    return promise;
  };
  window.setTimeout = (callback, milliseconds, ...args) => {
    if (closing) return 0;
    const id = nativeTimeout(() => {timers.delete(id); if (!closing) callback(...args);}, milliseconds);
    timers.add(id);
    return id;
  };
  window.clearTimeout = id => {timers.delete(id); nativeClearTimeout(id);};
  window.setInterval = (callback, milliseconds, ...args) => {
    if (closing) return 0;
    const id = nativeInterval(callback, milliseconds, ...args);
    intervals.add(id);
    return id;
  };
  window.clearInterval = id => {intervals.delete(id); nativeClearInterval(id);};
  window.fetch = (url, options = {}) => {
    assert.ok(!options.method || options.method === 'GET', 'Live DOM checks strictly prohibit writes');
    if (closing) return track(Promise.reject(new Error('Read-only harness is shutting down')));
    const controller = new AbortController();
    controllers.add(controller);
    const timeout = setTimeout(() => controller.abort(), 18000);
    const finish = () => {clearTimeout(timeout); controllers.delete(controller);};
    if (options.signal?.aborted) controller.abort();
    else options.signal?.addEventListener('abort', () => controller.abort(), {once: true});
    return track(fetch(new URL(url, base), {...options, signal: controller.signal}).then(response => ({
      ok: response.ok, status: response.status,
      json: () => track(response.json().finally(finish)),
      text: () => track(response.text().finally(finish)),
    }), error => {finish(); throw error;}));
  };
  return async () => {
    closing = true;
    for (const id of timers) nativeClearTimeout(id);
    for (const id of intervals) nativeClearInterval(id);
    for (const controller of controllers) controller.abort();
    // Application catches/finally callbacks must run before jsdom removes document.
    do {
      await Promise.allSettled([...pending]);
      await new Promise(resolve => setImmediate(resolve));
    } while (pending.size);
    window.close();
  };
};
