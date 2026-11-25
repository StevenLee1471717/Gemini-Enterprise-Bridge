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
const framed = (value, fence='```') => fence+'codex_tool_call\n'+JSON.stringify(value)+'\n'+fence;
const tool=framed({name:'exec_command',arguments:{cmd:'cat a'}});
state.parsePacket({groundedContent:{content:{text:tool}}}, snapshot);
state.parsePacket({summaryText:'\n```codex_tool_call\n { "arguments": {"cmd":"cat a"}, "name":"exec_command" } \n```\n'}, snapshot);
assert.equal(snapshot.networkText, tool, 'formatted final snapshots must not execute the same command twice');
state.parsePacket({summaryText:framed({name:'exec_command',arguments:{cmd:'cat  a'}})}, snapshot);
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
const nestedRaw = framed(nestedTool);
state.parsePacket({groundedContent:{content:{text:nestedRaw}}}, nestedSnapshot);
state.parsePacket({summaryText:'```codex_tool_call\n' + JSON.stringify(nestedTool, null, 2) + '\n```'}, nestedSnapshot);
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
const realCheckChatRpc = state.checkChatRpc;
state.getActiveJob = () => requestJob;
state.checkChatRpc = () => true;
state.nativeFetch = async () => {nativeCalls += 1;throw new TypeError('dropped headers');};
await assert.rejects(sandbox.fetch('https://vertexaisearch.cloud.google.com/chat', {method:'POST', body:'{}'}), /dropped headers/);
state.checkChatRpc = realCheckChatRpc;
assert.equal(nativeCalls, 1);
assert.equal(requestJob.submissionStarted, true);
assert.ok(requestJob.networkError);
console.log('PASS: upgraded network hooks, one native request, early submission acknowledgement and fetch failure');

const decoratedSnapshot = job();
const streamedWithPreamble = '正在检查文件。\n\n' + tool;
state.parsePacket({groundedContent:{content:{text:streamedWithPreamble}}}, decoratedSnapshot);
state.parsePacket({summaryText:'[[section:检查]]正在检查文件。\n\n```codex_tool_call\n { "arguments": {"cmd":"cat a"}, "name":"exec_command" } \n```\n[[suggested_questions]]\n下一步？'}, decoratedSnapshot);
assert.equal((decoratedSnapshot.networkText.match(/codex_tool_call/g) || []).length, 1, 'decorated final snapshots must not duplicate tool calls or preamble');

const suffixSnapshot = job();
state.parsePacket({groundedContent:{content:{text:streamedWithPreamble}}}, suffixSnapshot);
state.parsePacket({summaryText:'正在检查文件。\n\n```codex_tool_call\n { "arguments": {"cmd":"cat a"}, "name":"exec_command" } \n```\n已完成检查。'}, suffixSnapshot);
assert.equal((suffixSnapshot.networkText.match(/codex_tool_call/g) || []).length, 1, 'snapshot suffix extraction must keep single tool call');
assert.ok(suffixSnapshot.networkText.endsWith('\n已完成检查。'), 'genuine new suffix in snapshot must be preserved');
console.log('PASS: decorated and suffix final snapshots without duplicating streamed content');

