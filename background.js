// MV3 Background Service Worker for Gemini Enterprise Web-to-API Bridge
// Maintains persistent WebSocket to local FastAPI backend (outside webpage CSP)
// and routes commands to active vertexaisearch.cloud.google.com tabs.

const DEFAULT_SETTINGS = {
  wsUrl: "ws://127.0.0.1:8000/ws/browser",
  enabled: true,
  driverMode: "auto", // auto | dom | replay
  autoSpawnTabs: true,
  maxParallelTabs: 6,
  customModelSelector: "",
  customInputSelector: "",
  customSendSelector: "",
  customReplySelector: ""
};

const ALARM_NAME = "geb-keepalive-alarm";
const VERTEX_MATCH_PATTERN = "https://vertexaisearch.cloud.google.com/*";

// Live non-serializable WebSocket handle & per-tab fast caches; durable settings live in chrome.storage
const socketRuntime = {
  ws: null,
  connecting: false,
  heartbeatTimer: null,
  reconnectTimer: null,
  reconnectAttempts: 0
};

const runtimeTabMeta = new Map(); // tabId -> { activeModel, capturedRpc }
const injectedTabVersions = new Map(); // tabId -> driverVersion

async function getSettings() {
  const stored = await chrome.storage.local.get(Object.keys(DEFAULT_SETTINGS));
  return { ...DEFAULT_SETTINGS, ...stored };
}

async function getClientId() {
  const data = await chrome.storage.local.get("clientId");
  if (data.clientId) {
    return data.clientId;
  }
  const generated = "ext-" + Math.random().toString(36).slice(2, 10);
  await chrome.storage.local.set({ clientId: generated });
  return generated;
}

async function appendLog(level, message) {
  try {
    const { logs = [] } = await chrome.storage.session.get("logs");
    const next = logs.concat([
      {
        ts: Date.now(),
        level,
        message: String(message).slice(0, 400)
      }
    ]);
    if (next.length > 60) {
      next.splice(0, next.length - 60);
    }
    await chrome.storage.session.set({ logs: next });
  } catch (_) {
    // Ignore storage race errors during shutdown
  }
}

async function setSessionStatus(patch) {
  try {
    const current = await chrome.storage.session.get([
      "wsConnected",
      "lastError",
      "activeTabId",
      "activeTabUrl",
      "activeTabTitle",
      "capturedRpc",
      "tabCount"
    ]);
    const next = { ...current, ...patch };
    await chrome.storage.session.set(next);
    await updateActionBadge(next);
  } catch (_) {}
}

async function updateActionBadge(status) {
  const settings = await getSettings();
  if (!settings.enabled) {
    await chrome.action.setBadgeText({ text: "OFF" });
    await chrome.action.setBadgeBackgroundColor({ color: "#64748B" });
    return;
  }
  if (status.wsConnected && status.activeTabId) {
    const count = Number(status.tabCount) || 1;
    await chrome.action.setBadgeText({ text: count > 1 ? `${count}T` : "ON" });
    await chrome.action.setBadgeBackgroundColor({ color: "#0D9488" });
  } else if (status.wsConnected) {
    await chrome.action.setBadgeText({ text: "TAB" });
    await chrome.action.setBadgeBackgroundColor({ color: "#D97706" });
  } else {
    await chrome.action.setBadgeText({ text: "ERR" });
    await chrome.action.setBadgeBackgroundColor({ color: "#E11D48" });
  }
}

async function getEligibleVertexTabs() {
  const tabs = await chrome.tabs.query({ url: VERTEX_MATCH_PATTERN });
  if (!tabs || tabs.length === 0) {
    return [];
  }
  const { preferredTabId } = await chrome.storage.session.get("preferredTabId");
  if (preferredTabId) {
    const pinned = tabs.find((t) => t.id === preferredTabId);
    if (pinned) {
      return [pinned];
    }
  }
  return tabs.filter((t) => Boolean(t.id));
}

async function findTargetVertexTab(requestedTabId = null) {
  if (requestedTabId !== null && requestedTabId !== undefined) {
    try {
      const exact = await chrome.tabs.get(Number(requestedTabId));
      if (exact && exact.id) {
        return exact;
      }
    } catch (_) {}
  }
  return await findBestVertexTab();
}

async function findBestVertexTab() {
  const tabs = await getEligibleVertexTabs();
  if (tabs.length === 0) {
    return null;
  }
  const activeInWindow = tabs.find((t) => t.active);
  return activeInWindow || tabs[0];
}

async function protectTabFromFreeze(tabId) {
  if (!tabId) return;
  try {
    await chrome.tabs.update(tabId, { autoDiscardable: false });
  } catch (_) {}
}

async function ensureScriptsInjected(tabId) {
  await protectTabFromFreeze(tabId);
  try {
    await chrome.scripting.executeScript({
      target: { tabId },
      files: ["page-driver.js"],
      world: "MAIN"
    });
    await chrome.scripting.executeScript({
      target: { tabId },
      files: ["content-bridge.js"],
      world: "ISOLATED"
    });
  } catch (err) {
    await appendLog("warn", "Script injection skipped/failed on tab " + tabId + ": " + err.message);
  }
}

