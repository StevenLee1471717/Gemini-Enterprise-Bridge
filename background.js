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
const GITHUB_UPDATE_ALARM = "geb-github-update-check";
const VERTEX_MATCH_PATTERN = "https://vertexaisearch.cloud.google.com/*";
const DEFAULT_GITHUB_REPO = "StevenLee1471717/Gemini-Enterprise-Bridge";

// Live non-serializable WebSocket handle & per-tab fast caches; durable settings & update state live in chrome.storage
const socketRuntime = {
  ws: null,
  connecting: false,
  heartbeatTimer: null,
  reconnectTimer: null,
  reconnectAttempts: 0
};
const runtimeTabMeta = new Map(); // tabId -> { activeModel, capturedRpc }
const injectedTabVersions = new Map(); // tabId -> driverVersion
const activeInFlightRequests = new Map(); // requestId -> expiresAtMs

function getExtensionVersion() {
  try {
    return (chrome.runtime.getManifest() && chrome.runtime.getManifest().version) || "1.2.0";
  } catch (_) {
    return "1.2.0";
  }
}

function normalizeVersion(raw) {
  const text = String(raw || "").trim();
  if (text.length > 1 && (text[0] === "v" || text[0] === "V") && /^\d/.test(text.slice(1))) {
    return text.slice(1);
  }
  return text;
}

function parseSemver(raw) {
  const text = normalizeVersion(raw);
  if (!text) return [0, 0, 0, 0, ""];
  const m = text.match(/^(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:-([0-9A-Za-z.-]+))?/);
  if (!m) return [0, 0, 0, 1, ""];
  const major = Number(m[1] || 0);
  const minor = Number(m[2] || 0);
  const patch = Number(m[3] || 0);
  const pre = m[4] || "";
  return [major, minor, patch, pre ? 0 : 1, pre];
}

function compareSemver(v1, v2) {
  const a = parseSemver(v1);
  const b = parseSemver(v2);
  for (let i = 0; i < 4; i += 1) {
    if (a[i] !== b[i]) return a[i] > b[i] ? 1 : -1;
  }
  if (a[4] === b[4]) return 0;
  return a[4] > b[4] ? 1 : -1;
}

function hasActiveInFlightJobs() {
  const now = Date.now();
  for (const [reqId, exp] of activeInFlightRequests.entries()) {
    if (exp <= now) {
      activeInFlightRequests.delete(reqId);
    }
  }
  return activeInFlightRequests.size > 0;
}

async function getUpdateState() {
  const { updateState = {} } = await chrome.storage.local.get("updateState");
  return {
    githubRepo: DEFAULT_GITHUB_REPO,
    extensionVersion: getExtensionVersion(),
    backendVersion: "",
    latestGitHubVersion: "",
    releaseUrl: `https://github.com/${DEFAULT_GITHUB_REPO}/releases`,
    backendUpdateRequired: false,
    extensionOutdated: false,
    pendingExtensionReloadVersion: "",
    justAutoUpdatedTo: "",
    attemptedReloadTargetVersion: "",
    attemptedReloadFromVersion: "",
    lastCheckedAt: 0,
    lastCheckError: "",
    ...updateState,
    currentExtensionVersion: getExtensionVersion()
  };
}

