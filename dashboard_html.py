"""Built-in local Web Status & Interactive Test Console served at http://127.0.0.1:8000/."""

from __future__ import annotations

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Gemini Enterprise Bridge · 本地控制台</title>
  <style>
    :root {
      color-scheme: dark light;
      --bg: #0b1020;
      --card: #131b33;
      --card-border: #253259;
      --text: #e8eeff;
      --muted: #95a4cc;
      --accent: #5b8cff;
      --accent-hover: #7aa2ff;
      --ok: #2dd4bf;
      --warn: #fbbf24;
      --err: #fb7185;
      --code-bg: #090e1c;
    }
    @media (prefers-color-scheme: light) {
      :root {
        --bg: #f4f7ff;
        --card: #ffffff;
        --card-border: #dbe3f8;
        --text: #16203b;
        --muted: #5a698f;
        --accent: #2563eb;
        --accent-hover: #1d4ed8;
        --ok: #0d9488;
        --warn: #d97706;
        --err: #e11d48;
        --code-bg: #eef3ff;
      }
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
      background: var(--bg);
      color: var(--text);
      line-height: 1.55;
    }
    .wrap {
      max-width: 1040px;
      margin: 0 auto;
      padding: 28px 20px 48px;
    }
    header {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      margin-bottom: 22px;
    }
    h1 {
      font-size: 1.45rem;
      margin: 0;
      display: flex;
      align-items: center;
      gap: 10px;
    }
    .badge {
      display: inline-flex;
      align-items: center;
      gap: 7px;
      padding: 5px 12px;
      border-radius: 999px;
      font-size: 0.85rem;
      font-weight: 600;
      border: 1px solid var(--card-border);
      background: var(--card);
    }
    .dot {
      width: 9px;
      height: 9px;
      border-radius: 50%;
      background: var(--warn);
    }
    .dot.ok { background: var(--ok); box-shadow: 0 0 8px var(--ok); }
    .dot.err { background: var(--err); }
    .grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
      gap: 16px;
      margin-bottom: 18px;
    }
    .card {
      background: var(--card);
      border: 1px solid var(--card-border);
      border-radius: 14px;
      padding: 18px;
    }
    .card h2 {
      font-size: 1rem;
      margin: 0 0 12px;
      color: var(--text);
    }
    .kv {
      display: flex;
      justify-content: space-between;
      gap: 12px;
      padding: 6px 0;
      border-bottom: 1px dashed var(--card-border);
      font-size: 0.9rem;
    }
    .kv:last-child { border-bottom: none; }
    .kv span:first-child { color: var(--muted); }
    .mono {
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 0.85rem;
      background: var(--code-bg);
      padding: 3px 7px;
      border-radius: 6px;
      word-break: break-all;
    }
    .chat-box {
      display: flex;
      flex-direction: column;
      gap: 10px;
    }
    textarea {
      width: 100%;
      min-height: 82px;
      border-radius: 10px;
      border: 1px solid var(--card-border);
      background: var(--code-bg);
      color: var(--text);
      padding: 10px 12px;
      font: inherit;
      resize: vertical;
    }
    .actions {
      display: flex;
      gap: 10px;
      align-items: center;
      flex-wrap: wrap;
    }
    button {
      background: var(--accent);
      color: #fff;
      border: none;
      border-radius: 8px;
      padding: 8px 16px;
      font-weight: 600;
      cursor: pointer;
      transition: background 0.15s;
    }
    button:hover { background: var(--accent-hover); }
    button.secondary {
      background: transparent;
      color: var(--text);
      border: 1px solid var(--card-border);
    }
    pre#replyOut {
      margin: 6px 0 0;
      min-height: 90px;
      max-height: 360px;
      overflow: auto;
      padding: 12px;
      border-radius: 10px;
      background: var(--code-bg);
      border: 1px solid var(--card-border);
      white-space: pre-wrap;
      word-wrap: break-word;
      font-size: 0.9rem;
    }
    .logs {
      max-height: 190px;
      overflow: auto;
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 0.8rem;
      background: var(--code-bg);
      border-radius: 10px;
      padding: 10px;
      border: 1px solid var(--card-border);
    }
    .log-item { padding: 3px 0; border-bottom: 1px solid rgba(128,128,128,0.12); }
    .muted { color: var(--muted); font-size: 0.85rem; }
    .num-tab { font-variant-numeric: tabular-nums; }
    .cost-hero {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(210px, 1fr));
      gap: 12px;
      margin-bottom: 14px;
    }
    .stat-tile {
      padding: 12px 14px;
      border-radius: 10px;
      background: var(--code-bg);
      border: 1px solid var(--card-border);
    }
    .stat-tile .label { font-size: 0.8rem; color: var(--muted); }
    .stat-tile .big {
      font-size: 1.45rem;
      font-weight: 800;
      color: var(--ok);
      margin: 2px 0;
    }
    .stat-tile .sub { font-size: 0.8rem; color: var(--muted); }
    .ctx-bar {
      width: 100%;
      height: 8px;
      appearance: none;
      -webkit-appearance: none;
      border: none;
      border-radius: 999px;
      overflow: hidden;
      background: var(--code-bg);
      margin: 6px 0;
    }
    .ctx-bar::-webkit-progress-bar { background: var(--code-bg); border-radius: 999px; }
    .ctx-bar::-webkit-progress-value { background: linear-gradient(90deg, var(--ok), var(--accent)); border-radius: 999px; }
    .ctx-bar.warn::-webkit-progress-value { background: var(--warn); }
    .ctx-bar.danger::-webkit-progress-value { background: var(--err); }
    table.usage-table {
      width: 100%;
      border-collapse: collapse;
      font-size: 0.85rem;
      margin-top: 10px;
    }
    table.usage-table th,
    table.usage-table td {
      padding: 7px 8px;
      text-align: left;
      border-bottom: 1px solid var(--card-border);
    }
    table.usage-table th { color: var(--muted); font-weight: 600; }
  </style>
