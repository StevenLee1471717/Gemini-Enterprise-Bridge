import fs from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
const source = fs.readFileSync('popup.js','utf8');
const helperBody = source.slice(
  source.indexOf('function normalizeMaxParallelTabs('),
  source.indexOf('function renderCurrentTabControl(')
);
const body = source.slice(source.indexOf('async function saveSettings()'), source.indexOf('async function runPageDiagnosis()'));
const calls = [];
const segButtons = ['1', '5', '10', 'custom'].map((val) => ({
  disabled: false,
  active: false,
  textContent: val === 'custom' ? '自定义' : `${val} 页`,
  getAttribute: (attr) => (attr === 'data-val' ? val : null),
  classList: {
    toggle(cls, force) {
      if (cls === 'active') this._btn.active = Boolean(force);
    },
    add(cls) {
      if (cls === 'active') this._btn.active = true;
    }
  }
}));
for (const b of segButtons) b.classList._btn = b;
const optionsMap = new Map([['1', { value: '1' }], ['5', { value: '5' }], ['10', { value: '10' }]]);
const ctx = vm.createContext({
  elTabSelect:{value:''}, elEnabled:{checked:true}, elModeSelect:{value:'auto'},
  elAutoSpawn:{value:'true'},
  elMaxTabs:{value:'10', querySelector:(sel)=>{ const m=sel.match(/value="([^"]+)"/); return m ? optionsMap.get(m[1]) || null : null; }, appendChild:(opt)=>optionsMap.set(opt.value, opt)},
  document:{createElement:()=>({value:'', textContent:''})},
  customCapEditing:false, elCustomTabsRow:{hidden:true},
  elWsUrl:{value:'ws://127.0.0.1:8000/ws/browser'},
  elModelSel:{value:''}, elInputSel:{value:''}, elSendSel:{value:''}, elReplySel:{value:''}, elDiagOut:{dataset:{}},
  elAutoSpawnCheck:{checked:true, disabled:false},
  elPoolControlsWrap:{classList:{toggle(){}}},
  elMaxTabsSegments:{querySelectorAll: () => segButtons},
  chrome:{runtime:{sendMessage: async msg => calls.push(msg)}}, loadState:async () => {}
});
vm.runInContext(`${helperBody}\n${body}`,ctx);
await ctx.saveSettings();
assert.deepEqual(calls.map(x=>x.type), ['GEB_POPUP_SAVE_SETTINGS']);
assert.equal(calls[0].settings.maxParallelTabs,10);

// Legacy 0 (unlimited removed) normalizes back to default 10
ctx.elMaxTabs.value = '0';
await ctx.saveSettings();
assert.equal(calls[1].settings.maxParallelTabs, 10);

// Preset 5 highlights '5', and custom number (e.g. 8) highlights 'custom' with label '自定义 (8)'
ctx.syncSegmentUI(5, true, false);
assert.deepEqual(
  segButtons.filter((b) => b.active).map((b) => b.getAttribute('data-val')),
  ['5']
);
assert.equal(segButtons[3].textContent, '自定义');
assert.equal(ctx.parseCustomParallelTabsInput('8'), 8);
assert.equal(ctx.parseCustomParallelTabsInput('0'), null);
assert.equal(ctx.parseCustomParallelTabsInput('201'), null);
ctx.ensureSelectHasValue(ctx.elMaxTabs, '8');
await ctx.saveSettings();
assert.equal(calls[2].settings.maxParallelTabs, 8);
ctx.syncSegmentUI(8, true, false);
assert.deepEqual(
  segButtons.filter((b) => b.active).map((b) => b.getAttribute('data-val')),
  ['custom']
);
assert.equal(segButtons[3].textContent, '自定义 (8)');
console.log('PASS: changing parallel limit does not disconnect active requests');

// Verify pool cap, manual session exclusion, overflow promotion, and session URL persistence
const store = { enabled: true, maxParallelTabs: 2, autoSpawnTabs: true };
const openTabs = [
  { id: 11, url: 'https://vertexaisearch.cloud.google.com/u/1/home', title: 'Tab 1', status: 'complete' },
  { id: 12, url: 'https://vertexaisearch.cloud.google.com/u/1/home/cid/x/r/session/sess-own', title: 'Tab 2', status: 'complete' },
  { id: 13, url: 'https://vertexaisearch.cloud.google.com/u/1/home', title: 'Tab 3', status: 'complete' }
];
const sentTabMsgs = [];
const createdTabs = [];
let onMessageListener = null;
const eventStub = () => ({ addListener() {}, removeListener() {} });
const bgChrome = {
  storage: {
    local: {
      get: async (keys) => {
        if (!keys) return { ...store };
        const arr = Array.isArray(keys) ? keys : [keys];
        const out = {};
        for (const k of arr) {
          if (store[k] !== undefined) out[k] = store[k];
        }
        return out;
      },
      set: async (patch) => Object.assign(store, patch)
    },
    session: { get: async () => ({}), set: async () => {} }
  },
  action: { setBadgeText: async () => {}, setBadgeBackgroundColor: async () => {} },
  alarms: { create: async () => {}, onAlarm: eventStub() },
  runtime: {
    onInstalled: eventStub(),
    onStartup: eventStub(),
    onMessage: { addListener: (fn) => { onMessageListener = fn; }, removeListener() {} },
    onConnect: eventStub()
  },
  tabs: {
    query: async () => openTabs,
    get: async (id) => openTabs.find((t) => t.id === id),
    create: async (opts) => {
      const created = { id: 200 + createdTabs.length, url: opts.url, active: Boolean(opts.active), status: 'complete' };
      createdTabs.push(created);
      openTabs.push(created);
      return created;
    },
    update: async () => {},
    sendMessage: async (tabId, msg) => { sentTabMsgs.push({ tabId, msg }); return { ok: true }; },
    onUpdated: eventStub(),
    onRemoved: eventStub(),
    onActivated: eventStub()
  },
  scripting: { executeScript: async () => {} }
};
const bgCtx = vm.createContext({
  chrome: bgChrome,
  console,
  URL,
  navigator: { userAgent: 'test' },
  setTimeout,
  clearTimeout,
  setInterval,
  clearInterval,
  WebSocket: { OPEN: 1, CONNECTING: 0 }
});
vm.runInContext(fs.readFileSync('background.js', 'utf8'), bgCtx);
await new Promise((r) => setImmediate(r));

// 1) Default: first 2 tabs managed, 3rd tab overflow (not eligible)
let cls = await bgCtx.classifyAllVertexTabs();
assert.deepEqual(cls.managedTabs.map((t) => t.id), [11, 12]);
assert.deepEqual(cls.overflowTabs.map((t) => t.id), [13]);
assert.equal(cls.perTabState.get(13).mode, 'overflow');
assert.equal(await bgCtx.findTargetVertexTab(13), null);

// 2) Mark tab #12 as manual self-use -> removed from pool, #13 auto-promotes into pool, session key persisted
await bgCtx.setTabManualState(12, true);
cls = await bgCtx.classifyAllVertexTabs();
assert.deepEqual(cls.managedTabs.map((t) => t.id), [11, 13]);
assert.deepEqual(cls.manualTabs.map((t) => t.id), [12]);
assert.equal(cls.perTabState.get(12).mode, 'manual');
assert.equal(await bgCtx.findTargetVertexTab(12), null);
assert.ok(store.manualSessionKeys.includes('session:sess-own'));

// 3) Even if browser restarts and tab ID changes (#12 -> #99) on the same session URL, it stays manual!
openTabs[1] = { id: 99, url: 'https://vertexaisearch.cloud.google.com/u/1/home/cid/x/r/session/sess-own', title: 'Reopened' };
store.manualTabIds = [];
cls = await bgCtx.classifyAllVertexTabs();
assert.equal(cls.perTabState.get(99).mode, 'manual');
assert.deepEqual(cls.managedTabs.map((t) => t.id), [11, 13]);

// 4) Restore tab #99 back to Bridge -> manualSessionKeys cleared
await bgCtx.setTabManualState(99, false);
assert.ok(!store.manualSessionKeys.includes('session:sess-own'));

// 5) Opening a manual tab when existing tab is ALREADY a clean /home/cid/... URL (no /r/session/) or alongside a 404 root tab
const sendBgMsg = (msg) =>
  new Promise((resolve) => {
    onMessageListener(msg, {}, resolve);
  });
openTabs.length = 0;
delete store.lastVertexHomeUrl;
openTabs.push(
  { id: 50, url: 'https://vertexaisearch.cloud.google.com/', title: 'Error 404 (Not Found)!!1', status: 'complete' },
  { id: 51, url: 'https://vertexaisearch.cloud.google.com/u/1/home/cid/f8a26a7c-0f51-4098-bf58-52506661472b?hl=en_US', title: 'Gemini Enterprise', status: 'complete' }
);
cls = await bgCtx.classifyAllVertexTabs();
assert.deepEqual(cls.allTabs.map((t) => t.id), [51], 'Bare 404 root URL must not be admitted');
const manualOpenRes = await sendBgMsg({ type: 'GEB_POPUP_OPEN_MANUAL_TAB' });
assert.equal(manualOpenRes.ok, true);
assert.equal(
  manualOpenRes.url,
  'https://vertexaisearch.cloud.google.com/u/1/home/cid/f8a26a7c-0f51-4098-bf58-52506661472b?hl=en_US'
);
cls = await bgCtx.classifyAllVertexTabs();
assert.equal(cls.perTabState.get(manualOpenRes.tabId).mode, 'manual');
assert.equal(
  store.lastVertexHomeUrl,
  'https://vertexaisearch.cloud.google.com/u/1/home/cid/f8a26a7c-0f51-4098-bf58-52506661472b?hl=en_US'
);

// 6) Even when all Gemini tabs are closed, remembered workspace URL prevents 404
openTabs.length = 0;
const closedManualRes = await sendBgMsg({ type: 'GEB_POPUP_OPEN_MANUAL_TAB' });
assert.equal(
  closedManualRes.url,
  'https://vertexaisearch.cloud.google.com/u/1/home/cid/f8a26a7c-0f51-4098-bf58-52506661472b?hl=en_US'
);

// 7) Fresh install with zero history falls back to clean workspace cid entry instead of bare 404 root
openTabs.length = 0;
delete store.lastVertexHomeUrl;
const freshRes = await sendBgMsg({ type: 'GEB_POPUP_OPEN_VERTEX_TAB' });
assert.equal(
  freshRes.url,
  'https://vertexaisearch.cloud.google.com/home/cid/f8a26a7c-0f51-4098-bf58-52506661472b?hl=en_US'
);

// 8) Legacy 0 in storage auto-migrates to 10 (unlimited removed), and custom cap (e.g. 13) manages 13 tabs
openTabs.length = 0;
store.manualTabIds = [];
store.manualSessionKeys = [];
store.maxParallelTabs = 0;
for (let i = 1; i <= 12; i += 1) {
  openTabs.push({
    id: 300 + i,
    url: `https://vertexaisearch.cloud.google.com/u/1/home/cid/f8a26a7c-0f51-4098-bf58-52506661472b?tab=${i}`,
    title: `Worker ${i}`,
    status: 'complete'
  });
}
cls = await bgCtx.classifyAllVertexTabs();
assert.equal(cls.maxTabs, 10);
assert.equal(store.maxParallelTabs, 10);
assert.equal(cls.managedTabs.length, 10);
assert.equal(cls.overflowTabs.length, 2);
store.maxParallelTabs = 13;
cls = await bgCtx.classifyAllVertexTabs();
assert.equal(cls.maxTabs, 13);
assert.equal(cls.managedTabs.length, 12);
assert.equal(cls.overflowTabs.length, 0);
const spawnedCustom = await bgCtx.spawnParallelWorkerTab();
assert.equal(spawnedCustom.ready, true);
cls = await bgCtx.classifyAllVertexTabs();
assert.equal(cls.managedTabs.length, 13);
assert.equal(cls.overflowTabs.length, 0);

// 9) Backend push `set_max_parallel_tabs` saves custom number (and normalizes 0 -> 10)
store.maxParallelTabs = 6;
store.autoSpawnTabs = true;
const wsSent = [];
bgCtx.__mockWs = {
  readyState: 1,
  send: (raw) => wsSent.push(JSON.parse(raw))
};
vm.runInContext('socketRuntime.ws = __mockWs;', bgCtx);
await bgCtx.handleBackendCommand({
  type: 'set_max_parallel_tabs',
  max_parallel_tabs: 7,
  auto_spawn_tabs: false
});
assert.equal(store.maxParallelTabs, 7);
assert.equal(store.autoSpawnTabs, false);
const tabStatusEvent = wsSent.find((m) => m.type === 'tab_status');
assert.ok(tabStatusEvent, 'Expected tab_status WS report');
assert.equal(tabStatusEvent.max_parallel_tabs, 7);
const ackEvent = wsSent.find((m) => m.type === 'set_max_parallel_tabs_ack');
assert.deepEqual(ackEvent, {
  type: 'set_max_parallel_tabs_ack',
  ok: true,
  max_parallel_tabs: 7
});
vm.runInContext('socketRuntime.ws = null;', bgCtx);

bgCtx.clearReconnectTimer();
bgCtx.clearHeartbeatInterval();
console.log('PASS: default pool cap, manual tab/session exclusion, overflow promotion, and state broadcast');