async function patchUpdateState(patch) {
  const prev = await getUpdateState();
  const merged = {
    ...prev,
    ...patch,
    extensionVersion: getExtensionVersion()
  };
  delete merged.currentExtensionVersion;
  await chrome.storage.local.set({ updateState: merged });
  await setSessionStatus({
    backendUpdateRequired: Boolean(merged.backendUpdateRequired),
    extensionOutdated: Boolean(merged.extensionOutdated)
  });
  return merged;
}

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
      "tabCount",
      "backendUpdateRequired",
      "extensionOutdated"
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
  if (status.wsConnected && status.backendUpdateRequired) {
    await chrome.action.setBadgeText({ text: "UPD" });
    await chrome.action.setBadgeBackgroundColor({ color: "#D97706" });
  } else if (status.wsConnected && status.activeTabId) {
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
  const capabilities = ["multi_tab", "usage_push", "auto_update"];
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
    extension_version: getExtensionVersion(),
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
      const capabilities = ["multi_tab", "usage_push", "auto_update"];
      if (settings.autoSpawnTabs !== false && !preferredTabId) {
        capabilities.push("spawn_tab");
      }
      sendWsJson({
        type: "heartbeat",
        extension_version: getExtensionVersion(),
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

  const rawWsUrl = settings.wsUrl.trim() || DEFAULT_SETTINGS.wsUrl;
  const httpBase = deriveHttpBaseFromWs(rawWsUrl);

  // Preflight HTTP /health before opening WebSocket so Chromium never logs
  // unsuppressible net::ERR_CONNECTION_REFUSED or 403 handshake errors in chrome://extensions.
  try {
    const healthRes = await fetchJsonWithTimeout(
      `${httpBase}/health`,
      { method: "GET", cache: "no-store" },
      1800
    );
    const isBridgeHealthy =
      healthRes.ok &&
      healthRes.data &&
      healthRes.data.status === "ok" &&
      typeof healthRes.data.browser_connected === "boolean";
    if (!isBridgeHealthy) {
      socketRuntime.connecting = false;
      await setSessionStatus({
        wsConnected: false,
        lastError: `本地端口响应异常 (${httpBase}/health HTTP ${healthRes.status})，请确认运行的是最新 Gemini Enterprise Bridge 后端。`
      });
      scheduleReconnectWithBackoff();
      return;
    }
  } catch (_) {
    socketRuntime.connecting = false;
    await setSessionStatus({
      wsConnected: false,
      lastError: `等待本地后端启动 (${httpBase})... 请确认 start.bat / server.py 正在运行。`
    });
    scheduleReconnectWithBackoff();
    return;
  }

  const latestSettingsAfterProbe = await getSettings();
  if (!socketRuntime.connecting || !latestSettingsAfterProbe.enabled) {
    socketRuntime.connecting = false;
    return;
  }

  const clientId = await getClientId();
  const separator = rawWsUrl.includes("?") ? "&" : "?";
  const targetUrl = `${rawWsUrl}${separator}client_id=${encodeURIComponent(clientId)}`;

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
        activeInFlightRequests.clear();
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
    injectedTabVersions.set(freshTab.id, "2.6.0");
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
  injectedTabVersions.set(created.id, "2.6.0");

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
    try {
      await chrome.tabs.sendMessage(tab.id, { type: "GEB_PREPARE_UNLOAD" });
    } catch (_) {}
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

function deriveHttpBaseFromWs(wsUrl) {
  try {
    const u = new URL(wsUrl || DEFAULT_SETTINGS.wsUrl);
    const proto = u.protocol === "wss:" ? "https:" : "http:";
    return `${proto}//${u.host}`;
  } catch (_) {
    return "http://127.0.0.1:8000";
  }
}

function deriveCallbackUrlFromWs(wsUrl) {
  return `${deriveHttpBaseFromWs(wsUrl)}/api/bridge/callback`;
}

function uint8ArrayToBase64(bytes) {
  const chunkSize = 0x8000;
  const parts = [];
  for (let i = 0; i < bytes.length; i += chunkSize) {
    const sub = bytes.subarray(i, i + chunkSize);
    parts.push(String.fromCharCode.apply(null, sub));
  }
  return btoa(parts.join(""));
}

function resolveAttachmentUrl(att, wsUrl) {
  const rawUrl = String((att && att.url) || "").trim();
  if (/^https?:\/\//i.test(rawUrl)) {
    return rawUrl;
  }
  const relPath = String((att && (att.path || att.url)) || "").trim();
  if (!relPath) {
    return "";
  }
  const base = deriveHttpBaseFromWs(wsUrl).replace(/\/+$/, "");
  return relPath.startsWith("/") ? `${base}${relPath}` : `${base}/${relPath}`;
}

async function fetchAttachmentAsBase64(att, wsUrl, timeoutMs = 60000) {
  if (att && typeof att.data_b64 === "string" && att.data_b64.length > 0) {
    return att.data_b64;
  }
  const targetUrl = resolveAttachmentUrl(att, wsUrl);
  if (!targetUrl) {
    throw new Error(`附件 ${att && att.filename ? att.filename : ""} 缺少下载地址`);
  }
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    const resp = await fetch(targetUrl, { method: "GET", signal: ctrl.signal });
    if (!resp.ok) {
      throw new Error(`HTTP ${resp.status}`);
    }
    const buf = await resp.arrayBuffer();
    return uint8ArrayToBase64(new Uint8Array(buf));
  } finally {
    clearTimeout(timer);
  }
}

async function fetchJsonWithTimeout(url, options = {}, timeoutMs = 8000) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    const resp = await fetch(url, { ...options, signal: ctrl.signal });
    const data = await resp.json();
    return { ok: resp.ok, status: resp.status, data };
  } finally {
    clearTimeout(timer);
  }
}

