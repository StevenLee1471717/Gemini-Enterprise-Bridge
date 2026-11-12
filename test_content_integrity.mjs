import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const messages = [], posted = [];
let runtimeListener, windowListener;
const sandbox = {
  window: null, globalThis: null,
  location: {origin:'https://vertexaisearch.cloud.google.com'},
  document: {readyState:'complete', hidden:false, addEventListener(){}, removeEventListener(){}},
  addEventListener(type, callback) { if (type === 'message') windowListener = callback; },
  removeEventListener(){}, postMessage(message) { posted.push(message); },
  chrome: {runtime: {id:'test', onMessage:{addListener(callback){runtimeListener=callback;}},
    sendMessage:async message => {messages.push(message);}},
    storage:{local:{get:async()=>({})},onChanged:{addListener(){}}}},
  console, setTimeout, clearTimeout, setInterval, clearInterval,
};
sandbox.window=sandbox; sandbox.globalThis=sandbox;
const ctx=vm.createContext(sandbox);
vm.runInContext(fs.readFileSync('content-bridge.js','utf8'),ctx);
const vmWindow=vm.runInContext('window',ctx);
const send = message => new Promise(resolve => runtimeListener(message,{},resolve));
const fromMain = payload => windowListener({source:vmWindow,origin:sandbox.location.origin,data:{channel:'GEB_MAIN_TO_ISOLATED',payload}});
for (let index=0;index<2;index++) await send({type:'GEB_PROMPT_CHUNK',request_id:'valid',chunk_index:index,total_chunks:2,data:['first','second'][index]});
await send({type:'GEB_EXECUTE_PROMPT',request_id:'valid',chunked:true,total_chunks:2});
assert.equal(posted.find(m=>m.action==='execute_prompt' && m.payload.request_id==='valid').payload.prompt,'firstsecond');
await send({type:'GEB_PROMPT_CHUNK',request_id:'missing',chunk_index:0,total_chunks:2,data:'only half'});
await send({type:'GEB_EXECUTE_PROMPT',request_id:'missing',chunked:true,total_chunks:2});
assert.ok(!posted.some(m=>m.action==='execute_prompt' && m.payload.request_id==='missing'));
assert.match(messages.find(m=>m.payload?.request_id==='missing').payload.error,/分段/);
await send({type:'GEB_ATTACHMENT_CHUNK',att_key:'missing-file',chunk_index:0,total_chunks:2,data:'data:half'});
await send({type:'GEB_EXECUTE_PROMPT',request_id:'missing-attachment',prompt:'file',attachments:[{chunked_att_key:'missing-file',total_chunks:2}]});
assert.match(messages.find(m=>m.payload?.request_id==='missing-attachment').payload.error,/分段/);
fromMain({type:'reply_chunk',request_id:'missing-reply',chunk_index:0,total_chunks:2,data:'only half'});
fromMain({type:'request_complete',request_id:'missing-reply',chunked:true,total_chunks:2});
await new Promise(resolve=>setImmediate(resolve));
const reply=messages.find(m=>m.payload?.request_id==='missing-reply').payload;
assert.equal(reply.type,'request_error');assert.match(reply.error,/分段/);
ctx.__GEB_ISOLATED_CLEANUP__();
let disconnect, diagnostics = 0;
sandbox.document.hidden = true;
sandbox.chrome.runtime.connect = () => ({onDisconnect:{addListener(callback){disconnect=callback;}}, disconnect(){}, postMessage(){}});
Object.defineProperty(sandbox.chrome.runtime,'lastError',{get(){diagnostics += 1;return {message:'page entered bfcache'};}});
vm.runInContext(fs.readFileSync('content-bridge.js','utf8'),ctx);
disconnect();
assert.equal(diagnostics,1,'expected navigation errors must be consumed without unchecked console noise');
ctx.__GEB_ISOLATED_CLEANUP__();
console.log('PASS: complete input, missing prompt, missing attachment, missing reply');