for (const marker of ['```']) {
  const fenced = job();
  const payload = {name:'exec_command', arguments:{cmd:'printf "``` [TOOL_CALL] [[section:literal]] [[suggested_questions]]"'}};
  const raw = marker + 'codex_tool_call\n' + JSON.stringify(payload) + '\n' + marker;
  state.parsePacket({groundedContent:{content:{text:'准备检查。\n\n'+raw}}}, fenced);
  state.parsePacket({summaryText:'[[section:检查]]准备检查。\n\n```codex_tool_call\n'+JSON.stringify({arguments:payload.arguments,name:payload.name},null,2)+'\n```\n[[suggested_questions]]\n下一步？'}, fenced);
  assert.equal(fenced.networkText, '准备检查。\n\n'+raw, 'new fence formatting must never duplicate commands');
  state.parsePacket({summaryText:'准备检查。\n\n```codex_tool_call\n'+JSON.stringify(payload)+'\n```\n检查结束。'}, fenced);
  assert.equal(fenced.networkText, '准备检查。\n\n'+raw+'\n检查结束。', 'new final prose suffix must survive');
}
const differentCommand = job();
const one = {name:'exec_command',arguments:{cmd:'printf "[[section:one]]"'}};
const two = {name:'exec_command',arguments:{cmd:'printf "[[section:two]]"'}};
state.parsePacket({groundedContent:{content:{text:'```codex_tool_call\n'+JSON.stringify(one)+'\n```'}}}, differentCommand);
state.parsePacket({summaryText:'```codex_tool_call\n'+JSON.stringify(two)+'\n```'}, differentCommand);
assert.ok(differentCommand.networkText.includes('section:two'), 'decorations inside arguments are data and cannot be erased by deduplication');
console.log('PASS: dedicated fences, variable lengths, literal decorations and real suffixes');
const adjacentHeader = job();
const adjacentTool = {name:'exec_command',arguments:{cmd:'cat seed.txt'}};
const adjacentRaw = '正在查看图片。\n[[section:查看图片与测试文件]]```codex_tool_call\n'+JSON.stringify(adjacentTool)+'\n```';
state.parsePacket({groundedContent:{content:{text:adjacentRaw}}},adjacentHeader);
state.parsePacket({summaryText:'正在查看图片。\n\n```codex_tool_call\n'+JSON.stringify(adjacentTool,null,2)+'\n```'},adjacentHeader);
assert.equal(adjacentHeader.networkText,adjacentRaw,'adjacent webpage section markers cannot duplicate a fenced tool call');
console.log('PASS: actual webpage section directly adjacent to a client fence');

assert.equal(state.checkChatRpc('https://vertexaisearch.cloud.google.com/_/DotsSplashUi/data/batchexecute', 'POST'), false);
assert.equal(state.checkChatRpc('https://vertexaisearch.cloud.google.com/v1alpha/projects/p/locations/global/collections/default_collection/engines/e/sessions:createSession', 'POST'), false);
assert.equal(state.checkChatRpc('https://vertexaisearch.cloud.google.com/v1alpha/projects/p/locations/global/collections/default_collection/engines/e/servingConfigs/default_search:streamAssist', 'POST'), true);

const isolatedJob = job();
let ctrlPrimary, ctrlBg;
const primaryStream = new ReadableStream({ start(c) { ctrlPrimary = c; } });
const bgStream = new ReadableStream({ start(c) { ctrlBg = c; } });
const enc = new TextEncoder();
const pPrimary = state.consumeStream(primaryStream, isolatedJob);
const pBg = state.consumeStream(bgStream, isolatedJob);
ctrlPrimary.enqueue(enc.encode(")]}'\n[{\"groundedContent\":{\"content\":{\"text\":\"主响应\"}}}"));
await new Promise((r) => setTimeout(r, 10));
assert.ok(isolatedJob.primaryStreamToken, 'first content stream locks primaryStreamToken');
ctrlBg.error(new Error('background rpc aborted'));
await pBg;
assert.equal(isolatedJob.networkError, undefined, 'secondary RPC stream failure cannot poison primary stream');
const utf8Bytes = enc.encode(', {\"groundedContent\":{\"content\":{\"text\":\"完整收尾\"}}}]');
ctrlPrimary.enqueue(utf8Bytes.slice(0, 31));
ctrlPrimary.enqueue(utf8Bytes.slice(31));
ctrlPrimary.close();
await pPrimary;
assert.equal(isolatedJob.networkText, '主响应完整收尾');
assert.equal(isolatedJob.networkDone, true);

const fencedSqJob = job();
const ticks3 = '\x60\x60\x60';
const sqToolRaw = ticks3 + 'codex_tool_call\n' + JSON.stringify({name:'exec_command',arguments:{cmd:'rg foo'}}) + '\n' + ticks3;
state.parsePacket({groundedContent:{content:{text:'检查中。\n' + sqToolRaw}}}, fencedSqJob);
state.parsePacket({summaryText:'检查中。\n' + ticks3 + 'codex_tool_call\n' + JSON.stringify({name:'exec_command',arguments:{cmd:'rg foo'}}, null, 2) + '\n' + ticks3 + '\n' + ticks3 + 'suggested_questions\n{"questions":["Next?"],"type":"followup"}\n' + ticks3}, fencedSqJob);
assert.equal((fencedSqJob.networkText.match(/codex_tool_call/g) || []).length, 1);
assert.ok(!fencedSqJob.networkText.includes('suggested_questions'));
console.log('PASS: batchexecute exclusion, primary stream lock, XSSI + UTF-8 split, and fenced suggested_questions snapshot');