async function injectIntoExistingTabs() {
  const tabs = await chrome.tabs.query({ url: VERTEX_MATCH_PATTERN });
  for (const tab of tabs) {
    if (tab.id) {
      await protectTabFromFreeze(tab.id);
      await ensureScriptsInjected(tab.id);
    }
  }
}

async function buildReportedTabsSnapshot() {
  const tabs = await getEligibleVertexTabs();
  const { capturedRpc = false, activeModel = "" } = await chrome.storage.session.get([
    "capturedRpc",
    "activeModel"
  ]);
  return tabs.map((t) => {
    const meta = runtimeTabMeta.get(t.id) || {};
    return {
      tab_id: t.id,
      tab_url: t.url || "",
      tab_title: t.title || "",
      ready: Boolean(t.id),
      active_model: meta.activeModel || activeModel || "",
      captured_rpc: meta.capturedRpc !== undefined ? Boolean(meta.capturedRpc) : Boolean(capturedRpc)
    };
  });
}

async function reportTabStatusToBackend() {
  const settings = await getSettings();
  const tabsSnapshot = await buildReportedTabsSnapshot();
  const primary = tabsSnapshot[0] || null;
  const { preferredTabId } = await chrome.storage.session.get("preferredTabId");
  const capabilities = ["multi_tab", "usage_push"];
  if (settings.autoSpawnTabs !== false && !preferredTabId) {
    capabilities.push("spawn_tab");
  }

  await setSessionStatus({
    activeTabId: primary ? primary.tab_id : null,
    activeTabUrl: primary ? primary.tab_url : "",
    activeTabTitle: primary ? primary.tab_title : "",
    capturedRpc: tabsSnapshot.some((t) => t.captured_rpc),
    tabCount: tabsSnapshot.length,
    wsConnected: Boolean(socketRuntime.ws && socketRuntime.ws.readyState === WebSocket.OPEN)
  });

  sendWsJson({
    type: "tab_status",
    tab_id: primary ? primary.tab_id : null,
    tab_url: primary ? primary.tab_url : "",
    tab_title: primary ? primary.tab_title : "",
    ready: tabsSnapshot.length > 0,
    driver_mode: settings.driverMode,
    captured_rpc: tabsSnapshot.some((t) => t.captured_rpc),
    active_model: primary ? primary.active_model : "",
    capabilities,
    max_parallel_tabs: Math.max(1, Number(settings.maxParallelTabs) || 4),
    tabs: tabsSnapshot,
    user_agent: navigator.userAgent
  });
}

function sendWsJson(payload) {
  if (!socketRuntime.ws || socketRuntime.ws.readyState !== WebSocket.OPEN) {
    return false;
  }
  try {
    socketRuntime.ws.send(JSON.stringify(payload));
    return true;
  } catch (_) {
    return false;
  }
}

function clearHeartbeatInterval() {
  if (socketRuntime.heartbeatTimer !== null) {
    clearInterval(socketRuntime.heartbeatTimer);
    socketRuntime.heartbeatTimer = null;
  }
}

function startHeartbeatInterval() {
  clearHeartbeatInterval();
  // Send a WebSocket frame every 20s to keep MV3 service worker & WS alive (Chrome 116+)
  socketRuntime.heartbeatTimer = setInterval(() => {
    void (async () => {
      const settings = await getSettings();
      const tabsSnapshot = await buildReportedTabsSnapshot();
      const primary = tabsSnapshot[0] || null;
      const { preferredTabId } = await chrome.storage.session.get("preferredTabId");
      const capabilities = ["multi_tab", "usage_push"];
      if (settings.autoSpawnTabs !== false && !preferredTabId) {
        capabilities.push("spawn_tab");
      }
      sendWsJson({
        type: "heartbeat",
        tab_id: primary ? primary.tab_id : null,
        tab_url: primary ? primary.tab_url : "",
        tab_title: primary ? primary.tab_title : "",
        ready: tabsSnapshot.length > 0,
        driver_mode: settings.driverMode,
        captured_rpc: tabsSnapshot.some((t) => t.captured_rpc),
        capabilities,
        max_parallel_tabs: Math.max(1, Number(settings.maxParallelTabs) || 4),
        tabs: tabsSnapshot
      });
    })();
  }, 20000);
}

function clearReconnectTimer() {
  if (socketRuntime.reconnectTimer !== null) {
    clearTimeout(socketRuntime.reconnectTimer);
    socketRuntime.reconnectTimer = null;
  }
}

function scheduleReconnectWithBackoff() {
  if (socketRuntime.reconnectTimer !== null) {
    return;
  }
  const attempt = socketRuntime.reconnectAttempts;
  socketRuntime.reconnectAttempts = Math.min(attempt + 1, 8);
  const baseDelay = Math.min(500 * Math.pow(2, attempt), 15000);
  const jitter = Math.floor(Math.random() * 250);
  const delayMs = baseDelay + jitter;

  socketRuntime.reconnectTimer = setTimeout(() => {
    socketRuntime.reconnectTimer = null;
    void ensureWebSocketConnected(false, true);
  }, delayMs);
}