async function scheduleOrTriggerExtensionReload(targetVersion, reason = "auto_update", forceManual = false) {
  const currentExtVer = getExtensionVersion();
  const normalizedTarget = normalizeVersion(targetVersion);
  const state = await getUpdateState();

  // Prevent infinite reload loop if user loaded unpacked extension from a non-default directory
  if (
    !forceManual &&
    normalizedTarget &&
    state.attemptedReloadFromVersion === currentExtVer &&
    state.attemptedReloadTargetVersion === normalizedTarget
  ) {
    await appendLog(
      "warn",
      `已同步插件目录至 v${normalizedTarget}，但当前运行实例仍为 v${currentExtVer}；请在扩展弹窗点击重载或确认加载目录为 extension/`
    );
    await patchUpdateState({
      extensionOutdated: true,
      pendingExtensionReloadVersion: normalizedTarget
    });
    return { reloaded: false, deferred: false, manualRequired: true };
  }

  if (!forceManual && hasActiveInFlightJobs()) {
    await appendLog(
      "info",
      `检测到插件新版本 v${normalizedTarget || "latest"} (${reason})，等待当前对话生成完成后自动无感热重载...`
    );
    await patchUpdateState({
      extensionOutdated: true,
      pendingExtensionReloadVersion: normalizedTarget || "latest"
    });
    return { reloaded: false, deferred: true };
  }

  await patchUpdateState({
    justAutoUpdatedTo: normalizedTarget || currentExtVer,
    pendingExtensionReloadVersion: "",
    extensionOutdated: false,
    attemptedReloadFromVersion: currentExtVer,
    attemptedReloadTargetVersion: normalizedTarget || currentExtVer
  });
  await appendLog(
    "info",
    `正在自动热重载浏览器扩展至 v${normalizedTarget || currentExtVer} (${reason})...`
  );
  setTimeout(() => {
    chrome.runtime.reload();
  }, 180);
  return { reloaded: true, deferred: false };
}

async function maybeReloadPendingExtensionUpdate() {
  if (hasActiveInFlightJobs()) return;
  const state = await getUpdateState();
  if (state.pendingExtensionReloadVersion) {
    await scheduleOrTriggerExtensionReload(state.pendingExtensionReloadVersion, "idle_deferred_reload", false);
  }
}

async function syncExtensionDiskAndReload(targetVersionHint = "", forceManual = false) {
  const settings = await getSettings();
  const httpBase = deriveHttpBaseFromWs(settings.wsUrl);
  try {
    const res = await fetchJsonWithTimeout(
      `${httpBase}/api/update/extension`,
      { method: "POST", headers: { "Content-Type": "application/json" } },
      45000
    );
    if (res.ok && res.data && res.data.ok) {
      const ver = normalizeVersion(res.data.updated_version || targetVersionHint);
      await appendLog("info", `已从 GitHub Release 下载并更新本地 extension/ 目录至 v${ver}`);
      const reloadInfo = await scheduleOrTriggerExtensionReload(ver, "github_extension_sync", forceManual);
      return { ok: true, updatedVersion: ver, ...reloadInfo };
    }
    const errMsg = (res.data && res.data.error) || `HTTP ${res.status}`;
    await appendLog("warn", `同步扩展更新包失败: ${errMsg}`);
    return { ok: false, error: errMsg };
  } catch (err) {
    await appendLog("warn", `无法请求本地后端同步扩展目录: ${err.message}`);
    return { ok: false, error: err.message };
  }
}