// Truncated/unclosed summaryText snapshot must never corrupt an already-complete streamed tool call
const truncSummaryJob = job();
const longToolPayload = { name: 'create', arguments: { path: 'I:\\scene.py', file_text: 'x = 1\n'.repeat(800) } };
const fullStreamedTool = '我将创建场景脚本。\n\n' + ticks3 + 'codex_tool_call\n' + JSON.stringify(longToolPayload) + '\n' + ticks3;
state.parsePacket({ groundedContent: { content: { text: fullStreamedTool } } }, truncSummaryJob);
state.parsePacket({ summaryText: fullStreamedTool.slice(0, 1200) }, truncSummaryJob);
assert.equal(truncSummaryJob.networkText, fullStreamedTool, 'truncated summaryText must not append an unclosed duplicate tool block');

// Multi-leg RPC stream where Leg 1 closes mid-tool-fence releases primaryStreamToken so Leg 2 completes cleanly
const multiLegJob = job();
const fullMultiJson = JSON.stringify({ name: 'exec_command', arguments: { cmd: 'echo ' + 'A'.repeat(200) } });
const leg1Text = ticks3 + 'codex_tool_call\n' + fullMultiJson.slice(0, 80);
const leg2Text = fullMultiJson.slice(80) + '\n' + ticks3;
await state.consumeStream(stream(['[{"groundedContent":{"content":{"text":' + JSON.stringify(leg1Text) + '}}}]']), multiLegJob);
assert.equal(multiLegJob.primaryStreamToken, null, 'unclosed tool fence releases primaryStreamToken for continuation leg');
await state.consumeStream(stream(['[{"groundedContent":{"content":{"text":' + JSON.stringify(leg2Text) + '}}}]']), multiLegJob);
assert.equal(multiLegJob.networkText, leg1Text + leg2Text);
assert.equal(multiLegJob.networkDone, true);

// Thought-only RPC stream with zero visible text marks emptyRpcEndedAt cleanly without hanging
const thoughtOnlyJob = job();
await state.consumeStream(stream([')]}\'\n[{"groundedContent":{"content":{"text":"internal reasoning","thought":true}}}]']), thoughtOnlyJob);
assert.equal(thoughtOnlyJob.networkText, '');
assert.ok(!thoughtOnlyJob.networkDone);
assert.ok(typeof thoughtOnlyJob.emptyRpcEndedAt === 'number' && thoughtOnlyJob.emptyRpcEndedAt > 0);
console.log('PASS: truncated summaryText guard, multi-leg RPC continuation, and thought-only empty RPC detection');

// Multimodal <end_of_image> control tokens around fences must not break closing detection or duplicate summaryText
const eoiClosedJob = job();
const sshTool = { name: 'powershell', arguments: { command: 'ssh sakura-prod "echo ok"', description: 'Verify SSH' } };
const streamedEoiClosed =
  '<end_of_image>正在将本机公钥写入 60.205.9.176 的 authorized_keys。\n' +
  '<end_of_image>' + ticks3 + 'codex_tool_call\n' +
  JSON.stringify(sshTool) + '\n' +
  ticks3 + '<end_of_image>';
await state.consumeStream(stream(['[{"groundedContent":{"content":{"text":' + JSON.stringify(streamedEoiClosed) + '}}}]']), eoiClosedJob);
assert.ok(eoiClosedJob.primaryStreamToken !== null, 'closed fence with <end_of_image> must not be misclassified as unclosed');
state.parsePacket({
  summaryText:
    '正在将本机公钥写入 `60.205.9.176` 的 `authorized_keys`。\n\n' +
    ticks3 + 'codex_tool_call\n' +
    JSON.stringify(sshTool, null, 2) + '\n' +
    ticks3 + '\n' +
    ticks3 + 'suggested_questions\n{"questions":["Deploy now?"],"type":"followup"}\n' + ticks3
}, eoiClosedJob);
assert.equal((eoiClosedJob.networkText.match(/codex_tool_call/g) || []).length, 1, 'summaryText after <end_of_image> stream must never duplicate tool call');