</head>
<body>
  <div class="wrap">
    <header>
      <div>
        <h1>Gemini Enterprise Bridge <span class="mono" id="dashVerBadge" style="font-size:0.8rem;font-weight:600;">v1.1.0</span></h1>
        <div class="muted">Vertex AI Search / Agentspace 浏览器回路 OpenAI API 本地代理网关</div>
      </div>
      <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;">
        <button id="checkUpdateDashBtn" class="secondary" type="button" style="padding:6px 12px;font-size:0.82rem;">检查 GitHub 更新</button>
        <div class="badge" id="connBadge">
          <span class="dot" id="connDot"></span>
          <span id="connText">正在检测状态...</span>
        </div>
      </div>
    </header>

    <section class="card" id="dashUpdateBanner" hidden style="margin-bottom: 18px; border-color: var(--warn);">
      <div style="display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap;">
        <div>
          <strong id="dashUpdateTitle" style="color:var(--warn);">检测到新版本可用</strong>
          <div id="dashUpdateDesc" class="muted" style="margin-top:4px;color:var(--text);"></div>
        </div>
        <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;">
          <button id="applyUpdateDashBtn" type="button">立即从 GitHub 更新后端与插件</button>
          <a id="dashReleaseLink" href="https://github.com/StevenLee1471717/Gemini-Enterprise-Bridge/releases" target="_blank" rel="noreferrer" class="mono" style="text-decoration:none;color:var(--accent);">查看 Release</a>
        </div>
      </div>
    </section>

    <div class="grid">
      <section class="card">
        <h2>第三方客户端接入参数 (Cursor / Cline / ChatBox / NextChat)</h2>
        <div class="kv"><span>API Host / Base URL</span><span class="mono" id="baseUrlVal">http://127.0.0.1:8000/v1</span></div>
        <div class="kv"><span>API Key</span><span class="mono">sk-local-bridge (任意填写或按 .env)</span></div>
        <div class="kv"><span>可选网页模型</span><span class="mono">gemini-3.1-pro · gemini-4-argon · gemini-4-carbon · gemini-3.8-flash</span></div>
        <div class="kv"><span>默认/固定会话</span><span class="mono">gemini-4-argon / gemini-4-carbon / 4carbon@sess1</span></div>
        <div class="kv"><span>能力支持</span><span class="mono">多会话并行池 · Tools · Agentic Loop</span></div>
      </section>

      <section class="card">
        <h2>实时并行工作池状态</h2>
        <div class="kv"><span>运行模式</span><span class="mono" id="modeVal">-</span></div>
        <div class="kv"><span>连接通道</span><span class="mono" id="transportVal">未连接</span></div>
        <div class="kv"><span>并行工作标签页池</span><span class="mono" id="tabVal">等待打开 vertexaisearch.cloud.google.com</span></div>
        <div class="kv"><span>当前网页模型 / RPC</span><span class="mono" id="rpcVal">-</span></div>
        <div class="kv"><span>并发执行 / 排队 / 累计</span><span class="mono" id="statsVal">0 / 0 / 0</span></div>
      </section>
    </div>

    <section class="card num-tab" style="margin-bottom: 18px;">
      <div style="display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:10px;">
        <h2 style="margin:0;">Context 实时窗口监控 &amp; 官方 Google Gemini API 等价账单</h2>
        <div style="display:flex;gap:8px;align-items:center;">
          <span class="mono" id="dashActivePricingBadge">4 Carbon 官方价: $2/$10 / 1M</span>
          <button id="resetUsageDashBtn" class="secondary" type="button" style="padding:4px 10px;font-size:0.8rem;">重置统计</button>
        </div>
      </div>
      <div class="cost-hero">
        <div class="stat-tile">
          <div class="label">累计官方无缓存等价节省</div>
          <div class="big" id="dashAllUsd">$0.00</div>
          <div class="sub" id="dashAllSub">≈ ¥0.00 · 含缓存折算 $0.00</div>
        </div>
        <div class="stat-tile">
          <div class="label">今日等价消耗</div>
          <div class="big" style="color:var(--text);" id="dashTodayUsd">$0.00</div>
          <div class="sub" id="dashTodaySub">≈ ¥0.00 · 0 次请求</div>
        </div>
        <div class="stat-tile">
          <div class="label">累计输入 / 缓存命中 / 输出 Tokens</div>
          <div class="big" style="color:var(--accent);font-size:1.2rem;" id="dashTokenTotals">0 / 0 / 0</div>
          <div class="sub" id="dashSessionSub">本次服务启动: $0.00 (0 次)</div>
        </div>
      </div>
      <div>
        <div style="display:flex;justify-content:space-between;align-items:center;font-size:0.88rem;">
          <span id="dashCtxTitle">主会话 Context 窗口占用: 空闲待命</span>
          <strong id="dashCtxPct" class="mono">0 / 1.05M (0.0%)</strong>
        </div>
        <progress id="dashCtxBar" class="ctx-bar" max="100" value="0"></progress>
        <div id="dashSlotsCtxList" class="muted" style="margin-top:4px;"></div>
      </div>
      <table class="usage-table">
        <thead>
          <tr>
            <th>计费模型</th>
            <th>官方单价 (In / Cache / Out per 1M)</th>
            <th>请求数</th>
            <th>输入 / 输出 Tokens</th>
            <th>标准等价 USD (CNY)</th>
            <th>含缓存等价 USD</th>
          </tr>
        </thead>
        <tbody id="dashModelTableBody">
          <tr><td colspan="6" class="muted">暂无模型调用记录</td></tr>
        </tbody>
      </table>
    </section>

    <section class="card" style="margin-bottom: 18px;">
      <h2>在线流式联调测试 (SSE `/v1/chat/completions`)</h2>
      <div class="chat-box">
        <textarea id="promptInput" placeholder="输入测试问题，例如：请用三句话总结什么是 Google Cloud Agentspace？"></textarea>
        <div class="actions">
          <select id="modelSelect" style="padding:7px 10px;border-radius:8px;border:1px solid var(--card-border);background:var(--code-bg);color:var(--text);font:inherit;">
            <option value="gemini-enterprise-web">gemini-enterprise-web (保持当前模型)</option>
            <option value="gemini-3.1-pro">gemini-3.1-pro (自动切换 3.1 Pro)</option>
            <option value="gemini-4-argon">gemini-4-argon (自动切换 4 Argon)</option>
            <option value="gemini-4-carbon">gemini-4-carbon (自动切换 4 Carbon)</option>
            <option value="gemini-3.8-flash">gemini-3.8-flash (自动切换 3.8 Flash)</option>
            <option value="gemini-2.5-pro">gemini-2.5-pro (自动切换 2.5 Pro)</option>
            <option value="gemini-2.5-flash">gemini-2.5-flash (自动切换 2.5 Flash)</option>
          </select>
          <button id="sendBtn" type="button">发送流式测试</button>
          <button id="refreshBtn" class="secondary" type="button">刷新状态</button>
          <span class="muted" id="testHint">提示：请先在 Chrome/Edge 打开并登录 vertexaisearch.cloud.google.com 且加载扩展。</span>
        </div>
        <pre id="replyOut">等待测试输出...</pre>
      </div>
    </section>

    <section class="card">
      <h2>最近桥接日志</h2>
      <div class="logs" id="logsBox">暂无日志</div>
    </section>
  </div>

  <script>
    const baseOrigin = window.location.origin;
    document.getElementById('baseUrlVal').textContent = baseOrigin + '/v1';

    function fmtTok(n) {
      const v = Number(n || 0);
      if (v >= 1000000) return (v / 1000000).toFixed(2) + 'M';
      if (v >= 1000) return (v / 1000).toFixed(1) + 'K';
      return String(Math.round(v));
    }
    function fmtUsd(n) {
      const v = Number(n || 0);
      if (v <= 0) return '$0.00';
      return v < 0.01 ? ('$' + v.toFixed(4)) : ('$' + v.toFixed(2));
    }
    function fmtCny(n) {
      const v = Number(n || 0);
      if (v <= 0) return '¥0.00';
      return v < 0.01 ? ('¥' + v.toFixed(3)) : ('¥' + v.toFixed(2));
    }
    function renderDashboardUsage(usage, ctxMon) {
      const allT = usage.all_time || {};
      const todayT = usage.today || {};
      const sessT = usage.current_session || {};
      const ap = usage.active_pricing || {};
      if (ap.display_name) {
        document.getElementById('dashActivePricingBadge').textContent =
          ap.display_name + ' 官方价: $' + ap.input_per_1m + '/$' + ap.output_per_1m + ' / 1M';
      }
      document.getElementById('dashAllUsd').textContent = fmtUsd(allT.equivalent_cost_usd);
      document.getElementById('dashAllSub').textContent =
        '≈ ' + fmtCny(allT.equivalent_cost_cny) + ' · 含缓存折算 ' + fmtUsd(allT.cached_equivalent_cost_usd) + ' · 共 ' + (allT.requests || 0) + ' 次';
      document.getElementById('dashTodayUsd').textContent = fmtUsd(todayT.equivalent_cost_usd);
      document.getElementById('dashTodaySub').textContent =
        '≈ ' + fmtCny(todayT.equivalent_cost_cny) + ' · ' + (todayT.requests || 0) + ' 次请求';
      document.getElementById('dashTokenTotals').textContent =
        fmtTok(allT.prompt_tokens) + ' / ' + fmtTok(allT.cached_tokens) + ' / ' + fmtTok(allT.completion_tokens);
      document.getElementById('dashSessionSub').textContent =
        '本次进程启动: ' + fmtUsd(sessT.equivalent_cost_usd) + ' (' + (sessT.requests || 0) + ' 次)';

      const prim = ctxMon.primary || {};
      const cTok = Number(prim.total_context_tokens || 0);
      const cLim = Number(prim.context_limit_tokens || 1048576);
      const pct = Number(prim.utilization_pct || 0);
      document.getElementById('dashCtxPct').textContent =
        fmtTok(cTok) + ' / ' + fmtTok(cLim) + ' (' + pct.toFixed(1) + '%)';
      const bar = document.getElementById('dashCtxBar');
      bar.value = Math.min(100, Math.max(0, pct));
      bar.className = pct >= 85 ? 'ctx-bar danger' : (pct >= 60 ? 'ctx-bar warn' : 'ctx-bar');
      document.getElementById('dashCtxTitle').textContent =
        (prim.turn_count || 0) > 0
          ? ('主活跃会话 (' + (prim.active_model || 'Auto') + ' · 第 ' + (prim.user_turns || prim.turn_count) + ' 轮 · ' + (prim.last_strategy || '') + ')')
          : '主会话 Context 窗口占用: 空闲待命';
      const slotItems = Array.isArray(ctxMon.slots) ? ctxMon.slots : [];
      document.getElementById('dashSlotsCtxList').textContent =
        slotItems.length > 0
          ? slotItems.map(function (s) {
              return (s.tab_id ? ('Tab #' + s.tab_id) : s.slot_key) + ' [' + (s.active_model || 'Auto') + ']: ' + fmtTok(s.total_context_tokens) + ' (' + Number(s.utilization_pct || 0).toFixed(1) + '%)';
            }).join('  |  ')
          : '';

      const tbody = document.getElementById('dashModelTableBody');
      tbody.replaceChildren();
      const entries = Object.entries(usage.by_model || {});
      if (entries.length === 0) {
        const tr = document.createElement('tr');
        const td = document.createElement('td');
        td.colSpan = 6;
        td.className = 'muted';
        td.textContent = '暂无模型调用记录';
        tr.appendChild(td);
        tbody.appendChild(tr);
      } else {
        for (const [mKey, info] of entries) {
          const tr = document.createElement('tr');
          const cells = [
            info.display_name || mKey,
            '$' + info.input_rate_per_1m + ' / $' + info.cached_input_rate_per_1m + ' / $' + info.output_rate_per_1m,
            String(info.requests || 0),
            fmtTok(info.prompt_tokens) + ' / ' + fmtTok(info.completion_tokens),
            fmtUsd(info.equivalent_cost_usd) + ' (' + fmtCny(info.equivalent_cost_cny) + ')',
            fmtUsd(info.cached_equivalent_cost_usd)
          ];
          for (const val of cells) {
            const td = document.createElement('td');
            td.textContent = val;
            tr.appendChild(td);
          }
          tbody.appendChild(tr);
        }
      }
    }

    async function refreshStatus() {
      try {
        const res = await fetch('/api/status');
        const data = await res.json();
        const dot = document.getElementById('connDot');
        const txt = document.getElementById('connText');
        const active = data.hub && data.hub.active_client;

        document.getElementById('modeVal').textContent =
          data.backend_mode + ' / context=' + data.context_mode;

        const hub = data.hub || {};
        const slots = hub.worker_slots || [];

        if (active || slots.length > 0) {
          dot.className = 'dot ok';
          txt.textContent = slots.length > 1
            ? ('并行池已就绪 (' + slots.length + ' Tabs)')
            : '浏览器已挂载';
          document.getElementById('transportVal').textContent =
            (active ? active.transport + ' (' + active.driver_mode + ')' : 'multi-slot') +
            (hub.auto_spawn_tabs ? ' · 自动扩容(≤' + (hub.max_parallel_tabs || 4) + ')' : '');
          if (slots.length > 0) {
            const summary = slots.map(function (s) {
              return '#' + (s.tab_id || 'tm') + (s.busy ? '[忙碌]' : '[空闲]') + (s.active_model ? '(' + s.active_model + ')' : '');
            }).join('  ');
            document.getElementById('tabVal').textContent = summary;
          } else {
            document.getElementById('tabVal').textContent =
              (active.tab_title ? active.tab_title + ' · ' : '') + (active.tab_url || '已就绪');
          }
          const modelLabel = (active && active.active_model) ? (active.active_model + ' · ') : '';
          document.getElementById('rpcVal').textContent =
            modelLabel + ((active && active.captured_rpc) ? '已捕获 RPC 模板' : '被动监听 + Shadow DOM');
        } else if (data.gcloud_configured && data.backend_mode !== 'browser') {
          dot.className = 'dot ok';
          txt.textContent = 'GCloud 官方模式就绪';
          document.getElementById('transportVal').textContent = 'gcloud ADC';
          document.getElementById('tabVal').textContent = '无需浏览器页面';
          document.getElementById('rpcVal').textContent = '官方 API';
        } else {
          dot.className = 'dot err';
          txt.textContent = '等待浏览器挂载';
          document.getElementById('transportVal').textContent = '未连接';
          document.getElementById('tabVal').textContent = '请打开 vertexaisearch.cloud.google.com';
          document.getElementById('rpcVal').textContent = '-';
        }

        document.getElementById('statsVal').textContent =
          (hub.active_slots_busy || 0) + ' 并发 / ' +
          (hub.queued_requests || 0) + ' 排队 / ' +
          (hub.total_requests || 0) + ' 总计';

        if (data.backend_version) {
          const extTag = data.connected_extension_version ? (' · 插件 v' + data.connected_extension_version) : '';
          document.getElementById('dashVerBadge').textContent = '后端 v' + data.backend_version + extTag;
        }
        renderDashboardUpdateBanner(data);

        renderDashboardUsage(data.usage || {}, data.context_monitor || {});

        const logs = hub.recent_logs || [];
        const logsBox = document.getElementById('logsBox');
        if (logs.length === 0) {
          logsBox.textContent = '暂无日志';
        } else {
          logsBox.replaceChildren();
          for (const item of logs.slice().reverse()) {
            const div = document.createElement('div');
            div.className = 'log-item';
            const t = new Date(item.ts * 1000).toLocaleTimeString();
            div.textContent = '[' + t + '] [' + item.level.toUpperCase() + '] ' + item.message;
            logsBox.appendChild(div);
          }
        }
      } catch (err) {
        document.getElementById('connDot').className = 'dot err';
        document.getElementById('connText').textContent = '后端连接异常';
      }
    }

    async function sendTestPrompt() {
      const input = document.getElementById('promptInput');
      const out = document.getElementById('replyOut');
      const btn = document.getElementById('sendBtn');
      const prompt = input.value.trim();
      if (!prompt) {
        out.textContent = '请输入测试问题后再发送。';
        return;
      }
      btn.disabled = true;
      out.textContent = '';
      const chosenModel = document.getElementById('modelSelect').value || 'gemini-enterprise-web';
      try {
        const res = await fetch('/v1/chat/completions', {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'Authorization': 'Bearer sk-local-bridge'
          },
          body: JSON.stringify({
            model: chosenModel,
            stream: true,
            messages: [{ role: 'user', content: prompt }]
          })
        });
        if (!res.ok) {
          const errText = await res.text();
          out.textContent = 'HTTP ' + res.status + ':\\n' + errText;
          return;
        }
        const reader = res.body.getReader();
        const decoder = new TextDecoder('utf-8');
        let buffer = '';
        while (true) {
          const { value, done } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          const lines = buffer.split('\\n');
          buffer = lines.pop() || '';
          for (const line of lines) {
            const trimmed = line.trim();
            if (!trimmed.startsWith('data:')) continue;
            const payload = trimmed.slice(5).trim();
            if (payload === '[DONE]') continue;
            try {
              const parsed = JSON.parse(payload);
              const delta = parsed.choices && parsed.choices[0] && parsed.choices[0].delta;
              if (delta && delta.content) {
                out.textContent += delta.content;
              }
            } catch (_) {}
          }
        }
      } catch (err) {
        out.textContent = '请求失败: ' + err;
      } finally {
        btn.disabled = false;
        await refreshStatus();
      }
    }

    function renderDashboardUpdateBanner(data) {
      const banner = document.getElementById('dashUpdateBanner');
      const titleEl = document.getElementById('dashUpdateTitle');
      const descEl = document.getElementById('dashUpdateDesc');
      const linkEl = document.getElementById('dashReleaseLink');
      const upd = data.update || {};
      const bVer = data.backend_version || upd.current_backend_version || '';
      const eVer = data.connected_extension_version || upd.connected_extension_version || '';
      const gVer = upd.latest_github_version || '';
      if (upd.release_url) {
        linkEl.href = upd.release_url;
      }
      if (data.backend_update_required || upd.backend_outdated) {
        banner.hidden = false;
        const targetV = gVer || eVer || 'latest';
        titleEl.textContent = '需要更新本地后端（目标版本 v' + targetV + '）';
        descEl.textContent =
          (eVer && eVer !== bVer
            ? ('浏览器插件已自动更新至 v' + eVer + '，而当前本地后端仍为 v' + bVer + '。')
            : ('检测到 GitHub Release 新版本 v' + targetV + '（当前本地后端 v' + bVer + '）。')) +
          ' 点击右侧按钮即可从 GitHub Releases 自动下载更新包（保留 .env 与历史统计）并平滑重启。';
      } else if (upd.extension_outdated) {
        banner.hidden = false;
        titleEl.textContent = '浏览器扩展待同步更新（当前插件 v' + eVer + ' -> v' + (gVer || bVer) + '）';
        descEl.textContent = '后端已就绪并通知扩展空闲时自动重载；也可点击右侧按钮立即从 GitHub 同步 extension/ 目录。';
      } else {
        banner.hidden = true;
      }
    }

    document.getElementById('checkUpdateDashBtn').addEventListener('click', async function () {
      const btn = document.getElementById('checkUpdateDashBtn');
      btn.disabled = true;
      btn.textContent = '检查中...';
      try {
        const res = await fetch('/api/update/status?force=true');
        const u = await res.json();
        await refreshStatus();
        if (!u.update_available) {
          btn.textContent = '已是最新 (v' + (u.current_backend_version || '1.1.0') + ')';
          setTimeout(function () { btn.textContent = '检查 GitHub 更新'; }, 2500);
        } else {
          btn.textContent = '发现 v' + (u.latest_github_version || u.highest_known_version);
        }
      } catch (_) {
        btn.textContent = '检查失败';
        setTimeout(function () { btn.textContent = '检查 GitHub 更新'; }, 2500);
      } finally {
        btn.disabled = false;
      }
    });

    document.getElementById('applyUpdateDashBtn').addEventListener('click', async function () {
      const btn = document.getElementById('applyUpdateDashBtn');
      const descEl = document.getElementById('dashUpdateDesc');
      btn.disabled = true;
      btn.textContent = '正在从 GitHub 下载并更新...';
      try {
        const res = await fetch('/api/update/apply', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ restart: true })
        });
        const out = await res.json();
        if (res.ok && out.ok) {
          descEl.textContent = out.message || ('已更新至 v' + out.updated_version + '，正在重启服务...');
          setTimeout(refreshStatus, 3200);
        } else {
          descEl.textContent = '自动更新失败: ' + (out.error || ('HTTP ' + res.status));
        }
      } catch (err) {
        descEl.textContent = '后端正在重启以应用新版本，约 3 秒后自动恢复连接...';
        setTimeout(refreshStatus, 3200);
      } finally {
        setTimeout(function () {
          btn.disabled = false;
          btn.textContent = '立即从 GitHub 更新后端与插件';
        }, 3500);
      }
    });

    document.getElementById('sendBtn').addEventListener('click', sendTestPrompt);
    document.getElementById('refreshBtn').addEventListener('click', refreshStatus);
    document.getElementById('resetUsageDashBtn').addEventListener('click', async function () {
      await fetch('/api/usage/reset', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ scope: 'all' })
      });
      await refreshStatus();
    });
    refreshStatus();
    setInterval(refreshStatus, 3000);
  </script>
</body>
</html>
"""
