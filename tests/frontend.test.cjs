const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const html = fs.readFileSync(path.join(__dirname, '..', 'index.html'), 'utf8');
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
const elements = {};
const element = id => elements[id] ||= {innerHTML:'', textContent:'', value:'', style:{}, addEventListener(){}};
const now = Date.parse('2026-11-25T12:00:00+09:00');
class Clock extends Date { static now(){ return now; } }
const context = { Date:Clock, console, document:{getElementById:element}, setInterval(){}, window:{} };
// Execute the actual production helpers before event binding.
vm.createContext(context);
vm.runInContext(script.slice(0, script.indexOf('  // ---------------- 30일 일별 가격 추이')) + '\n globalThis.test={state, freshness, checkedAt, cardHTML, render, renderBanner};})();', context);
const t = context.test;
const fresh = {productId:'1', productName:'확인된 상품', productPrice:9000, isRocket:true, unitPrice:900, lastCheckedAt:'2026-11-25 11:00 KST'};
const stale = {...fresh, productId:'2', productName:'오래된 저가 상품', productPrice:100, unitPrice:10, lastCheckedAt:'2026-11-24 12:00 KST', discountRate:99};
assert.equal(t.freshness(fresh).stale, false);
assert.equal(t.freshness(stale).stale, true);
assert.equal(t.freshness({...fresh, refreshStatus:'not_found'}).stale, true);
assert.equal(t.freshness({...fresh, lastCheckedAt:'2026-11-26 12:00 KST'}).stale, true);
assert.equal(t.checkedAt({priceHistory:[['2026-11-19 07:03 KST',8300]], updatedAt:'2026-11-25 12:00 KST'}), '2026-11-19 07:03 KST');
assert.equal(t.checkedAt({updatedAt:'2026-11-25 12:00 KST'}), null);
assert.match(t.cardHTML(stale), /24시간 지난 가격/);
assert.doesNotMatch(t.cardHTML(stale), /평균 대비 99%/);
assert.match(t.cardHTML(fresh), /마지막 가격 확인 · 2026-11-25 11:00 KST/);
const standard = {...fresh, productId:'4', isRocket:false, refreshStatus:'ok'};
t.state.data = {deals:[standard], status:'ok'};
t.render();
assert.match(elements.grid.innerHTML, /일반배송/);
assert.doesNotMatch(elements.grid.innerHTML, /🚀|재확인 필요/);
assert.match(t.cardHTML({...standard,isRocket:null}), /배송 정보 확인/);
const noUnit = {...fresh, productId:'3', unitPrice:null, unitStatus:'unconfirmed'};
t.state.data = {deals:[noUnit], status:'ok'};
t.render();
assert.match(elements.grid.innerHTML, /용량·수량 확인 중/);
assert.match(elements.grid.innerHTML, /9,000/);
t.state.data = {deals:[stale, fresh], status:'ok'};
t.render();
assert.ok(elements.grid.innerHTML.indexOf('확인된 상품') < elements.grid.innerHTML.indexOf('오래된 저가 상품'));
t.state.freshOnly = true;
t.render();
assert.doesNotMatch(elements.grid.innerHTML, /오래된 저가 상품/);
t.renderBanner(t.state.data);
assert.equal(elements.bannerSlot.innerHTML, "");
assert.doesNotMatch(t.cardHTML({...fresh, productName:'<script>alert(1)</script>'}), /<script>alert/);
console.log('Frontend: freshness, time boundary, timestamps, stale separation, filter, escaping passed');

async function testPolling() {
  const model = {data:null, cat:'크림·연유', q:'프레지덩', sort:'unit'};
  let next = {status:'ok', deals:[fresh]}, calls = 0, fail = false;
  const polling = {state:model, el:{stamp:{}}, Date:Clock,
    fetch: async () => { calls++; if (fail) throw new Error('offline'); return {ok:true,json:async()=>next}; },
    setStamp(){},renderBanner(){},renderWatch(){},renderScope(){},render(){}};
  vm.createContext(polling);
  const start = script.indexOf('  var loadingData = false;');
  vm.runInContext(script.slice(start, script.lastIndexOf('  refreshData();')) + '\nglobalThis.load=refreshData;', polling);
  const first = polling.load();
  polling.load();
  await first;
  assert.equal(calls, 1, 'concurrent polls must not duplicate downloads');
  assert.equal(model.byId['1'].productPrice, 9000);
  next = {status:'ok', deals:[{...fresh, productPrice:8800}]};
  await polling.load();
  assert.equal(model.byId['1'].productPrice, 8800);
  assert.equal(model.q, '프레지덩');
  fail = true;
  await polling.load();
  assert.equal(model.byId['1'].productPrice, 8800, 'temporary network failure retains last data');
  assert.match(polling.el.stamp.textContent, /연결 지연/);
  console.log('Frontend: polling, new prices, preserved filters, failed download fallback passed');
}
testPolling().catch(error => { console.error(error); process.exitCode = 1; });