async function disconnectWebSocket() {
  clearHeartbeatInterval();
  clearReconnectTimer();
  if (socketRuntime.ws) {
    const closingWs = socketRuntime.ws;
    socketRuntime.ws = null;
    try {
      closingWs.close(1000, "disabled_or_reconnecting");
    } catch (_) {}
  }
  socketRuntime.connecting = false;
  await setSessionStatus({ wsConnected: false });
}

async function ensureWebSocketConnected(forceReconnect = false, fromBackoffTimer = false) {
  const settings = await getSettings();
  if (!settings.enabled) {
    socketRuntime.reconnectAttempts = 0;
    await disconnectWebSocket();
    return;
  }

  if (forceReconnect) {
    socketRuntime.reconnectAttempts = 0;
    await disconnectWebSocket();
  }

  if (
    socketRuntime.connecting ||
    (socketRuntime.ws &&
      (socketRuntime.ws.readyState === WebSocket.OPEN ||
        socketRuntime.ws.readyState === WebSocket.CONNECTING))
  ) {
    if (socketRuntime.ws && socketRuntime.ws.readyState === WebSocket.OPEN) {
      await reportTabStatusToBackend();
    }
    return;
  }

  // Respect active exponential backoff timer unless explicitly forced or triggered by the timer itself
  if (!forceReconnect && !fromBackoffTimer && socketRuntime.reconnectTimer !== null) {
    return;
  }

  clearReconnectTimer();
  socketRuntime.connecting = true;
  const clientId = await getClientId();
  let targetUrl = settings.wsUrl.trim() || DEFAULT_SETTINGS.wsUrl;
  const separator = targetUrl.includes("?") ? "&" : "?";
  targetUrl = `${targetUrl}${separator}client_id=${encodeURIComponent(clientId)}`;

  try {
    const ws = new WebSocket(targetUrl);
    socketRuntime.ws = ws;

    ws.addEventListener("open", () => {
      void (async () => {
        if (socketRuntime.ws !== ws) return;
        socketRuntime.connecting = false;
        socketRuntime.reconnectAttempts = 0;
        clearReconnectTimer();
        await appendLog("info", "Connected to local FastAPI gateway: " + settings.wsUrl);
        await setSessionStatus({ wsConnected: true, lastError: "" });
        startHeartbeatInterval();
        await reportTabStatusToBackend();
      })();
    });

    ws.addEventListener("message", (event) => {
      void (async () => {
        try {
          const msg = JSON.parse(String(event.data || "{}"));
          await handleBackendCommand(msg);
        } catch (err) {
          await appendLog("error", "Invalid WS payload: " + err.message);
        }
      })();
    });

    ws.addEventListener("close", () => {
      void (async () => {
        clearHeartbeatInterval();
        if (socketRuntime.ws === ws) {
          socketRuntime.ws = null;
          socketRuntime.connecting = false;
          await setSessionStatus({ wsConnected: false });
          const latestSettings = await getSettings();
          if (latestSettings.enabled) {
            scheduleReconnectWithBackoff();
          }
        }
      })();
    });

    ws.addEventListener("error", () => {
      void (async () => {
        if (socketRuntime.ws !== ws) return;
        socketRuntime.connecting = false;
        await setSessionStatus({
          wsConnected: false,
          lastError: "无法连接本地后端 (" + settings.wsUrl + ")，正在按指数退避自动重连；请确认 start.bat / server.py 正在运行。"
        });
      })();
    });
  } catch (err) {
    socketRuntime.connecting = false;
    socketRuntime.ws = null;
    await setSessionStatus({
      wsConnected: false,
      lastError: err.message || "WebSocket 初始化失败"
    });
    scheduleReconnectWithBackoff();
  }
}