async function checkGitHubReleaseDirectly(repo = DEFAULT_GITHUB_REPO) {
  const cleanRepo = String(repo || DEFAULT_GITHUB_REPO).trim().replace(/^\/+|\/+$/g, "");
  const latestApiUrl = `https://api.github.com/repos/${cleanRepo}/releases/latest`;
  const manifestUrl = `https://github.com/${cleanRepo}/releases/latest/download/release-manifest.json`;
  let apiErrMessage = "";

  // 1. Prefer GitHub REST API first (native CORS support, zero cross-domain 302 redirects)
  try {
    const apiRes = await fetchJsonWithTimeout(
      latestApiUrl,
      {
        cache: "no-store",
        credentials: "omit",
        headers: { Accept: "application/vnd.github+json" }
      },
      7000
    );
    if (apiRes.ok && apiRes.data && apiRes.data.tag_name) {
      const ver = normalizeVersion(apiRes.data.tag_name);
      return {
        ok: true,
        version: ver,
        releaseUrl:
          apiRes.data.html_url || `https://github.com/${cleanRepo}/releases/tag/${apiRes.data.tag_name}`
      };
    }
  } catch (err) {
    apiErrMessage = err && err.message ? err.message : String(err || "");
  }

  // 2. Fallback to release-manifest.json as a strict CORS-simple GET (no custom headers -> zero OPTIONS preflight)
  try {
    const mfRes = await fetchJsonWithTimeout(
      manifestUrl,
      { cache: "no-store" },
      7000
    );
    if (mfRes.ok && mfRes.data && mfRes.data.version) {
      const ver = normalizeVersion(mfRes.data.version);
      return {
        ok: true,
        version: ver,
        releaseUrl:
          mfRes.data.release_url || `https://github.com/${cleanRepo}/releases/tag/v${ver}`
      };
    }
  } catch (err) {
    return {
      ok: false,
      version: "",
      error: (err && err.message) || apiErrMessage || "github_release_unreachable"
    };
  }

  return { ok: false, version: "", error: apiErrMessage || "no_github_release" };
}

