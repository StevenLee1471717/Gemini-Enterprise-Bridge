import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const listeners = [];
const event = () => ({ addListener() {}, removeListener() {} });
const sent = [];
const fetchedBodies = [];
let nextCreatedId = 10;
let simulateRedirectToHome = false;

const tabs = [
  {
    id: 1,
    windowId: 1,
    url: 'https://vertexaisearch.cloud.google.com/u/0/home/cid/org1',
    title: 'Gemini Enterprise',
    status: 'complete',
    active: false
  }
];

const chrome = {
  storage: {
    local: {
      get: async () => ({
        enabled: true,
        autoSpawnTabs: true,
        maxParallelTabs: 4,
        manualTabIds: [],
        manualSessionIds: []
      }),
      set: async () => {}
    },
    session: { get: async () => ({}), set: async () => {} }
  },
  action: { setBadgeText: async () => {}, setBadgeBackgroundColor: async () => {} },
  alarms: { create: async () => {}, onAlarm: event() },
  runtime: { onInstalled: event(), onStartup: event(), onMessage: event(), onConnect: event() },
  tabs: {
    query: async () => tabs.slice(),
    get: async (id) => tabs.find((t) => t.id === id) || null,
    update: async (id, patch) => {
      const t = tabs.find((item) => item.id === id);
      if (t && patch.url) {
        t.url = simulateRedirectToHome
          ? 'https://vertexaisearch.cloud.google.com/u/0/home/cid/org1'
          : patch.url;
        t.title = simulateRedirectToHome ? 'Home' : 'Session Re-entered';
        queueMicrotask(() =>
          listeners.slice().forEach((fn) => fn(id, { status: 'complete' }, t))
        );
      }
      return t;
    },
    create: async (opts) => {
      const created = {
        id: ++nextCreatedId,
        windowId: opts.windowId || 1,
        url: opts.url,
        title: 'Spawned Session',
        status: 'complete',
        active: false
      };
      tabs.push(created);
      queueMicrotask(() =>
        listeners.slice().forEach((fn) => fn(created.id, { status: 'complete' }, created))
      );
      return created;
    },
    sendMessage: async (tabId, msg) => {
      const t = tabs.find((item) => item.id === tabId);
      if (msg.type === 'GEB_GET_VISIBILITY') {
        return { ok: true, driverVersion: '2.8.4', visibilityState: 'hidden' };
      }
      if (msg.type === 'GEB_DIAGNOSE_PAGE') {
        return {
          ok: true,
          driverVersion: '2.8.4',
          url: t ? t.url : '',
          title: t ? t.title : '',
          inputFound: true,
          activeModel: '4 Carbon'
        };
      }
      return { ok: true };
    },
    reload: async (id) => {
      const t = tabs.find((item) => item.id === id);
      queueMicrotask(() =>
        listeners.slice().forEach((fn) => fn(id, { status: 'complete' }, t))
      );
    },
    onUpdated: {
      addListener: (f) => listeners.push(f),
      removeListener: (f) => {
        const idx = listeners.indexOf(f);
        if (idx >= 0) listeners.splice(idx, 1);
      }
    },
    onRemoved: event(),
    onActivated: event()
  },
  scripting: { executeScript: async () => {} }
};

const mockFetch = async (_url, init) => {
  fetchedBodies.push(JSON.parse(init.body));
  return { ok: true };
};

const ctx = vm.createContext({
  chrome,
  console,
  URL,
  fetch: mockFetch,
  navigator: { userAgent: 'MockChrome/130' },
  setTimeout,
  clearTimeout,
  setInterval,
  clearInterval,
  WebSocket: Object.assign(
    function MockWS() {
      this.readyState = 1;
      this.send = (val) => sent.push(JSON.parse(val));
      this.close = () => {};
      this.addEventListener = () => {};
    },
    { OPEN: 1, CONNECTING: 0 }
  )
});

vm.runInContext(fs.readFileSync('background.js', 'utf8'), ctx);
await new Promise((r) => setImmediate(r));
vm.runInContext(
  'socketRuntime.ws = { readyState: 1, send: (val) => sent.push(JSON.parse(val)) }',
  Object.assign(ctx, { sent })
);

// 1) Capabilities advertise open_session
await ctx.reportTabStatusToBackend();
const statusMsg = sent.find((m) => m.type === 'tab_status');
assert.ok(statusMsg && statusMsg.capabilities.includes('open_session'), 'capabilities must include open_session');

// 2) open_session succeeds when URL retains target session ID and composer input is ready
sent.length = 0;
const targetUrl = 'https://vertexaisearch.cloud.google.com/u/0/app/cid1/r/session/vsid-target-42';
await ctx.handleBackendCommand({
  type: 'open_session',
  open_id: 'op-ok-1',
  tab_id: 1,
  session_url: targetUrl
});
const okRes = sent.find((m) => m.type === 'open_session_result' && m.open_id === 'op-ok-1');
assert.equal(okRes.ok, true);
assert.equal(okRes.tab_url, targetUrl);
assert.equal(okRes.active_model, '4 Carbon');

// 3) open_session detects redirect back to blank /home and reports ok: false
sent.length = 0;
simulateRedirectToHome = true;
await ctx.handleBackendCommand({
  type: 'open_session',
  open_id: 'op-redir-2',
  tab_id: 1,
  session_url: 'https://vertexaisearch.cloud.google.com/u/0/app/cid1/r/session/vsid-expired-99'
});
const redirRes = sent.find((m) => m.type === 'open_session_result' && m.open_id === 'op-redir-2');
assert.equal(redirRes.ok, false);
assert.match(redirRes.error, /重定向/);
simulateRedirectToHome = false;

// 4) spawn_tab with preserve_session_url=true preserves /r/session/<id>
sent.length = 0;
const directArchiveUrl = 'https://vertexaisearch.cloud.google.com/u/0/app/cid1/r/session/vsid-spawn-77';
await ctx.handleBackendCommand({
  type: 'spawn_tab',
  spawn_id: 'sp-preserve-1',
  base_url: directArchiveUrl,
  preserve_session_url: true
});
const spawnRes = sent.find((m) => m.type === 'spawn_tab_result' && m.spawn_id === 'sp-preserve-1');
assert.equal(spawnRes.ok, true);
assert.equal(spawnRes.tab.tab_url, directArchiveUrl);

// 5) deliverResultToBackend forwards tab_url and tab_title in HTTP callback body
await ctx.deliverResultToBackend({
  type: 'request_complete',
  request_id: 'req-large-1',
  tab_id: 1,
  tab_url: targetUrl,
  tab_title: 'Session Title 42',
  reply: 'x'.repeat(30000)
});
assert.equal(fetchedBodies.length, 1);
assert.equal(fetchedBodies[0].tab_url, targetUrl);
assert.equal(fetchedBodies[0].tab_title, 'Session Title 42');

console.log('PASS: open_session navigation, redirect detection, preserve_session_url spawn, and callback tab_url forwarding');
process.exit(0);
