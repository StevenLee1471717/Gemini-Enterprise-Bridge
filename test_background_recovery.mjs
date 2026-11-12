import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const listeners = [];
const event = () => ({addListener() {}, removeListener() {}});
let receiver = true, injectWorks = true, reloadWorks = true;
let injections = 0, reloads = 0;
const sent = [];
const tabs = [{id: 1, url: 'https://vertexaisearch.cloud.google.com/u/1/home', status: 'complete'}];
const chrome = {
  storage: {
    local: {get: async () => ({enabled: false, preferredTabId: 1}), set: async () => {}},
    session: {get: async () => ({}), set: async () => {}}
  },
  action: {setBadgeText: async () => {}, setBadgeBackgroundColor: async () => {}},
  alarms: {create: async () => {}, onAlarm: event()},
  runtime: {onInstalled: event(), onStartup: event(), onMessage: event(), onConnect: event()},
  tabs: {
    query: async () => tabs, get: async id => tabs.find(t => t.id === id), update: async () => {},
    sendMessage: async () => { if (!receiver) throw new Error('Receiving end does not exist.'); return {ok:true}; },
    reload: async id => {reloads++; receiver = reloadWorks; queueMicrotask(() => listeners.slice().forEach(f => f(id, {status:'complete'}, tabs[0])));},
    onUpdated: {addListener: f => listeners.push(f), removeListener: f => listeners.splice(listeners.indexOf(f), 1)},
    onRemoved: event(), onActivated: event()
  },
  scripting: {executeScript: async () => {injections++; if (!injectWorks) throw new Error('Frame showing error page'); receiver = true;}}
};
const ctx = vm.createContext({chrome, console, URL, setTimeout, clearTimeout, setInterval, clearInterval, WebSocket:{OPEN:1, CONNECTING:0}});
vm.runInContext(fs.readFileSync('background.js','utf8'), ctx);
await new Promise(r => setImmediate(r));
listeners.length = 0;
await ctx.ensureTabBridgeReady(1);
assert.equal(injections, 0, 'healthy page must not reset its driver');
receiver=false;
await ctx.ensureTabBridgeReady(1);
assert.equal(reloads, 0, 'lost receiver can be reinjected without reload');
receiver=false; injectWorks=false;
await ctx.ensureTabBridgeReady(1);
assert.equal(reloads, 1, 'network error page gets one reload');
receiver=false; reloadWorks=false;
await assert.rejects(ctx.ensureTabBridgeReady(1), /Receiving end/);
assert.equal(reloads, 2, 'persistent outage has bounded recovery');
assert.equal(await ctx.findTargetVertexTab(99), null, 'missing assigned tab must not route into another session');
vm.runInContext('socketRuntime.ws = {readyState:1, send: value => sent.push(JSON.parse(value))}', Object.assign(ctx, {sent}));
await ctx.handleBackendCommand({type:'execute_prompt', request_id:'failed-page', tab_id:1, prompt:'hello'});
assert.equal(sent.filter(e => e.type === 'request_error').length, 1, 'preparation errors reach the caller');
assert.match(sent.find(e => e.type === 'request_error').error, /Receiving end/);
console.log('PASS: healthy page, reinjection, reload recovery, bounded failure, session isolation, error delivery');

await ctx.handleBackendCommand({type:'execute_prompt_chunk', request_id:'missing-chunk', chunk_index:0, total_chunks:2, data:'only half'});
await ctx.handleBackendCommand({type:'execute_prompt', request_id:'missing-chunk', tab_id:1, chunked:true, total_chunks:2});
assert.match(sent.find(e => e.type === 'request_error' && e.request_id === 'missing-chunk').error, /分段/);
console.log('PASS: incomplete input chunks fail before page submission');
