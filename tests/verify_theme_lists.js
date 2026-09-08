// Verify the edited theme files: syntax + BLOCKED_SITES/DENY behaviour.
// Patterns are extracted from the real file, not retyped.
const fs = require('fs');
const vm = require('vm');
const ROOT = 'C:\\Users\\Shan\\documents\\daigo\\theme\\';

let pass = 0, fail = 0;
function check(name, cond, detail) {
  if (cond) { pass++; console.log('OK   ' + name); }
  else { fail++; console.log('FAIL ' + name + (detail ? '  -- ' + detail : '')); }
}

// ── 1. daigo.js must still parse ────────────────────────────────────
const js = fs.readFileSync(ROOT + 'assets/daigo.js', 'utf8');
try { new vm.Script('(function(){' + js + '})'); check('daigo.js parses', true); }
catch (e) { check('daigo.js parses', false, e.message); }

// 🔴 一定要先把 daikoManualOrder 的函式本體切出來再驗。
//    daikoCreateOrder 裡有一模一樣的
//      if (!data.success) { backToInput(); showError(data.error || '建立商品失敗', ...) }
//    整檔搜尋會命中它，於是「manual 這邊漏了 backToInput」也會全綠 ——
//    2026-09-08 缺陷注入就是這樣抓到這條斷言本身是假的。
const manualStart = js.indexOf('async function daikoManualOrder');
const manualEnd = js.indexOf('async function daikoCreateOrder');
check('located daikoManualOrder before daikoCreateOrder',
      manualStart > 0 && manualEnd > manualStart, `${manualStart}/${manualEnd}`);
const manual = js.slice(manualStart, manualEnd);

check('daikoManualOrder no longer uses alert for !success',
      !/if \(!data\.success\) \{ alert\(/.test(manual));
check('daikoManualOrder now calls showError with handoff_url',
      /if \(!data\.success\) \{[\s\S]{0,120}?showError\(data\.error \|\| '建立商品失敗', data\.handoff_url\);/.test(manual));
// backToInput 不是可有可無：#daiko-error 在 step-input 裡，而手動步驟時
// step-input 是隱藏的 —— 少了它訊息會畫在看不見的容器裡（檔案自己的註解寫過）。
check('daikoManualOrder calls backToInput before showError',
      /if \(!data\.success\) \{\s*backToInput\(\);/.test(manual));
// the two blocked branches (daikoSearch + daikoManualOrder) must be untouched
check('both blocked branches untouched',
      (js.match(/showError\(data\.error \|\| '這個連結目前不開放代購。', data\.handoff_url\)/g) || []).length === 2);
// handoff_url call sites: 2 blocked + daikoCreateOrder + the new manual one = 4
// (count `, data.handoff_url)` so the mention inside my comment isn't counted)
check('daigo.js now passes handoff_url at 4 call sites (was 3)',
      (js.match(/, data\.handoff_url\)/g) || []).length === 4,
      String((js.match(/, data\.handoff_url\)/g) || []).length));
check('alert() still used for the genuinely local validations',
      /alert\('請填寫商品名稱'\)/.test(js) && /alert\('請填寫正確的日幣價格'\)/.test(js));

// ── 2. extract BLOCKED_SITES + DENY out of the liquid ───────────────
const liq = fs.readFileSync(ROOT + 'sections/daigo.liquid', 'utf8');

const pats = [...liq.matchAll(/pattern:\s*(\/(?:\\.|\[[^\]]*\]|[^/\\])+\/[a-z]*)/g)].map(m => m[1]);
check('found 14 BLOCKED_SITES patterns', pats.length === 14, String(pats.length));
const RE = pats.map(p => vm.runInNewContext(p));   // throws if any is invalid

const denyRaw = liq.match(/var DENY = \[([\s\S]*?)\];/)[1];
const DENY = [...denyRaw.matchAll(/'([^']+)'/g)].map(m => m[1]);
check('DENY now has 14 entries', DENY.length === 14, String(DENY.length));
for (const d of ['jpgoodbuy.com', 'buyma.com', 'buyee.jp'])
  check('DENY contains ' + d, DENY.includes(d));

function hostMatches(host, domain) {
  return host === domain || host.slice(-(domain.length + 1)) === '.' + domain;
}
function denied(url) {
  const h = new URL(url).hostname.toLowerCase().replace(/^www\./, '');
  return DENY.some(d => hostMatches(h, d));
}
function gated(url) { return RE.some(r => r.test(url)); }

// ── 3. the gate: must still block, must stop over-blocking ──────────
const MUST_BLOCK = [
  'https://www.hoka.com/en/us/x', 'https://hoka.com/x', 'https://shop.hoka.com/x',
  'https://www.buyee.jp/item/1', 'https://buyee.jp/item/1',
  'https://www.buyma.com/item/1', 'https://buyma.com/item/1',
  'https://www.amazon.com/dp/B0X', 'https://amazon.com/dp/B0X',
  'https://tw.mercari.com/item/1', 'https://jpgoodbuy.com/x',
  'https://www.zenmarket.jp/x', 'https://dokodemo.world/x',
  'https://duty-free-japan.jp/x', 'https://bibian.co.jp/x',
  'https://tokukai.com/x', 'https://letao.com.tw/x',
  'https://daigobang.com/x', 'https://go1buy1.com/x',
];
for (const u of MUST_BLOCK) check('gate still blocks ' + u, gated(u));

const MUST_PASS = [
  ['https://hoka.com.tw/x', 'HOKA 台灣'],
  ['https://www.hoka.com.au/x', 'HOKA 澳洲'],
  ['https://xbuyee.jp/x', '左邊誤擋'],
  ['https://mybuyee.jp/x', '左邊誤擋'],
  ['https://notbuyma.com/x', '左邊誤擋'],
  ['https://buyma.com.tw/x', '右邊誤擋'],
  ['https://notamazon.com/x', '左邊誤擋'],
  ['https://amazon.com.tw/dp/1', '右邊誤擋（原本就有守衛）'],
  ['https://www.amazon.co.jp/dp/1', 'Amazon JP 絕不可擋'],
  ['https://jp.mercari.com/item/m1', 'Mercari JP'],
  ['https://item.rakuten.co.jp/x/1', '樂天'],
  ['https://zozo.jp/shop/x/goods/1', 'ZOZO'],
  ['https://www.dot-st.com/x', 'dot-st'],
];
for (const [u, why] of MUST_PASS) check('gate passes ' + u + ' (' + why + ')', !gated(u));

// ── 4. DENY (auto-fill) must now agree with the gate ────────────────
for (const u of MUST_BLOCK) check('DENY also refuses to autofill ' + u, denied(u));
for (const [u] of MUST_PASS) check('DENY allows autofill ' + u, !denied(u));

console.log('\npass ' + pass + ' / fail ' + fail);
process.exit(fail ? 1 : 0);