async function checkUpdatesAndAutoSync(force = false) {
  const { checkingUpdateLock = 0 } = await chrome.storage.session.get("checkingUpdateLock");
  if (!force && checkingUpdateLock && Date.now() - checkingUpdateLock < 20000) {
    return await getUpdateState();
  }
  await chrome.storage.session.set({ checkingUpdateLock: Date.now() });

  try {
    const settings = await getSettings();
    const httpBase = deriveHttpBaseFromWs(settings.wsUrl);
    const prev = await getUpdateState();
    const extVer = getExtensionVersion();
    let backendVer = prev.backendVersion || "";
    let latestGitHubVer = prev.latestGitHubVersion || "";
    let releaseUrl = prev.releaseUrl || `https://github.com/${DEFAULT_GITHUB_REPO}/releases`;
    let githubRepo = prev.githubRepo || DEFAULT_GITHUB_REPO;
    let lastCheckError = "";

    // Path A: Query local backend /api/update/status (supports private repos via gh CLI / token)
    let backendReturnedRemoteVersion = false;
    try {
      const bRes = await fetchJsonWithTimeout(
        `${httpBase}/api/update/status?force=${force ? "true" : "false"}`,
        { method: "GET", cache: "no-store" },
        8500
      );
      if (bRes.ok && bRes.data) {
        if (bRes.data.repo) githubRepo = bRes.data.repo;
        if (bRes.data.current_backend_version) {
          backendVer = normalizeVersion(bRes.data.current_backend_version);
        }
        if (bRes.data.latest_github_version) {
          const bRemote = normalizeVersion(bRes.data.latest_github_version);
          if (bRemote) {
            backendReturnedRemoteVersion = true;
            if (!latestGitHubVer || compareSemver(bRemote, latestGitHubVer) >= 0) {
              latestGitHubVer = bRemote;
            }
          }
        }
        if (bRes.data.release_url) releaseUrl = bRes.data.release_url;
      }
    } catch (_) {}

    // Path B: Only poll GitHub Releases directly from MV3 Service Worker when local backend is offline or omitted remote version
    if (!backendReturnedRemoteVersion) {
      const direct = await checkGitHubReleaseDirectly(githubRepo);
      if (direct.ok && direct.version) {
        if (!latestGitHubVer || compareSemver(direct.version, latestGitHubVer) >= 0) {
          latestGitHubVer = direct.version;
          if (direct.releaseUrl) releaseUrl = direct.releaseUrl;
        }
      } else if (!latestGitHubVer && direct.error) {
        lastCheckError = direct.error;
      }
    }

    const extNewerThanBackend = Boolean(backendVer && compareSemver(extVer, backendVer) > 0);
    const githubNewerThanBackend = Boolean(
      backendVer && latestGitHubVer && compareSemver(latestGitHubVer, backendVer) > 0
    );
    const backendUpdateRequired = extNewerThanBackend || githubNewerThanBackend;

    const githubNewerThanExt = Boolean(latestGitHubVer && compareSemver(latestGitHubVer, extVer) > 0);
    const backendNewerThanExt = Boolean(backendVer && compareSemver(backendVer, extVer) > 0);
    const extensionOutdated = githubNewerThanExt || backendNewerThanExt;

    // Clear justAutoUpdatedTo notice once backend catches up
    let justAutoUpdatedTo = prev.justAutoUpdatedTo || "";
    if (justAutoUpdatedTo && backendVer && compareSemver(backendVer, justAutoUpdatedTo) >= 0 && !backendUpdateRequired) {
      justAutoUpdatedTo = "";
    }

    const updatedState = await patchUpdateState({
      githubRepo,
      backendVersion: backendVer,
      latestGitHubVersion: latestGitHubVer,
      releaseUrl,
      backendUpdateRequired,
      extensionOutdated,
      justAutoUpdatedTo,
      lastCheckedAt: Date.now(),
      lastCheckError
    });

    // Automatically sync & hot-reload unpacked extension when GitHub has a newer release
    if (githubNewerThanExt) {
      await syncExtensionDiskAndReload(latestGitHubVer, false);
    } else if (backendNewerThanExt) {
      await scheduleOrTriggerExtensionReload(backendVer, "backend_ahead", false);
    }

    return updatedState;
  } finally {
    await chrome.storage.session.set({ checkingUpdateLock: 0 });
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
  if (msg && msg.backend_version) {
    const bVer = normalizeVersion(msg.backend_version);
    const extVer = getExtensionVersion();
    const state = await getUpdateState();
    const remoteVer = state.latestGitHubVersion || "";
    const backendOutdated =
      Boolean(msg.backend_update_required) ||
      compareSemver(extVer, bVer) > 0 ||
      Boolean(remoteVer && compareSemver(remoteVer, bVer) > 0);
    const extOutdated =
      compareSemver(bVer, extVer) > 0 ||
      Boolean(remoteVer && compareSemver(remoteVer, extVer) > 0);
    let clearedNotice = state.justAutoUpdatedTo || "";
    if (clearedNotice && compareSemver(bVer, clearedNotice) >= 0 && !backendOutdated) {
      clearedNotice = "";
    }
    await patchUpdateState({
      backendVersion: bVer,
      backendUpdateRequired: backendOutdated,
      extensionOutdated: extOutdated,
      justAutoUpdatedTo: clearedNotice
    });
    if (compareSemver(extVer, bVer) > 0) {
      await appendLog(
        "warn",
        `插件已更新至 v${extVer}，检测到本地后端仍为 v${bVer} — 请在插件弹窗或控制台一键更新后端`
      );
    }
  }

  if (type === "extension_reload_available") {
    const targetVer = normalizeVersion(msg.version || "");
    if (!targetVer || compareSemver(targetVer, getExtensionVersion()) > 0) {
      await scheduleOrTriggerExtensionReload(targetVer || getExtensionVersion(), "backend_ws_push", false);
    }
    return;
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

    const reqTtlMs = Math.max(60000, Number(msg.timeout_ms) || 600000) + 15000;
    activeInFlightRequests.set(requestId, Date.now() + reqTtlMs);
    sendWsJson({ type: "request_ack", request_id: requestId, tab_id: rawTab.id });
    const rawAttachments = Array.isArray(msg.attachments) ? msg.attachments : [];
    const tab = await prepareTabForBackgroundExecution(rawTab);

    await appendLog(
      "info",
      `Dispatching request ${String(requestId).slice(0, 8)} to tab #${tab.id}` +
        ` [len=${assembledPrompt.length}]` +
        (rawAttachments.length > 0 ? ` [files=${rawAttachments.length}]` : "") +
        (tab.active ? "" : " [bg]") +
        (msg.target_model ? ` [model=${msg.target_model}]` : "") +
        (msg.reset_chat ? " [new_chat]" : "")
    );

    // Ensure hot-swapped latest page-driver.js & content-bridge.js are active on this tab (race-free per-tab map)
    if (injectedTabVersions.get(tab.id) !== "2.6.0") {
      await ensureScriptsInjected(tab.id);
      injectedTabVersions.set(tab.id, "2.6.0");
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

    const preparedAttachments = [];
    if (rawAttachments.length > 0) {
      try {
        for (let idx = 0; idx < rawAttachments.length; idx += 1) {
          const att = rawAttachments[idx] || {};
          const attId = String(att.id || `att-${idx}`);
          const dataB64 = await fetchAttachmentAsBase64(att, settings.wsUrl, 60000);
          preparedAttachments.push({
            id: attId,
            filename: String(att.filename || `attachment-${idx + 1}.bin`),
            mime_type: String(att.mime_type || "application/octet-stream"),
            kind: String(att.kind || "file"),
            size_bytes: Number(att.size_bytes || 0),
            data_b64: dataB64
          });
        }
      } catch (attErr) {
        activeInFlightRequests.delete(requestId);
        sendWsJson({
          type: "request_error",
          request_id: requestId,
          error:
            "下载本地暂存多模态附件失败: " +
            (attErr && attErr.message ? attErr.message : String(attErr))
        });
        await maybeReloadPendingExtensionUpdate();
        return;
      }
    }

    const dispatchToReadyTab = async () => {
      const attachmentMetaList = [];
      for (const att of preparedAttachments) {
        const b64 = att.data_b64 || "";
        const totalAttChunks = Math.max(1, Math.ceil(b64.length / TAB_IPC_CHUNK_SIZE));
        for (let c = 0; c < totalAttChunks; c += 1) {
          await chrome.tabs.sendMessage(tab.id, {
            type: "GEB_ATTACHMENT_CHUNK",
            request_id: requestId,
            attachment_id: att.id,
            chunk_index: c,
            total_chunks: totalAttChunks,
            data: b64.slice(c * TAB_IPC_CHUNK_SIZE, (c + 1) * TAB_IPC_CHUNK_SIZE)
          });
        }
        attachmentMetaList.push({
          id: att.id,
          filename: att.filename,
          mime_type: att.mime_type,
          kind: att.kind,
          size_bytes: att.size_bytes,
          total_chunks: totalAttChunks
        });
      }

      const driverConfigPayload = {
        driverMode: settings.driverMode,
        customModelSelector: settings.customModelSelector,
        customInputSelector: settings.customInputSelector,
        customSendSelector: settings.customSendSelector,
        customReplySelector: settings.customReplySelector
      };

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
          attachments: attachmentMetaList,
          callback_url: callbackUrl,
          target_model: msg.target_model || null,
          reset_chat: remainingResetChat,
          timeout_ms: msg.timeout_ms || 600000,
          driver_config: driverConfigPayload
        });
      } else {
        await chrome.tabs.sendMessage(tab.id, {
          type: "GEB_EXECUTE_PROMPT",
          request_id: requestId,
          prompt: assembledPrompt,
          chunked: false,
          total_chunks: 0,
          attachments: attachmentMetaList,
          callback_url: callbackUrl,
          target_model: msg.target_model || null,
          reset_chat: remainingResetChat,
          timeout_ms: msg.timeout_ms || 600000,
          driver_config: driverConfigPayload
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
        activeInFlightRequests.delete(requestId);
        sendWsJson({
          type: "request_error",
          request_id: requestId,
          error: "无法向 Gemini Enterprise 标签页注入桥接脚本，请刷新该网页后重试: " + retryErr.message
        });
        await maybeReloadPendingExtensionUpdate();
      }
    }
    return;
  }

  if (type === "cancel_request") {
    if (msg.request_id) {
      activeInFlightRequests.delete(msg.request_id);
    }
    const tab = await findTargetVertexTab(msg.tab_id);
    if (tab && tab.id) {
      try {
        await chrome.tabs.sendMessage(tab.id, {
          type: "GEB_CANCEL_REQUEST",
          request_id: msg.request_id
        });
      } catch (_) {}
    }
    await maybeReloadPendingExtensionUpdate();
  }
}

async function setupAlarms() {
  await chrome.alarms.create(ALARM_NAME, { periodInMinutes: 0.4 });
  await chrome.alarms.create(GITHUB_UPDATE_ALARM, { periodInMinutes: 30 });
}

chrome.runtime.onInstalled.addListener((details) => {
  void (async () => {
    const existing = await chrome.storage.local.get(Object.keys(DEFAULT_SETTINGS));
    const merged = { ...DEFAULT_SETTINGS, ...existing };
    await chrome.storage.local.set(merged);
    if (details && details.reason === "update") {
      const curVer = getExtensionVersion();
      await patchUpdateState({
        justAutoUpdatedTo: curVer,
        pendingExtensionReloadVersion: "",
        extensionOutdated: false,
        attemptedReloadFromVersion: "",
        attemptedReloadTargetVersion: ""
      });
      await appendLog("info", `浏览器扩展已升级并热重载至 v${curVer}`);
    }
    await setupAlarms();
    await injectIntoExistingTabs();
    await ensureWebSocketConnected(false);
    void checkUpdatesAndAutoSync(false);
  })();
});

chrome.runtime.onStartup.addListener(() => {
  void (async () => {
    await setupAlarms();
    await injectIntoExistingTabs();
    await ensureWebSocketConnected(false);
    void checkUpdatesAndAutoSync(false);
  })();
});

chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === ALARM_NAME) {
    void ensureWebSocketConnected(false);
  } else if (alarm.name === GITHUB_UPDATE_ALARM) {
    void checkUpdatesAndAutoSync(false);
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
  port.onDisconnect.addListener(() => {
    // Consume runtime.lastError when tab navigates or enters bfcache
    void chrome.runtime?.lastError;
  });
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

let tabActivationTail = Promise.resolve();
async function withTabActivationLock(fn) {
  const prev = tabActivationTail;
  let release;
  tabActivationTail = new Promise((resolve) => {
    release = resolve;
  });
  try {
    await prev;
  } catch (_) {}
  try {
    return await fn();
  } finally {
    release();
  }
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
        // stream_chunk | request_progress | request_complete | request_error
        if ((eventData.type === "stream_chunk" || eventData.type === "request_progress") && eventData.request_id) {
          if (activeInFlightRequests.has(eventData.request_id)) {
            activeInFlightRequests.set(eventData.request_id, Date.now() + 180000);
          }
        }
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
        if (eventData.type === "request_complete" || eventData.type === "request_error") {
          if (eventData.request_id) {
            activeInFlightRequests.delete(eventData.request_id);
          }
          await maybeReloadPendingExtensionUpdate();
        }
      }
      sendResponse({ ok: true });
      return;
    }

    if (msgType === "GEB_POPUP_GET_STATE") {
      await ensureWebSocketConnected(false);
      const settings = await getSettings();
      const updateState = await getUpdateState();
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
        extensionVersion: getExtensionVersion(),
        updateState,
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

    if (msgType === "GEB_POPUP_CHECK_UPDATE") {
      const uState = await checkUpdatesAndAutoSync(true);
      sendResponse({ ok: true, updateState: uState });
      return;
    }

    if (msgType === "GEB_POPUP_SYNC_EXTENSION") {
      const syncRes = await syncExtensionDiskAndReload(message.targetVersion || "", true);
      sendResponse(syncRes);
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
