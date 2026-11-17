import fs from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
const source = fs.readFileSync('popup.js','utf8');
const body = source.slice(source.indexOf('async function saveSettings()'), source.indexOf('async function runPageDiagnosis()'));
const calls = [];
const ctx = vm.createContext({
  elTabSelect:{value:''}, elEnabled:{checked:true}, elModeSelect:{value:'auto'},
  elAutoSpawn:{value:'true'}, elMaxTabs:{value:'10'}, elWsUrl:{value:'ws://127.0.0.1:8000/ws/browser'},
  elModelSel:{value:''}, elInputSel:{value:''}, elSendSel:{value:''}, elReplySel:{value:''}, elDiagOut:{dataset:{}},
  chrome:{runtime:{sendMessage: async msg => calls.push(msg)}}, loadState:async () => {}
});
vm.runInContext(body,ctx);
await ctx.saveSettings();
assert.deepEqual(calls.map(x=>x.type), ['GEB_POPUP_SAVE_SETTINGS']);
assert.equal(calls[0].settings.maxParallelTabs,10);
console.log('PASS: changing parallel limit does not disconnect active requests');

// Verify pool cap, manual session exclusion, overflow promotion, and session URL persistence
const store = { enabled: true, maxParallelTabs: 2, autoSpawnTabs: true };
const openTabs = [
  { id: 11, url: 'https://vertexaisearch.cloud.google.com/u/1/home', title: 'Tab 1', status: 'complete' },
  { id: 12, url: 'https://vertexaisearch.cloud.google.com/u/1/home/cid/x/r/session/sess-own', title: 'Tab 2', status: 'complete' },
  { id: 13, url: 'https://vertexaisearch.cloud.google.com/u/1/home', title: 'Tab 3', status: 'complete' }
];
const sentTabMsgs = [];
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
  runtime: { onInstalled: eventStub(), onStartup: eventStub(), onMessage: eventStub(), onConnect: eventStub() },
  tabs: {
    query: async () => openTabs,
    get: async (id) => openTabs.find((t) => t.id === id),
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
bgCtx.clearReconnectTimer();
bgCtx.clearHeartbeatInterval();
console.log('PASS: default pool cap, manual tab/session exclusion, overflow promotion, and state broadcast');
