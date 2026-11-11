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
  </style>
</head>
<body>
  <div class="wrap">
    <header>
      <div>
        <h1>Gemini Enterprise Bridge</h1>
        <div class="muted">Vertex AI Search / Agentspace 浏览器回路 OpenAI API 本地代理网关</div>
      </div>
      <div class="badge" id="connBadge">
        <span class="dot" id="connDot"></span>
        <span id="connText">正在检测状态...</span>
      </div>
    </header>

    <div class="grid">
      <section class="card">
        <h2>第三方客户端接入参数 (Cursor / Cline / ChatBox / NextChat)</h2>
        <div class="kv"><span>API Host / Base URL</span><span class="mono" id="baseUrlVal">http://127.0.0.1:8000/v1</span></div>
        <div class="kv"><span>API Key</span><span class="mono">sk-local-bridge (任意填写或按 .env)</span></div>
        <div class="kv"><span>可选网页模型</span><span class="mono">gemini-3.1-pro · gemini-4-argon · gemini-3.8-flash</span></div>
        <div class="kv"><span>默认/新建会话</span><span class="mono">gemini-enterprise-web / *-newchat</span></div>
        <div class="kv"><span>能力支持</span><span class="mono">SSE 流式 · OpenAI Tools · Agentic Loop</span></div>
      </section>

      <section class="card">
        <h2>实时桥接状态</h2>
        <div class="kv"><span>运行模式</span><span class="mono" id="modeVal">-</span></div>
        <div class="kv"><span>连接通道</span><span class="mono" id="transportVal">未连接</span></div>
        <div class="kv"><span>挂载标签页</span><span class="mono" id="tabVal">等待打开 vertexaisearch.cloud.google.com</span></div>
        <div class="kv"><span>当前网页模型 / RPC</span><span class="mono" id="rpcVal">-</span></div>
        <div class="kv"><span>队列 / 累计请求</span><span class="mono" id="statsVal">0 / 0</span></div>
      </section>
    </div>

    <section class="card" style="margin-bottom: 18px;">
      <h2>在线流式联调测试 (SSE `/v1/chat/completions`)</h2>
      <div class="chat-box">
        <textarea id="promptInput" placeholder="输入测试问题，例如：请用三句话总结什么是 Google Cloud Agentspace？"></textarea>
        <div class="actions">
          <select id="modelSelect" style="padding:7px 10px;border-radius:8px;border:1px solid var(--card-border);background:var(--code-bg);color:var(--text);font:inherit;">
            <option value="gemini-enterprise-web">gemini-enterprise-web (保持当前模型)</option>
            <option value="gemini-3.1-pro">gemini-3.1-pro (自动切换 3.1 Pro)</option>
            <option value="gemini-4-argon">gemini-4-argon (自动切换 4 Argon)</option>
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

    async function refreshStatus() {
      try {
        const res = await fetch('/api/status');
        const data = await res.json();
        const dot = document.getElementById('connDot');
        const txt = document.getElementById('connText');
        const active = data.hub && data.hub.active_client;

        document.getElementById('modeVal').textContent =
          data.backend_mode + ' / context=' + data.context_mode;

        if (active) {
          dot.className = 'dot ok';
          txt.textContent = '浏览器已挂载';
          document.getElementById('transportVal').textContent =
            active.transport + ' (' + active.driver_mode + ')';
          document.getElementById('tabVal').textContent =
            (active.tab_title ? active.tab_title + ' · ' : '') + (active.tab_url || '已就绪');
          const modelLabel = active.active_model ? (active.active_model + ' · ') : '';
          document.getElementById('rpcVal').textContent =
            modelLabel + (active.captured_rpc ? '已捕获 RPC 模板' : '被动监听 + Shadow DOM');
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

        const hub = data.hub || {};
        document.getElementById('statsVal').textContent =
          (hub.queued_requests || 0) + ' 排队 / ' + (hub.total_requests || 0) + ' 总计';

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

    document.getElementById('sendBtn').addEventListener('click', sendTestPrompt);
    document.getElementById('refreshBtn').addEventListener('click', refreshStatus);
    refreshStatus();
    setInterval(refreshStatus, 3000);
  </script>
</body>
</html>
"""
