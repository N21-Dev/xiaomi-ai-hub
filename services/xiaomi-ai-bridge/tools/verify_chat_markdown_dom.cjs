// 聊天 Markdown 渲染回归：结构、转义、链接白名单。
// 只跑 DOM，不发真实请求。
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {JSDOM} = require(process.env.HUB_JSDOM_MODULE || 'jsdom');

const root = path.resolve(__dirname, '../src/xiaomi_ai_bridge/web');
const html = fs.readFileSync(path.join(root, 'index.html'), 'utf8');
const script = fs.readFileSync(path.join(root, 'app.js'), 'utf8');

const SAMPLE = [
  '## 客厅状态',
  '',
  '灯是**开着的**，亮度 `62%`，模式 _阅读_。',
  '',
  '- 台灯 · 已开',
  '- 落地灯 · 关闭',
  '  - 备注：昨天换过灯泡',
  '',
  '1. 先关灯',
  '2. 再拉窗帘',
  '',
  '> 这条是引用',
  '',
  '```json',
  '{"power": "on", "brightness": 62}',
  '```',
  '',
  '| 设备 | 状态 |',
  '| --- | --- |',
  '| 台灯 | 开 |',
  '| 空调 | 关 |',
  '',
  '---',
  '',
  '详情见 [米家](https://example.com/a?b=1)，或直接访问 https://example.com/x 。',
  '',
  '~~旧的~~ 说法要改。',
].join('\n');

async function run() {
  const dom = new JSDOM(html, {url: 'http://hub.test/#chat', runScripts: 'outside-only', pretendToBeVisual: true});
  const w = dom.window;

  w.EventSource = class {close() {}};
  w.matchMedia = w.matchMedia || (() => ({matches: false, addEventListener() {}, removeEventListener() {}}));
  w.HTMLElement.prototype.scrollTo = function () {};
  w.HTMLElement.prototype.scrollIntoView = function () {};
  w.fetch = async () => ({ok: true, status: 200, json: async () => ({})});

  w.eval(script);
  await new Promise(resolve => setTimeout(resolve, 25));

  const doc = w.document;
  const render = (markdown) => {
    const host = doc.createElement('div');
    w.renderChatMarkdown(host, markdown);
    return host;
  };

  const box = render(SAMPLE);

  // 1) 标题
  const h2 = box.querySelector('h2');
  assert.ok(h2, '二级标题应渲染为 h2');
  assert.equal(h2.textContent, '客厅状态');

  // 2) 行内：粗体、行内代码、斜体、删除线
  assert.equal(box.querySelector('strong').textContent, '开着的', '**粗体** 应转成 strong');
  assert.equal(box.querySelector('code.md-inline-code').textContent, '62%');
  assert.equal(box.querySelector('em').textContent, '阅读');
  assert.equal(box.querySelector('del').textContent, '旧的');

  // 3) 无序列表 + 缩进项
  const ul = box.querySelector('ul');
  assert.ok(ul, '无序列表应渲染为 ul');
  assert.equal(ul.querySelectorAll('li').length, 3, '两条主项 + 一条缩进项');
  assert.ok(ul.querySelector('li.md-nested'), '缩进项要带 md-nested');

  // 4) 有序列表
  const ol = box.querySelector('ol');
  assert.ok(ol, '有序列表应渲染为 ol');
  assert.equal(ol.querySelectorAll('li').length, 2);

  // 5) 引用
  const quote = box.querySelector('blockquote');
  assert.ok(quote, '引用应渲染为 blockquote');
  assert.match(quote.textContent, /这条是引用/);

  // 6) 围栏代码块：原样保留，内部不解析 Markdown
  const pre = box.querySelector('pre.md-code');
  assert.ok(pre, '围栏代码应渲染为 pre.md-code');
  assert.equal(pre.dataset.lang, 'json', '语言标记要留在 data-lang 上');
  assert.equal(pre.querySelector('code').textContent, '{"power": "on", "brightness": 62}');
  assert.equal(pre.querySelectorAll('strong, em, a').length, 0, '代码块里不能解析行内标记');

  // 7) 表格
  const table = box.querySelector('table.md-table');
  assert.ok(table, '表格应渲染为 table.md-table');
  assert.equal(table.querySelectorAll('thead th').length, 2);
  assert.equal(table.querySelectorAll('tbody tr').length, 2, '分隔行不能被当成数据行');
  assert.equal(table.querySelectorAll('tbody td')[3].textContent, '关');

  // 8) 分隔线
  assert.ok(box.querySelector('hr'), '--- 应渲染为 hr');

  // 9) 链接：合法链接可点，裸网址自动成链
  const links = [...box.querySelectorAll('a.md-link')];
  assert.equal(links.length, 2, '[文字](链接) 与裸网址各一条');
  assert.equal(links[0].getAttribute('href'), 'https://example.com/a?b=1');
  assert.equal(links[0].getAttribute('rel'), 'noopener noreferrer');
  assert.equal(links[1].getAttribute('href'), 'https://example.com/x');

  // 10) 安全：HTML 不能变成节点，javascript: 链接不能成为 <a>
  const evil = render('看这个 <img src=x onerror="alert(1)"> 和 [点我](javascript:alert(1))');
  assert.equal(evil.querySelector('img'), null, '模型输出里的 HTML 不能被解析成节点');
  assert.match(evil.textContent, /<img src=x/, 'HTML 应原样当文字显示');
  assert.equal(evil.querySelector('a'), null, 'javascript: 链接不能渲染成可点链接');
  assert.match(evil.textContent, /点我/, '被拒绝的链接要保留文字');

  // 11) 用户消息保持原样，不做 Markdown
  const log = doc.querySelector('#chat-log');
  w.appendChat('user', '**这不是粗体**');
  const last = log.lastElementChild;
  assert.equal(last.querySelector('strong'), null, '用户消息不该被渲染成 Markdown');
  assert.equal(last.querySelector('p').textContent, '**这不是粗体**');

  // 12) 助手消息走 Markdown
  w.appendChat('assistant', '**这是粗体**');
  const reply = log.lastElementChild;
  assert.ok(reply.querySelector('.chat-markdown strong'), '助手消息要渲染 Markdown');

  // 13) 空回复与纯空白不炸
  const blank = render('');
  assert.equal(blank.childNodes.length, 0);

  console.log('chat markdown DOM: 13 项通过');
  w.close();
  process.exit(0);
}

run().catch(error => {
  console.error('FAIL:', error.message);
  process.exit(1);
});