function computeCleanVertexHomeUrl(rawUrl) {
  if (!rawUrl) return "";
  try {
    const u = new URL(rawUrl);
    const nextPath = u.pathname
      .replace(/\/r\/session\/[^/?#]+.*$/i, "")
      .replace(/\/session\/[^/?#]+.*$/i, "")
      .replace(/\/c\/[^/?#]+.*$/i, "");
    if (nextPath && nextPath !== u.pathname) {
      u.pathname = nextPath;
      return u.toString();
    }
  } catch (_) {}
  return "";
}

function waitForTabComplete(tabId, timeoutMs = 12000) {
  return new Promise((resolve) => {
    let settled = false;
    const finish = () => {
      if (settled) return;
      settled = true;
      chrome.tabs.onUpdated.removeListener(listener);
      clearTimeout(timer);
      resolve();
    };
    const listener = (updatedTabId, changeInfo) => {
      if (updatedTabId === tabId && changeInfo.status === "complete") {
        finish();
      }
    };
    const timer = setTimeout(finish, timeoutMs);
    chrome.tabs.onUpdated.addListener(listener);
  });
}

async function prepareTabForBackgroundExecution(tab) {
  let freshTab = tab;
  try {
    freshTab = await chrome.tabs.get(tab.id);
  } catch (_) {}

  await protectTabFromFreeze(freshTab.id);

  // If Chrome Memory Saver discarded/unloaded the background tab, reload it silently.
  // Note: Do NOT force `active: true` here so multiple tabs in the same Chrome window
  // can run concurrently in the background without stealing tab focus from each other!
  if (freshTab.discarded || freshTab.status === "unloaded") {
    await appendLog("info", `Waking discarded background tab #${freshTab.id}...`);
    const reloadWait = waitForTabComplete(freshTab.id, 15000);
    await chrome.tabs.reload(freshTab.id);
    await reloadWait;
    await new Promise((r) => setTimeout(r, 650));
    await ensureScriptsInjected(freshTab.id);
    injectedTabVersions.set(freshTab.id, "2.5");
    try {
      freshTab = await chrome.tabs.get(freshTab.id);
    } catch (_) {}
  }

  return freshTab;
}

async function spawnParallelWorkerTab(baseUrlHint = "") {
  const allVertexTabs = await chrome.tabs.query({ url: VERTEX_MATCH_PATTERN });
  const settings = await getSettings();
  const maxTabs = Math.max(1, Number(settings.maxParallelTabs) || 6);
  if (allVertexTabs.length >= maxTabs) {
    throw new Error(`已达到最大并行标签页上限 (${maxTabs})`);
  }

  let targetUrl = "";
  if (baseUrlHint && baseUrlHint.startsWith("https://vertexaisearch.cloud.google.com")) {
    targetUrl = computeCleanVertexHomeUrl(baseUrlHint) || baseUrlHint;
  }
  if (!targetUrl && allVertexTabs.length > 0) {
    const refUrl = allVertexTabs[0].url || "";
    targetUrl = computeCleanVertexHomeUrl(refUrl) || refUrl;
  }
  if (!targetUrl) {
    targetUrl = "https://vertexaisearch.cloud.google.com/";
  }

  const createOpts = { url: targetUrl, active: false };
  if (allVertexTabs.length > 0 && allVertexTabs[0].windowId !== undefined) {
    createOpts.windowId = allVertexTabs[0].windowId;
  }

  const created = await chrome.tabs.create(createOpts);
  if (!created || !created.id) {
    throw new Error("无法创建新的后台工作标签页");
  }

  await protectTabFromFreeze(created.id);
  await waitForTabComplete(created.id, 14000);
  await new Promise((r) => setTimeout(r, 600));
  await ensureScriptsInjected(created.id);
  injectedTabVersions.set(created.id, "2.5");

  let latest = created;
  try {
    latest = await chrome.tabs.get(created.id);
  } catch (_) {}

  await appendLog("info", `Spawned parallel worker tab #${latest.id}`);
  await reportTabStatusToBackend();
  return {
    tab_id: latest.id,
    tab_url: latest.url || targetUrl,
    tab_title: latest.title || "Gemini Enterprise",
    ready: true
  };
}

async function resetTabChatWithFallback(tab) {
  let pageHidden = !tab.active;
  try {
    const vis = await chrome.tabs.sendMessage(tab.id, { type: "GEB_GET_VISIBILITY" });
    if (vis && typeof vis.hidden === "boolean") {
      pageHidden = vis.hidden;
    }
  } catch (_) {}

  const preCleanUrl = computeCleanVertexHomeUrl(tab.url || "");

  // When page is hidden in a background tab and currently sits on an active `/r/session/...` URL,
  // navigating directly via `chrome.tabs.update` avoids restricted hidden-DOM transition animations!
  if (pageHidden && preCleanUrl && preCleanUrl !== tab.url) {
    await appendLog("info", "Background tab fast URL reset -> " + preCleanUrl);
    const loadPromise = waitForTabComplete(tab.id, 12000);
    await chrome.tabs.update(tab.id, { url: preCleanUrl });
    await loadPromise;
    await new Promise((r) => setTimeout(r, 650));
    await ensureScriptsInjected(tab.id);
    return true;
  }

  await ensureScriptsInjected(tab.id);
  let inPageResult = null;
  try {
    inPageResult = await chrome.tabs.sendMessage(tab.id, { type: "GEB_TRIGGER_NEW_CHAT" });
  } catch (_) {}

  if (inPageResult && inPageResult.cleared) {
    await appendLog(
      "info",
      `New chat reset succeeded in-page (${inPageResult.method || "button"})`
    );
    return true;
  }

  // Hard fallback: navigate to clean /u/<n>/home/cid/<cid> URL without /r/session/<id>
  const targetCleanUrl =
    (inPageResult && inPageResult.cleanUrl) || preCleanUrl;
  if (targetCleanUrl && targetCleanUrl !== tab.url) {
    await appendLog("info", "Navigating tab to clean New Chat URL: " + targetCleanUrl);
    const loadPromise = waitForTabComplete(tab.id, 12000);
    await chrome.tabs.update(tab.id, { url: targetCleanUrl });
    await loadPromise;
    await new Promise((r) => setTimeout(r, 700));
    await ensureScriptsInjected(tab.id);
    return true;
  }

  return Boolean(inPageResult && inPageResult.ok);
}

const LARGE_REPLY_THRESHOLD = 24576;
const TAB_IPC_CHUNK_SIZE = 24576;
const swPromptReassembly = new Map();
const swReplyReassembly = new Map();

function deriveCallbackUrlFromWs(wsUrl) {
  try {
    const u = new URL(wsUrl || DEFAULT_SETTINGS.wsUrl);
    const proto = u.protocol === "wss:" ? "https:" : "http:";
    return `${proto}//${u.host}/api/bridge/callback`;
  } catch (_) {
    return "http://127.0.0.1:8000/api/bridge/callback";
  }
}

async function deliverResultToBackend(eventData) {
  const replyStr = String(eventData.reply || "");
  if (eventData.type === "request_complete" && replyStr.length > LARGE_REPLY_THRESHOLD) {
    const settings = await getSettings();
    const clientId = await getClientId();
    const callbackUrl = eventData.callback_url || deriveCallbackUrlFromWs(settings.wsUrl);
    try {
      const resp = await fetch(callbackUrl, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          client_id: clientId,
          tab_id: eventData.tab_id || null,
          request_id: eventData.request_id,
          reply: replyStr,
          citations: eventData.citations || [],
          source: eventData.source || "http_callback",
          active_model: eventData.active_model || null
        })
      });
      if (resp.ok) {
        return;
      }
    } catch (_) {
      // Fallback to chunked WebSocket frames below if HTTP callback fails
    }

    if (!socketRuntime.ws || socketRuntime.ws.readyState !== WebSocket.OPEN) {
      await ensureWebSocketConnected(false);
    }
    const chunkSize = 24576;
    const totalChunks = Math.ceil(replyStr.length / chunkSize);
    for (let i = 0; i < totalChunks; i += 1) {
      sendWsJson({
        type: "reply_chunk",
        request_id: eventData.request_id,
        chunk_index: i,
        total_chunks: totalChunks,
        data: replyStr.slice(i * chunkSize, (i + 1) * chunkSize)
      });
    }
    sendWsJson({
      ...eventData,
      reply: "",
      chunked: true,
      total_chunks: totalChunks
    });
    return;
  }

  if (!socketRuntime.ws || socketRuntime.ws.readyState !== WebSocket.OPEN) {
    await ensureWebSocketConnected(false);
  }
  sendWsJson(eventData);
}

async function handleBackendCommand(msg) {
  const type = msg.type || "";
  if (msg && msg.usage_snapshot && typeof msg.usage_snapshot === "object") {
    try {
      await chrome.storage.session.set({ usageSnapshot: msg.usage_snapshot });
    } catch (_) {}
  }
  if (type === "welcome" || type === "pong" || type === "ack" || type === "usage_update") {
    return;
  }
  if (type === "ping") {
    await reportTabStatusToBackend();
    return;
  }

  if (type === "spawn_tab") {
    const spawnId = msg.spawn_id || "";
    try {
      const spawned = await spawnParallelWorkerTab(msg.base_url || "");
      sendWsJson({
        type: "spawn_tab_result",
        spawn_id: spawnId,
        ok: true,
        tab: spawned
      });
    } catch (err) {
      await appendLog("warn", `Auto-spawn tab skipped: ${err.message}`);
      sendWsJson({
        type: "spawn_tab_result",
        spawn_id: spawnId,
        ok: false,
        error: err.message
      });
    }
    return;
  }

  if (type === "execute_prompt_chunk") {
    // Buffer chunks safely inside Service Worker FIRST so any subsequent tab wake/reload/new_chat
    // navigation during `execute_prompt` NEVER wipes buffered prompt chunks!
    let entry = swPromptReassembly.get(msg.request_id);
    if (!entry) {
      entry = { parts: [], total: msg.total_chunks || 0 };
      swPromptReassembly.set(msg.request_id, entry);
    }
    entry.parts[msg.chunk_index] = msg.data || "";
    if (msg.total_chunks) entry.total = msg.total_chunks;
    return;
  }

  if (type === "execute_prompt") {
    const requestId = msg.request_id;
    let assembledPrompt = String(msg.prompt || "");
    if (msg.chunked) {
      const entry = swPromptReassembly.get(requestId);
      swPromptReassembly.delete(requestId);
      const total = msg.total_chunks || (entry ? entry.total : 0);
      assembledPrompt = entry
        ? Array.from({ length: total }, (_, idx) => entry.parts[idx] || "").join("")
        : "";
    }

    const rawTab = await findTargetVertexTab(msg.tab_id);
    if (!rawTab || !rawTab.id) {
      sendWsJson({
        type: "request_error",
        request_id: requestId,
        error: "未找到已打开的 https://vertexaisearch.cloud.google.com 标签页。"
      });
      return;
    }

    sendWsJson({ type: "request_ack", request_id: requestId, tab_id: rawTab.id });

    const tab = await prepareTabForBackgroundExecution(rawTab);

    await appendLog(
      "info",
      `Dispatching request ${String(requestId).slice(0, 8)} to tab #${tab.id}` +
        ` [len=${assembledPrompt.length}]` +
        (tab.active ? "" : " [bg]") +
        (msg.target_model ? ` [model=${msg.target_model}]` : "") +
        (msg.reset_chat ? " [new_chat]" : "")
    );

    // Ensure hot-swapped latest page-driver.js & content-bridge.js are active on this tab (race-free per-tab map)
    if (injectedTabVersions.get(tab.id) !== "2.5") {
      await ensureScriptsInjected(tab.id);
      injectedTabVersions.set(tab.id, "2.5");
    }

    let remainingResetChat = Boolean(msg.reset_chat);
    if (remainingResetChat) {
      const resetDone = await resetTabChatWithFallback(tab);
      if (resetDone) {
        remainingResetChat = false;
      }
    }

    const settings = await getSettings();
    const callbackUrl = msg.callback_url || deriveCallbackUrlFromWs(settings.wsUrl);

    const dispatchToReadyTab = async () => {
      if (assembledPrompt.length > TAB_IPC_CHUNK_SIZE) {
        const totalChunks = Math.ceil(assembledPrompt.length / TAB_IPC_CHUNK_SIZE);
        for (let i = 0; i < totalChunks; i += 1) {
          await chrome.tabs.sendMessage(tab.id, {
            type: "GEB_PROMPT_CHUNK",
            request_id: requestId,
            chunk_index: i,
            total_chunks: totalChunks,
            data: assembledPrompt.slice(i * TAB_IPC_CHUNK_SIZE, (i + 1) * TAB_IPC_CHUNK_SIZE)
          });
        }
        await chrome.tabs.sendMessage(tab.id, {
          type: "GEB_EXECUTE_PROMPT",
          request_id: requestId,
          prompt: "",
          chunked: true,
          total_chunks: totalChunks,
          callback_url: callbackUrl,
          target_model: msg.target_model || null,
          reset_chat: remainingResetChat,
          timeout_ms: msg.timeout_ms || 600000,
          driver_config: {
            driverMode: settings.driverMode,
            customModelSelector: settings.customModelSelector,
            customInputSelector: settings.customInputSelector,
            customSendSelector: settings.customSendSelector,
            customReplySelector: settings.customReplySelector
          }
        });
      } else {
        await chrome.tabs.sendMessage(tab.id, {
          type: "GEB_EXECUTE_PROMPT",
          request_id: requestId,
          prompt: assembledPrompt,
          chunked: false,
          total_chunks: 0,
          callback_url: callbackUrl,
          target_model: msg.target_model || null,
          reset_chat: remainingResetChat,
          timeout_ms: msg.timeout_ms || 600000,
          driver_config: {
            driverMode: settings.driverMode,
            customModelSelector: settings.customModelSelector,
            customInputSelector: settings.customInputSelector,
            customSendSelector: settings.customSendSelector,
            customReplySelector: settings.customReplySelector
          }
        });
      }
    };

    try {
      await dispatchToReadyTab();
    } catch (_) {
      await ensureScriptsInjected(tab.id);
      try {
        await dispatchToReadyTab();
      } catch (retryErr) {
        sendWsJson({
          type: "request_error",
          request_id: requestId,
          error: "无法向 Gemini Enterprise 标签页注入桥接脚本，请刷新该网页后重试: " + retryErr.message
        });
      }
    }
    return;
  }

  if (type === "cancel_request") {
    const tab = await findTargetVertexTab(msg.tab_id);
    if (tab && tab.id) {
      try {
        await chrome.tabs.sendMessage(tab.id, {
          type: "GEB_CANCEL_REQUEST",
          request_id: msg.request_id
        });
      } catch (_) {}
    }
  }
}

async function setupAlarms() {
  await chrome.alarms.create(ALARM_NAME, { periodInMinutes: 0.4 });
}

chrome.runtime.onInstalled.addListener(() => {
  void (async () => {
    const existing = await chrome.storage.local.get(Object.keys(DEFAULT_SETTINGS));
    const merged = { ...DEFAULT_SETTINGS, ...existing };
    await chrome.storage.local.set(merged);
    await setupAlarms();
    await injectIntoExistingTabs();
    await ensureWebSocketConnected(false);
  })();
});

chrome.runtime.onStartup.addListener(() => {
  void (async () => {
    await setupAlarms();
    await injectIntoExistingTabs();
    await ensureWebSocketConnected(false);
  })();
});

chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === ALARM_NAME) {
    void ensureWebSocketConnected(false);
  }
});

