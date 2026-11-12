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
