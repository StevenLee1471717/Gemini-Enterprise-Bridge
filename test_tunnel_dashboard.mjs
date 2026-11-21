import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const source = fs.readFileSync(new URL('dashboard_html.py', import.meta.url), 'utf8');
const start = source.indexOf('      function renderTunnel(tun, dist) {');
const end = source.indexOf('      function buildClientBundleText(', start);
assert.ok(start >= 0 && end > start);
const elements = new Map();
const element = (id) => {
  if (!elements.has(id)) elements.set(id, { textContent: '', className: '', value: '' });
  return elements.get(id);
};
const context = vm.createContext({
  document: { getElementById: element, activeElement: null },
  stripTrailingSlashes: (value) => value.replace(/\/+$/, ''),
  tunnelActionInFlight: false,
  lastTunnelSnapshot: null,
});
vm.runInContext(source.slice(start, end), context);
const origin = 'https://old-gateway.trycloudflare.com';
const distribution = { policy: { public_base_url: origin }, clients: [] };
const tunnel = { running: true, active_url: origin, mode: 'quick', cloudflared_installed: true };
const probe = (healthy) => ({
  public_url: origin, health_ok: healthy, ok: healthy,
  auth_lock_verified: true, admin_lock_verified: true, detail: healthy ? '正常' : '公网连接异常',
});

for (const running of [true, false]) {
  context.renderTunnel({ ...tunnel, running, active_url: running ? origin : '', last_probe: probe(false) }, distribution);
  assert.equal(element('ovTunnelVal').textContent, '公网未连通');
  assert.equal(element('ovTunnelBadge').className, 'chip warn');
  assert.equal(element('ovTunnelBadge').textContent, '连接异常 →');
  assert.equal(element('navTunnelChip').textContent, '连接异常');
  assert.doesNotMatch(element('tunnelStatusChip').textContent, /同步|在线/);
  assert.match(element('tunnelProbeBox').textContent, /HTTPS 连通: ✕/);
}

// A configured address and a live process alone do not verify external reachability.
for (const running of [true, false]) {
  context.renderTunnel({ ...tunnel, running, active_url: running ? origin : '' }, distribution);
  assert.equal(element('ovTunnelVal').textContent, '公网待自检');
  assert.doesNotMatch(element('ovTunnelBadge').textContent, /在线/);
}

context.renderTunnel({ ...tunnel, last_probe: probe(true) }, distribution);
assert.equal(element('ovTunnelVal').textContent, '公网已连通');
assert.equal(element('ovTunnelBadge').textContent, '在线 →');
assert.match(element('tunnelProbeBox').textContent, /HTTPS 连通: ✓/);

// Results from a previous address cannot change the current address's status.
context.renderTunnel({ ...tunnel, last_probe: { ...probe(false), public_url: 'https://previous.trycloudflare.com' } }, distribution);
assert.equal(element('ovTunnelVal').textContent, '公网待自检');
assert.equal(element('tunnelProbeBox').hidden, true);

// Recovery can assign a new address during either the automatic or manual self-test.
const startHandler = source.indexOf('      async function handleTunnelStart(mode, restartFlag) {');
const endStartHandler = source.indexOf("      document.getElementById('tunnelStartQuickBtn').addEventListener", startHandler);
const probeHandler = source.indexOf("      document.getElementById('tunnelProbeBtn').addEventListener('click', async function () {");
const endProbeHandler = source.indexOf("      document.getElementById('tunnelCopyUrlBtn').addEventListener", probeHandler);
assert.ok(startHandler >= 0 && endStartHandler > startHandler && probeHandler >= 0 && endProbeHandler > probeHandler);
const listeners = new Map();
const updatedDistributions = [];
const updatedTunnels = [];
const recoveredDistribution = { policy: { public_base_url: 'https://recovered.trycloudflare.com' } };
const recoveredTunnel = { active_url: recoveredDistribution.policy.public_base_url };
let replies = [];
const handlers = vm.createContext({
  document: { getElementById: (id) => {
    const node = element(id);
    node.addEventListener = (_event, listener) => listeners.set(id, listener);
    return node;
  } },
  buildAdminHeaders: () => ({}),
  fetch: async () => {
    const body = replies.shift();
    return { ok: body.httpOk !== false, json: async () => body };
  },
  renderDistribution: (value) => { updatedDistributions.push(value); handlers.lastDistSnapshot = value; },
  renderTunnel: (value, dist) => updatedTunnels.push({ tunnel: value, distribution: dist }),
  lastDefaultModel: 'gemini-4-carbon',
  lastDistSnapshot: distribution,
  distPolicyDirty: false,
  tunnelActionInFlight: false,
});
vm.runInContext(source.slice(startHandler, endStartHandler) + source.slice(probeHandler, endProbeHandler), handlers);
replies = [
  { ok: true, tunnel, distribution },
  { ok: true, tunnel: recoveredTunnel, distribution: recoveredDistribution },
];
await handlers.handleTunnelStart('quick', false);
assert.equal(updatedDistributions.at(-1), recoveredDistribution);
assert.equal(updatedTunnels.at(-1).distribution, recoveredDistribution);
assert.equal(updatedTunnels.at(-1).tunnel, recoveredTunnel);

updatedDistributions.length = 0;
updatedTunnels.length = 0;
handlers.lastDistSnapshot = distribution;
replies = [{ ok: true, tunnel: recoveredTunnel, distribution: recoveredDistribution }];
await listeners.get('tunnelProbeBtn')();
assert.equal(updatedDistributions.at(-1), recoveredDistribution);
assert.equal(updatedTunnels.at(-1).distribution, recoveredDistribution);
assert.equal(updatedTunnels.at(-1).tunnel, recoveredTunnel);

// Creating a process is not evidence that a failed self-test succeeded.
replies = [
  { ok: true, tunnel, distribution },
  { httpOk: false, detail: '自检服务暂时不可用' },
];
await handlers.handleTunnelStart('quick', false);
assert.equal(element('tunnelProbeBox').className, 'tunnel-probe-box warn');
assert.match(element('tunnelProbeBox').textContent, /自检未完成.*自检服务暂时不可用/);

console.log('PASS: tunnel status and recovered addresses update accurately in both self-test flows');
