// 聊天输入框的键盘行为回归：Enter 发送、Shift+Enter 换行、输入法组字期间不发送。
// 只跑 DOM，不发真实请求。
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {JSDOM} = require(process.env.HUB_JSDOM_MODULE || 'jsdom');

const root = path.resolve(__dirname, '../src/xiaomi_ai_bridge/web');
const html = fs.readFileSync(path.join(root, 'index.html'), 'utf8');
const script = fs.readFileSync(path.join(root, 'app.js'), 'utf8');
const settle = () => new Promise(resolve => setTimeout(resolve, 25));

async function run() {
  const dom = new JSDOM(html, {url: 'http://hub.test/#chat', runScripts: 'outside-only', pretendToBeVisual: true});
  const w = dom.window;
  const chatCalls = [];
  const outerScrollCalls = [];
  const chatScrollCalls = [];

  w.EventSource = class {close() {}};
  // jsdom 没有实现 matchMedia，滚动行为判断会直接抛错
  w.matchMedia = w.matchMedia || (() => ({matches: false, addEventListener() {}, removeEventListener() {}}));
  w.HTMLElement.prototype.scrollIntoView = function () { outerScrollCalls.push(this.id || this.className || this.tagName); };
  w.HTMLElement.prototype.scrollTo = function (options) {
    if (this.id === 'chat-log') chatScrollCalls.push(options);
  };
  w.fetch = async (target, options = {}) => {
    const url = String(target);
    if (url === '/api/actions/chat') chatCalls.push(JSON.parse(options.body || '{}'));
    let data = {};
    if (url === '/api/status') data = {started_at: new Date().toISOString(), ready: true};
    else if (url === '/api/hub') data = {resources: [], events: []};
    else if (url === '/api/home') data = {devices: [], audit: []};
    else if (url.includes('/automations')) data = {tasks: [], items: []};
    else if (url === '/api/actions/chat') data = {answer: '好的'};
    return {ok: true, status: 200, json: async () => structuredClone(data)};
  };

  try {
    w.eval(script);
    await settle();

    const input = w.document.querySelector('#chat-prompt');
    const send = (init = {}) => input.dispatchEvent(
      new w.KeyboardEvent('keydown', {key: 'Enter', bubbles: true, cancelable: true, ...init}),
    );

    // 1) 输入框必须是无障碍可达的 textarea
    assert.ok(input, '聊天输入框必须存在');
    assert.equal(input.tagName, 'TEXTAREA');

    // 2) Shift + Enter 只换行，不发送
    input.value = '第一行';
    send({shiftKey: true});
    await settle();
    assert.equal(chatCalls.length, 0, 'Shift+Enter 不能发送，要留给换行');

    // 3) 输入法组字期间的 Enter 不能发送
    input.value = 'nihao';
    const composing = new w.KeyboardEvent('keydown', {key: 'Enter', bubbles: true, cancelable: true});
    Object.defineProperty(composing, 'isComposing', {value: true});
    input.dispatchEvent(composing);
    await settle();
    assert.equal(chatCalls.length, 0, '中文选词按 Enter 不能把半截拼音发出去');

    // 4) 空白内容不发送
    input.value = '   ';
    send();
    await settle();
    assert.equal(chatCalls.length, 0, '空白输入不应发送');

    // 5) 普通 Enter 直接发送
    input.value = '客厅的灯开着吗';
    send();
    await settle();
    await settle();
    assert.equal(chatCalls.length, 1, 'Enter 应该发送');
    assert.equal(chatCalls[0].prompt, '客厅的灯开着吗', '发送内容应与输入一致');
    assert.equal(input.value, '', '发送后清空输入框');
    assert.equal(outerScrollCalls.length, 0, '发送消息不能滚动外层页面');
    assert.ok(chatScrollCalls.length >= 1, '发送消息应只滚动聊天记录容器');

    // 6) Ctrl + Enter 仍然有效（老习惯）
    input.value = '再问一次';
    send({ctrlKey: true});
    await settle();
    await settle();
    assert.equal(chatCalls.length, 2, 'Ctrl+Enter 仍应发送');

    console.log('PASS: 聊天输入框 Enter 发送、Shift+Enter 换行、输入法组字期间不误发、空白不发送、Ctrl+Enter 兼容。');
  } finally {
    w.close();
  }
}

run().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