// Unclosed streamed tool call where closing ``` only arrives in reformatted summaryText must close cleanly without pasting prose
const eoiUnclosedJob = job();
const streamedUnclosed =
  '<end_of_image>正在配置 authorized_keys。\n' +
  ticks3 + 'codex_tool_call\n' +
  JSON.stringify(sshTool);
state.parsePacket({ groundedContent: { content: { text: streamedUnclosed } } }, eoiUnclosedJob);
state.parsePacket({
  summaryText:
    '正在配置 `authorized_keys`。\n\n' +
    ticks3 + 'codex_tool_call\n' +
    JSON.stringify(sshTool, null, 2) + '\n' +
    ticks3
}, eoiUnclosedJob);
assert.equal(eoiUnclosedJob.networkText, streamedUnclosed + '\n' + ticks3, 'closing fence from reformatted summaryText must close open tool block cleanly');

// Host internal parser intercept swallowing closing ``` when Leg 2 emits nothing auto-closes complete JSON fence
const hostInterceptEmptyLeg2 = job();
await state.consumeStream(
  stream([
    '[{"groundedContent":{"content":{"text":' + JSON.stringify(streamedUnclosed) + '}}},' +
      '{"functionCall":{"name":"invalid_tool_call_notifier","args":{"tool_name":"unknown"}}},' +
      '{"groundedContent":{"content":{"text":""}}}]'
  ]),
  hostInterceptEmptyLeg2
);
assert.equal(hostInterceptEmptyLeg2.networkText, streamedUnclosed + '\n' + ticks3, 'host parser intercept with empty continuation must restore swallowed closing fence');

// Host internal parser intercept where Leg 2 re-emits prose + identical closed fence must be recognized as closed & deduplicated
const hostInterceptRetryLeg2 = job();
await state.consumeStream(
  stream([
    '[{"groundedContent":{"content":{"text":' + JSON.stringify(streamedUnclosed) + '}}},' +
      '{"functionCall":{"name":"invalid_tool_call_notifier","args":{"tool_name":"unknown"}}},' +
      '{"groundedContent":{"content":{"text":' +
      JSON.stringify('\n我将重新配置 authorized_keys：\n' + ticks3 + 'codex_tool_call\n' + JSON.stringify(sshTool) + '\n' + ticks3) +
      '}}}]'
  ]),
  hostInterceptRetryLeg2
);
assert.ok(hostInterceptRetryLeg2.primaryStreamToken !== null, 'superseded unclosed fence followed by valid closed retry must count as closed');

const api = sandbox.__GEB_MAIN_API__;
const bgSrc = fs.readFileSync(new URL('./background.js', import.meta.url), 'utf8');
const expectedMatch = bgSrc.match(/const\s+EXPECTED_DRIVER_VERSION\s*=\s*"([^"]+)"/);
assert.ok(expectedMatch, 'background.js must declare EXPECTED_DRIVER_VERSION');
assert.equal(api.version, expectedMatch[1], 'page-driver.js DRIVER_VERSION must stay in sync with background.js EXPECTED_DRIVER_VERSION');

assert.equal(api.computePureAppendToolCloseSuffix(streamedUnclosed), '\n' + ticks3);
assert.equal(api.computePureAppendToolCloseSuffix(streamedUnclosed + '\n'), ticks3);
assert.equal(api.computePureAppendToolCloseSuffix(streamedUnclosed + '\n\u0060\u0060'), '\u0060');
assert.equal(api.computePureAppendToolCloseSuffix(leg1Text), '', 'incomplete JSON must never receive premature closing fence');
console.log('PASS: <end_of_image> fence recognition, host parser intercept recovery, pure-append closure, and driver version sync');