chrome.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
  if (changeInfo.status === "complete" && tab.url && tab.url.startsWith("https://vertexaisearch.cloud.google.com/")) {
    void (async () => {
      await protectTabFromFreeze(tabId);
      await ensureWebSocketConnected(false);
      await reportTabStatusToBackend();
    })();
  }
});

chrome.tabs.onRemoved.addListener((tabId) => {
  runtimeTabMeta.delete(tabId);
  injectedTabVersions.delete(tabId);
  void reportTabStatusToBackend();
});

chrome.tabs.onActivated.addListener(() => {
  void reportTabStatusToBackend();
});

// Keep-alive port from hidden/background Gemini Enterprise content scripts
chrome.runtime.onConnect.addListener((port) => {
  if (port.name !== "geb-keepalive") return;
  port.onMessage.addListener((msg) => {
    if (msg && msg.type === "keepalive_ping") {
      try {
        port.postMessage({ type: "keepalive_pong", ts: Date.now() });
      } catch (_) {}
      void ensureWebSocketConnected(false);
    }
  });
});

let tabActivationMutex = Promise.resolve();

function withTabActivationLock(fn) {
  const next = tabActivationMutex.then(fn, fn);
  tabActivationMutex = next.catch(() => {});
  return next;
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  const msgType = message && message.type ? message.type : "";

  // Unthrottled timer service for hidden/background tabs (exempt from Blink Intensive Wake Up Throttling)
  if (msgType === "GEB_SW_SLEEP") {
    const delay = Math.max(0, Math.min(Number(message.ms) || 0, 10000));
    setTimeout(() => {
      sendResponse({ ok: true });
    }, delay);
    return true;
  }

  // Mutex-guarded brief tab activation within the Chrome window (never focuses the OS window!)
  // so background/parallel tabs whose ProseMirror/Lit submit ignored synthetic events can self-heal.
  if (msgType === "GEB_BRIEFLY_ACTIVATE_TAB") {
    const targetTabId = (sender && sender.tab && sender.tab.id) || message.tab_id;
    const holdMs = Math.max(120, Math.min(Number(message.hold_ms) || 240, 1500));
    const restoreDelayMs = Math.max(300, Math.min(Number(message.restore_delay_ms) || 650, 2500));
    if (!targetTabId) {
      sendResponse({ ok: false, error: "missing_tab_id" });
      return false;
    }
    void withTabActivationLock(async () => {
      let prevActiveId = null;
      try {
        const targetTab = await chrome.tabs.get(targetTabId);
        if (targetTab && targetTab.windowId !== undefined) {
          const activeInWin = await chrome.tabs.query({ active: true, windowId: targetTab.windowId });
          if (activeInWin.length > 0 && activeInWin[0].id !== targetTabId) {
            prevActiveId = activeInWin[0].id;
          }
        }
        await chrome.tabs.update(targetTabId, { active: true });
        await new Promise((r) => setTimeout(r, holdMs));
        sendResponse({ ok: true, activated_tab_id: targetTabId, previous_tab_id: prevActiveId });
        if (message.restore_previous !== false && prevActiveId) {
          await new Promise((r) => setTimeout(r, restoreDelayMs));
          try {
            await chrome.tabs.update(prevActiveId, { active: true });
          } catch (_) {}
        }
      } catch (err) {
        sendResponse({ ok: false, error: err && err.message ? err.message : String(err) });
      }
    });
    return true;
  }

  (async () => {
    if (msgType === "GEB_DRIVER_EVENT") {
      const senderTabId = sender && sender.tab && sender.tab.id ? sender.tab.id : null;
      const eventData = { ...(message.payload || {}) };
      if (senderTabId !== null && eventData.tab_id === undefined) {
        eventData.tab_id = senderTabId;
      }

      if (senderTabId !== null) {
        const prevMeta = runtimeTabMeta.get(senderTabId) || {};
        const nextMeta = { ...prevMeta };
        if (typeof eventData.captured_rpc === "boolean") {
          nextMeta.capturedRpc = eventData.captured_rpc;
          await chrome.storage.session.set({ capturedRpc: eventData.captured_rpc });
        }
        if (typeof eventData.active_model === "string" && eventData.active_model) {
          nextMeta.activeModel = eventData.active_model;
          await chrome.storage.session.set({ activeModel: eventData.active_model });
        }
        runtimeTabMeta.set(senderTabId, nextMeta);
      }

      if (eventData.type === "driver_ready" || eventData.type === "driver_heartbeat" || eventData.type === "tab_visibility") {
        if (senderTabId) {
          await protectTabFromFreeze(senderTabId);
        }
        await ensureWebSocketConnected(false);
        await reportTabStatusToBackend();
      } else if (eventData.type === "log") {
        const tabPrefix = senderTabId ? `[Tab#${senderTabId}] ` : "";
        await appendLog(eventData.level || "info", tabPrefix + (eventData.message || ""));
        sendWsJson({ ...eventData, message: tabPrefix + (eventData.message || "") });
      } else if (eventData.type === "sw_reply_chunk") {
        let entry = swReplyReassembly.get(eventData.request_id);
        if (!entry) {
          entry = { parts: [], total: eventData.total_chunks || 0 };
          swReplyReassembly.set(eventData.request_id, entry);
        }
        entry.parts[eventData.chunk_index] = eventData.data || "";
        if (eventData.total_chunks) entry.total = eventData.total_chunks;
      } else {
        // stream_chunk | request_complete | request_error
        if (eventData.type === "request_complete" && eventData.chunked) {
          const entry = swReplyReassembly.get(eventData.request_id);
          swReplyReassembly.delete(eventData.request_id);
          const total = eventData.total_chunks || (entry ? entry.total : 0);
          const merged = entry
            ? Array.from({ length: total }, (_, idx) => entry.parts[idx] || "").join("")
            : "";
          await deliverResultToBackend({ ...eventData, reply: merged, chunked: false });
        } else {
          await deliverResultToBackend(eventData);
        }
      }
      sendResponse({ ok: true });
      return;
    }

    if (msgType === "GEB_POPUP_GET_STATE") {
      await ensureWebSocketConnected(false);
      const settings = await getSettings();
      const tab = await findBestVertexTab();
      const session = await chrome.storage.session.get([
        "wsConnected",
        "lastError",
        "capturedRpc",
        "activeModel",
        "logs",
        "preferredTabId",
        "usageSnapshot"
      ]);
      const allTabs = await chrome.tabs.query({ url: VERTEX_MATCH_PATTERN });
      sendResponse({
        ok: true,
        settings,
        wsConnected: Boolean(socketRuntime.ws && socketRuntime.ws.readyState === WebSocket.OPEN),
        lastError: session.lastError || "",
        capturedRpc: Boolean(session.capturedRpc),
        activeModel: session.activeModel || "",
        preferredTabId: session.preferredTabId || null,
        usageSnapshot: session.usageSnapshot || null,
        activeTab: tab
          ? { id: tab.id, url: tab.url || "", title: tab.title || "" }
          : null,
        vertexTabs: allTabs.map((t) => ({
          id: t.id,
          url: t.url || "",
          title: t.title || "",
          active: Boolean(t.active)
        })),
        logs: session.logs || []
      });
      return;
    }

    if (msgType === "GEB_POPUP_SAVE_SETTINGS") {
      const patch = message.settings || {};
      const current = await getSettings();
      const updated = { ...current, ...patch };
      await chrome.storage.local.set(updated);
      if (message.preferredTabId !== undefined) {
        await chrome.storage.session.set({ preferredTabId: message.preferredTabId });
      }
      const wsChanged = patch.wsUrl !== undefined && patch.wsUrl !== current.wsUrl;
      const enabledChanged = patch.enabled !== undefined && patch.enabled !== current.enabled;
      await ensureWebSocketConnected(wsChanged || enabledChanged);
      await reportTabStatusToBackend();
      sendResponse({ ok: true });
      return;
    }

    if (msgType === "GEB_POPUP_RECONNECT") {
      await ensureWebSocketConnected(true);
      sendResponse({ ok: true });
      return;
    }

    if (msgType === "GEB_POPUP_DIAGNOSE_TAB") {
      const tab = await findBestVertexTab();
      if (!tab || !tab.id) {
        sendResponse({ ok: false, error: "未打开 vertexaisearch.cloud.google.com 页面" });
        return;
      }
      await ensureScriptsInjected(tab.id);
      try {
        const diag = await chrome.tabs.sendMessage(tab.id, { type: "GEB_DIAGNOSE_PAGE" });
        sendResponse({ ok: true, diagnosis: diag });
      } catch (err) {
        sendResponse({ ok: false, error: err.message });
      }
      return;
    }

    if (msgType === "GEB_POPUP_NEW_CHAT") {
      const tab = await findBestVertexTab();
      if (!tab || !tab.id) {
        sendResponse({ ok: false, error: "未打开 vertexaisearch.cloud.google.com 页面" });
        return;
      }
      await ensureScriptsInjected(tab.id);
      try {
        const res = await chrome.tabs.sendMessage(tab.id, { type: "GEB_TRIGGER_NEW_CHAT" });
        sendResponse({ ok: true, result: res });
      } catch (err) {
        sendResponse({ ok: false, error: err.message });
      }
      return;
    }

    if (msgType === "GEB_POPUP_SPAWN_TAB") {
      try {
        const spawned = await spawnParallelWorkerTab("");
        sendResponse({ ok: true, tab: spawned });
      } catch (err) {
        sendResponse({ ok: false, error: err.message });
      }
      return;
    }

    if (msgType === "GEB_POPUP_CLEAR_LOGS") {
      await chrome.storage.session.set({ logs: [] });
      sendResponse({ ok: true });
      return;
    }

    sendResponse({ ok: true, senderTab: sender && sender.tab ? sender.tab.id : null });
  })();
  return true;
});

// Kick off connection check whenever the Service Worker wakes up
void (async () => {
  await setupAlarms();
  await ensureWebSocketConnected(false);
})();
