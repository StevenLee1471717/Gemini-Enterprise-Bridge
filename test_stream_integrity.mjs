import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const sandbox = {
  window: null, globalThis: null,
  fetch: async () => {}, XMLHttpRequest: function () {},
  location: { origin: 'https://vertexaisearch.cloud.google.com' },
  document: {}, console: { log() {}, warn() {}, error() {} },
  TextDecoder, Headers, URL,
  postMessage() {}, addEventListener() {}, removeEventListener() {},
};
sandbox.window = sandbox; sandbox.globalThis = sandbox;
sandbox.XMLHttpRequest.prototype = {open() {}, send() {}};
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(new URL('./page-driver.js', import.meta.url), 'utf8'), sandbox);
const state = sandbox.__GEB_MAIN_STATE__;
function job() {
  return { cancelled: false, networkText: '', citations: [], networkDone: false,
    onNetworkDelta() {}, onNetworkDone() { this.doneCount = (this.doneCount || 0) + 1; } };
}
function stream(parts, fail = false) {
  const bytes = new TextEncoder();
  return new ReadableStream({start(controller) {
    for (const part of parts) controller.enqueue(bytes.encode(part));
    if (fail) controller.error(new Error('dropped')); else controller.close();
  }});
}
const complete = job();
await state.consumeStream(stream(['[{"groundedContent":{"content":{"text":"verified"}}}]']), complete);
assert.equal(complete.networkText, 'verified'); assert.equal(complete.networkDone, true);
const incomplete = job();
await state.consumeStream(stream(['[{"groundedContent":{"content":{"text":"partial"}}}, {"groundedContent":']), incomplete);
assert.equal(incomplete.networkDone, false); assert.ok(incomplete.networkError);
const dropped = job();
await state.consumeStream(stream([], true), dropped);
assert.equal(dropped.networkDone, false); assert.ok(dropped.networkError);
const thought = job();
state.parsePacket({thought: true, groundedContent: {content:{text:'internal draft'}}}, thought);
state.parsePacket({isThought: true, content:{role:'model',parts:[{text:'internal draft'}]}}, thought);
state.parsePacket({groundedContent: {content:{text:'public answer'}}}, thought);
assert.equal(thought.networkText, 'public answer');
console.log('PASS: complete, truncated, dropped streams and thought subtree isolation');

const snapshot = job();
const tool = '[TOOL_CALL]{"name":"exec_command","arguments":{"cmd":"cat a"}}[/TOOL_CALL]';
state.parsePacket({groundedContent:{content:{text:tool}}}, snapshot);
state.parsePacket({summaryText:'\n[TOOL_CALL] { "arguments": {"cmd":"cat a"}, "name":"exec_command" } [/TOOL_CALL]\n'}, snapshot);
assert.equal(snapshot.networkText, tool, 'formatted final snapshots must not execute the same command twice');
state.parsePacket({summaryText:'[TOOL_CALL]{"name":"exec_command","arguments":{"cmd":"cat  a"}}[/TOOL_CALL]'}, snapshot);
assert.ok(snapshot.networkText.includes('cat  a'), 'spaces inside command strings must not be erased');
const increments = job();
state.parsePacket({groundedContent:{content:{text:'ha'}}}, increments);
state.parsePacket({groundedContent:{content:{text:'ha'}}}, increments);
assert.equal(increments.networkText, 'haha', 'normal repeated token deltas must be preserved');
const suggestions = job();
state.parsePacket({groundedContent:{content:{text:'answer'}},suggestedQuestions:[{summaryText:'unwanted suggestion'}]}, suggestions);
assert.equal(suggestions.networkText, 'answer');
console.log('PASS: formatted snapshots, unchanged command strings, repeated deltas and suggestion isolation');

const nestedSnapshot = job();
const nestedValue = '[TOOL_CALL]{"name":"inner","arguments":{}}[/TOOL_CALL]';
const nestedTool = {name:'exec_command', arguments:{cmd:nestedValue}};
const nestedRaw = '[TOOL_CALL]' + JSON.stringify(nestedTool) + '[/TOOL_CALL]';
state.parsePacket({groundedContent:{content:{text:nestedRaw}}}, nestedSnapshot);
state.parsePacket({summaryText:'[TOOL_CALL]\n' + JSON.stringify(nestedTool, null, 2) + '\n[/TOOL_CALL]'}, nestedSnapshot);
assert.equal(nestedSnapshot.networkText, nestedRaw, 'markers inside command strings must not defeat snapshot deduplication');
console.log('PASS: nested protocol strings in final snapshots');

for (const suffix of [', {"broken": invalid}]', '']) {
  const invalid = job();
  await state.consumeStream(stream(['[{"groundedContent":{"content":{"text":"partial"}}}' + suffix]), invalid);
  assert.equal(invalid.networkDone, false, 'malformed packets and unclosed arrays cannot complete a response');
  assert.ok(invalid.networkError);
}
console.log('PASS: malformed packets and truncated outer arrays');

// Reloading a different version replaces the old wrapper around the original fetch.
const oldFetch = sandbox.fetch;
state.networkPatchVersion = 'previous-version';
vm.runInContext(fs.readFileSync(new URL('./page-driver.js', import.meta.url), 'utf8'), sandbox);
assert.notEqual(sandbox.fetch, oldFetch);
let nativeCalls = 0;
const requestJob = job();
state.getActiveJob = () => requestJob;
state.checkChatRpc = () => true;
state.nativeFetch = async () => {nativeCalls += 1;throw new TypeError('dropped headers');};
await assert.rejects(sandbox.fetch('https://vertexaisearch.cloud.google.com/chat', {method:'POST', body:'{}'}), /dropped headers/);
assert.equal(nativeCalls, 1);
assert.equal(requestJob.submissionStarted, true);
assert.ok(requestJob.networkError);
console.log('PASS: upgraded network hooks, one native request, early submission acknowledgement and fetch failure');

const decoratedSnapshot = job();
const streamedWithPreamble = '正在检查文件。\n\n' + tool;
state.parsePacket({groundedContent:{content:{text:streamedWithPreamble}}}, decoratedSnapshot);
state.parsePacket({summaryText:'[[section:检查]]正在检查文件。\n\n[TOOL_CALL] { "arguments": {"cmd":"cat a"}, "name":"exec_command" } [/TOOL_CALL]\n[[suggested_questions]]\n下一步？'}, decoratedSnapshot);
assert.equal((decoratedSnapshot.networkText.match(/\[TOOL_CALL\]/g) || []).length, 1, 'decorated final snapshots must not duplicate tool calls or preamble');

const suffixSnapshot = job();
state.parsePacket({groundedContent:{content:{text:streamedWithPreamble}}}, suffixSnapshot);
state.parsePacket({summaryText:'正在检查文件。\n\n[TOOL_CALL] { "arguments": {"cmd":"cat a"}, "name":"exec_command" } [/TOOL_CALL]\n已完成检查。'}, suffixSnapshot);
assert.equal((suffixSnapshot.networkText.match(/\[TOOL_CALL\]/g) || []).length, 1, 'snapshot suffix extraction must keep single tool call');
assert.ok(suffixSnapshot.networkText.endsWith('\n已完成检查。'), 'genuine new suffix in snapshot must be preserved');
console.log('PASS: decorated and suffix final snapshots without duplicating streamed content');
