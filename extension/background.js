// MV3 Background Service Worker for Gemini Enterprise Web-to-API Bridge
// Maintains persistent WebSocket to local FastAPI backend (outside webpage CSP)
// and routes commands to active vertexaisearch.cloud.google.com tabs.

const DEFAULT_SETTINGS = {
  wsUrl: "ws://127.0.0.1:8000/ws/browser",
  enabled: true,
  driverMode: "auto", // auto | dom | replay
  autoSpawnTabs: true,
  maxParallelTabs: 10,
  customModelSelector: "",
  customInputSelector: "",
  customSendSelector: "",
  customReplySelector: ""
};

const ALARM_NAME = "geb-keepalive-alarm";
const UPDATE_CHECK_ALARM_NAME = "geb-update-check-alarm";
const VERTEX_MATCH_PATTERN = "https://vertexaisearch.cloud.google.com/*";
const DEFAULT_VERTEX_HOME_URL =
  "https://vertexaisearch.cloud.google.com/home/cid/f8a26a7c-0f51-4098-bf58-52506661472b?hl=en_US";
const EXPECTED_DRIVER_VERSION = "2.8.9";
const DEFAULT_GITHUB_REPO = "StevenLee1471717/Gemini-Enterprise-Bridge";
const BUSY_REQUEST_SESSION_TTL_MS = 45 * 60 * 1000;

// Live non-serializable WebSocket handle & per-tab fast caches; durable settings live in chrome.storage
const socketRuntime = {
  ws: null,
  connecting: false,
  connectPromise: null,
  heartbeatTimer: null,
  reconnectTimer: null,
  reportStatusTimer: null,
  reconnectAttempts: 0
};

const runtimeTabMeta = new Map(); // tabId -> { activeModel, capturedRpc }
const injectedTabVersions = new Map(); // tabId -> driverVersion
const busyRequestsByTab = new Map(); // requestId -> tabId
const busyRequestTimestamps = new Map(); // requestId -> epochMs
const preparingRequests = new Map(); // requestId -> { cancelled }
const spawningTabIds = new Set(); // tabIds currently mid-spawn before URL commits
let backendBusyTabIds = new Set(); // session-level active tabIds pushed by Bridge
let managedTabOrder = new Set(); // stable admitted tabIds within maxParallelTabs
const pendingManualTabIds = new Set(); // immediate guard while new manual tab persists
let lastSavedVertexHomeUrl = "";
let pendingIdleExtensionReload = false;

function maybePerformIdleExtensionReload() {
  if (!pendingIdleExtensionReload) return;
  if (busyRequestsByTab.size > 0) return;
  pendingIdleExtensionReload = false;
  if (typeof chrome.runtime?.reload === "function") {
    setTimeout(() => {
      if (busyRequestsByTab.size === 0) {
        try {
          chrome.runtime.reload();
        } catch (_) {}
      }
    }, 150);
  }
}

function normalizeMaxParallelTabs(val, fallback = 10) {
  let parsed = NaN;
  if (typeof val === "string") {
    const trimmed = val.trim();
    if (!trimmed) return fallback;
    if (!/^\d+$/.test(trimmed)) return fallback;
    parsed = Number(trimmed);
  } else if (typeof val === "number") {
    parsed = val;
  } else {
    return fallback;
  }
  if (!Number.isInteger(parsed) || parsed < 1) return fallback;
  return Math.min(200, parsed);
}

function extractVertexSessionKey(rawUrl) {
  if (!rawUrl || typeof rawUrl !== "string") return "";
  try {
    const u = new URL(rawUrl);
    if (!u.hostname.includes("vertexaisearch.cloud.google.com")) return "";
    const m =
      u.pathname.match(/\/r\/session\/([^/?#]+)/i) ||
      u.pathname.match(/\/session\/([^/?#]+)/i) ||
      u.pathname.match(/\/c\/([^/?#]+)/i);
    return m && m[1] ? `session:${m[1]}` : "";
  } catch (_) {
    return "";
  }
}

function persistBusyRequestsToSession() {
  try {
    const now = Date.now();
    const serialized = {};
    for (const [reqId, tid] of busyRequestsByTab) {
      const ts = busyRequestTimestamps.get(reqId) || now;
      if (now - ts <= BUSY_REQUEST_SESSION_TTL_MS) {
        serialized[reqId] = { tabId: tid, ts };
      }
    }
    chrome.storage.session.set({ busyRequestsMap: serialized }).catch(() => {});
  } catch (_) {}
}

async function hydrateBusyRequestsFromSession() {
  try {
    const { busyRequestsMap } = await chrome.storage.session.get("busyRequestsMap");
    if (!busyRequestsMap || typeof busyRequestsMap !== "object") return;
    const now = Date.now();
    let dirty = false;
    for (const [reqId, entry] of Object.entries(busyRequestsMap)) {
      const tid = Number(entry && entry.tabId);
      const ts = Number(entry && entry.ts) || 0;
      if (!reqId || !Number.isFinite(tid) || tid <= 0 || now - ts > BUSY_REQUEST_SESSION_TTL_MS) {
        dirty = true;
        continue;
      }
      if (!busyRequestsByTab.has(reqId)) {
        busyRequestsByTab.set(reqId, tid);
        busyRequestTimestamps.set(reqId, ts);
      }
    }
    if (dirty) persistBusyRequestsToSession();
  } catch (_) {}
}

function setBusyRequestForTab(requestId, tabId) {
  busyRequestsByTab.set(requestId, tabId);
  busyRequestTimestamps.set(requestId, Date.now());
  persistBusyRequestsToSession();
}

function deleteBusyRequest(requestId) {
  busyRequestTimestamps.delete(requestId);
  const deleted = busyRequestsByTab.delete(requestId);
  if (deleted) persistBusyRequestsToSession();
  return deleted;
}

function clearBusyRequestsByTabId(tabId) {
  const numTabId = Number(tabId);
  let removed = false;
  for (const [reqId, tid] of Array.from(busyRequestsByTab.entries())) {
    if (tid === numTabId) {
      busyRequestsByTab.delete(reqId);
      busyRequestTimestamps.delete(reqId);
      removed = true;
    }
  }
  if (removed) persistBusyRequestsToSession();
  return removed;
}

function isTabCurrentlyBusy(tabId) {
  if (!tabId) return false;
  if (backendBusyTabIds.has(Number(tabId))) return true;
  const now = Date.now();
  for (const [reqId, tid] of Array.from(busyRequestsByTab.entries())) {
    const ts = busyRequestTimestamps.get(reqId) || now;
    if (now - ts > BUSY_REQUEST_SESSION_TTL_MS) {
      busyRequestsByTab.delete(reqId);
      busyRequestTimestamps.delete(reqId);
      continue;
    }
    if (tid === tabId) return true;
  }
  return false;
}

function applyBackendBusyTabIds(rawIds) {
  const next = new Set(
    (Array.isArray(rawIds) ? rawIds : [])
      .map((n) => Number(n))
      .filter((n) => Number.isFinite(n) && n > 0)
  );
  let changed = next.size !== backendBusyTabIds.size;
  if (!changed) {
    for (const id of next) {
      if (!backendBusyTabIds.has(id)) {
        changed = true;
        break;
      }
    }
  }
  if (changed) {
    backendBusyTabIds = next;
    void broadcastTabTakeoverStates();
  }
}

async function getManualExclusions() {
  try {
    const data = await chrome.storage.local.get(["manualTabIds", "manualSessionKeys"]);
    const tabIds = Array.isArray(data?.manualTabIds)
      ? data.manualTabIds.map((n) => Number(n)).filter((n) => Number.isFinite(n) && n > 0)
      : [];
    const sessionKeys = Array.isArray(data?.manualSessionKeys)
      ? data.manualSessionKeys.map((s) => String(s || "").trim()).filter(Boolean)
      : [];
    return {
      manualTabIds: new Set(tabIds),
      manualSessionKeys: new Set(sessionKeys)
    };
  } catch (_) {
    return { manualTabIds: new Set(), manualSessionKeys: new Set() };
  }
}

async function setTabManualState(tabId, explicitManual = null) {
  const numId = Number(tabId);
  if (!Number.isFinite(numId) || numId <= 0) {
    throw new Error("无效的标签页编号");
  }
  let tab = null;
  try {
    tab = await chrome.tabs.get(numId);
  } catch (_) {}
  const { manualTabIds, manualSessionKeys } = await getManualExclusions();
  const skey = tab && tab.url ? extractVertexSessionKey(tab.url) : "";
  const currentlyManual = manualTabIds.has(numId) || Boolean(skey && manualSessionKeys.has(skey));
  const targetManual = typeof explicitManual === "boolean" ? explicitManual : !currentlyManual;

  if (targetManual) {
    manualTabIds.add(numId);
    if (skey) manualSessionKeys.add(skey);
    managedTabOrder.delete(numId);
  } else {
    pendingManualTabIds.delete(numId);
    manualTabIds.delete(numId);
    if (skey) manualSessionKeys.delete(skey);
  }

  const cappedSessions = Array.from(manualSessionKeys).slice(-200);
  await chrome.storage.local.set({
    manualTabIds: Array.from(manualTabIds),
    manualSessionKeys: cappedSessions
  });
  if (targetManual) {
    pendingManualTabIds.delete(numId);
  }
  await appendLog(
    "info",
    targetManual
      ? `标签页 #${numId} 已设为手动自用（Bridge 不接管此会话）`
      : `标签页 #${numId} 已恢复交给 Bridge 接管`
  );
  await reportTabStatusToBackend();
  return targetManual;
}

async function classifyAllVertexTabs() {
  const settings = await getSettings();
  const maxTabs = normalizeMaxParallelTabs(settings.maxParallelTabs, 10);
  const wsConnected = Boolean(socketRuntime.ws && socketRuntime.ws.readyState === WebSocket.OPEN);
  const rawTabs = (await chrome.tabs.query({ url: VERTEX_MATCH_PATTERN })) || [];
  const seenRawIds = new Set(rawTabs.filter((t) => t && t.id).map((t) => t.id));
  for (const spId of Array.from(spawningTabIds)) {
    if (seenRawIds.has(spId)) continue;
    try {
      const spTab = await chrome.tabs.get(spId);
      if (spTab && spTab.id) {
        rawTabs.push(spTab);
        seenRawIds.add(spTab.id);
      } else {
        spawningTabIds.delete(spId);
      }
    } catch (_) {
      spawningTabIds.delete(spId);
    }
  }
  const allTabs = rawTabs.filter((t) =>
    Boolean(
      t &&
        t.id &&
        (spawningTabIds.has(t.id) || isUsableVertexTabUrl(t.url || t.pendingUrl || ""))
    )
  );
  for (const t of allTabs) {
    const normalizedHome = normalizeVertexHomeUrl(t.url || "");
    if (normalizedHome && hasVertexWorkspacePath(normalizedHome)) {
      void rememberVertexHomeUrl(normalizedHome);
      break;
    }
  }
  const { manualTabIds, manualSessionKeys } = await getManualExclusions();

  let sessionKeysDirty = false;
  const manualSet = new Set();
  for (const t of allTabs) {
    const skey = extractVertexSessionKey(t.url || "");
    const byTab = manualTabIds.has(t.id) || pendingManualTabIds.has(t.id);
    const bySession = Boolean(skey && manualSessionKeys.has(skey));
    if (byTab || bySession) {
      manualSet.add(t.id);
      if (byTab && skey && !manualSessionKeys.has(skey)) {
        manualSessionKeys.add(skey);
        sessionKeysDirty = true;
      }
    }
  }
  if (sessionKeysDirty) {
    try {
      await chrome.storage.local.set({
        manualSessionKeys: Array.from(manualSessionKeys).slice(-200)
      });
    } catch (_) {}
  }

  const candidates = allTabs.filter((t) => !manualSet.has(t.id));
  const prioritized = candidates.slice().sort((a, b) => {
    const aBusy = isTabCurrentlyBusy(a.id) ? 0 : 1;
    const bBusy = isTabCurrentlyBusy(b.id) ? 0 : 1;
    if (aBusy !== bBusy) return aBusy - bBusy;
    const aKnown = managedTabOrder.has(a.id) ? 0 : 1;
    const bKnown = managedTabOrder.has(b.id) ? 0 : 1;
    if (aKnown !== bKnown) return aKnown - bKnown;
    return 0;
  });
  const admittedList = prioritized.slice(0, maxTabs);
  const admittedIds = new Set(admittedList.map((t) => t.id));
  managedTabOrder = new Set(admittedIds);

  const managedTabs = allTabs.filter((t) => admittedIds.has(t.id));
  const manualTabs = allTabs.filter((t) => manualSet.has(t.id));
  const overflowTabs = allTabs.filter((t) => !manualSet.has(t.id) && !admittedIds.has(t.id));

  const seatMap = new Map();
  managedTabs.forEach((t, idx) => seatMap.set(t.id, idx + 1));

  const perTabState = new Map();
  let busyCount = 0;
  for (const t of allTabs) {
    const isManual = manualSet.has(t.id);
    const isAdmitted = admittedIds.has(t.id);
    const isBusy = isTabCurrentlyBusy(t.id);
    if (isBusy) busyCount += 1;
    const seatIndex = seatMap.get(t.id) || null;
    let mode = "managed";
    if (isManual) {
      mode = "manual";
    } else if (!settings.enabled) {
      mode = "paused";
    } else if (!isAdmitted) {
      mode = "overflow";
    } else if (isBusy) {
      mode = "working";
    } else if (!wsConnected) {
      mode = "disconnected";
    }
    perTabState.set(t.id, {
      tabId: t.id,
      mode,
      manual: isManual,
      managed: isAdmitted && Boolean(settings.enabled),
      admitted: isAdmitted,
      overflow: !isManual && !isAdmitted,
      busy: isBusy,
      seatIndex,
      maxTabs,
      wsConnected,
      enabled: Boolean(settings.enabled)
    });
  }

  return {
    settings,
    wsConnected,
    maxTabs,
    busyCount,
    allTabs,
    managedTabs,
    manualTabs,
    overflowTabs,
    perTabState
  };
}

async function broadcastTabTakeoverStates(precomputed = null) {
  try {
    const cls = precomputed || (await classifyAllVertexTabs());
    for (const t of cls.allTabs) {
      if (!t.id) continue;
      const state = cls.perTabState.get(t.id);
      if (!state) continue;
      chrome.tabs
        .sendMessage(t.id, { type: "GEB_TAB_TAKEOVER_STATE", state })
        .catch(() => {});
    }
  } catch (_) {}
}

async function getSettings() {
  const stored = await chrome.storage.local.get(Object.keys(DEFAULT_SETTINGS));
  const merged = { ...DEFAULT_SETTINGS, ...stored };
  const normCap = normalizeMaxParallelTabs(merged.maxParallelTabs, DEFAULT_SETTINGS.maxParallelTabs);
  if (merged.maxParallelTabs !== normCap) {
    merged.maxParallelTabs = normCap;
    chrome.storage.local.set({ maxParallelTabs: normCap }).catch(() => {});
  }
  return merged;
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

async function getPreferredTabId() {
  return null;
}

async function getEligibleVertexTabs() {
  const cls = await classifyAllVertexTabs();
  return cls.managedTabs;
}

async function findTargetVertexTab(requestedTabId = null) {
  if (requestedTabId !== null && requestedTabId !== undefined) {
    try {
      const eligible = await getEligibleVertexTabs();
      const exact = eligible.find((tab) => tab.id === Number(requestedTabId));
      if (exact && exact.id) {
        return exact;
      }
    } catch (_) {}
    return null;
  }
  return await findBestVertexTab();
}

async function findBestVertexTab() {
  const tabs = await getEligibleVertexTabs();
  if (tabs.length === 0) {
    return null;
  }
  const idleTabs = tabs.filter((t) => !isTabCurrentlyBusy(t.id));
  const pool = idleTabs.length > 0 ? idleTabs : tabs;
  const activeInWindow = pool.find((t) => t.active);
  return activeInWindow || pool[0];
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

// Probe the receiver before sending; error pages can retain a normal tab URL.
async function ensureTabBridgeReady(tabId) {
  const probe = async () => {
    const response = await chrome.tabs.sendMessage(tabId, { type: "GEB_GET_VISIBILITY" });
    if (!response || !response.ok) throw new Error("Page bridge did not respond");
    return response;
  };
  try {
    const status = await probe();
    const needsDriverUpgrade = status.driverVersion
      ? status.driverVersion !== EXPECTED_DRIVER_VERSION
      : Boolean(status.visibilityState);
    if (!needsDriverUpgrade) {
      injectedTabVersions.set(tabId, status.driverVersion || EXPECTED_DRIVER_VERSION);
      return;
    }
  } catch (_) {}
  await ensureScriptsInjected(tabId);
  try {
    const status = await probe();
    injectedTabVersions.set(tabId, status.driverVersion || EXPECTED_DRIVER_VERSION);
    return;
  } catch (_) {}

  await appendLog("warn", `Recovering unavailable page bridge on tab #${tabId}`);
  const loaded = waitForTabComplete(tabId, 20000);
  await chrome.tabs.reload(tabId);
  await loaded;
  await ensureScriptsInjected(tabId);
  const status = await probe();
  injectedTabVersions.set(tabId, status.driverVersion || EXPECTED_DRIVER_VERSION);
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

async function buildReportedTabsSnapshot(precomputedCls = null) {
  const cls = precomputedCls || (await classifyAllVertexTabs());
  const tabs = cls.managedTabs;
  const { capturedRpc = false, activeModel = "", availableModels = [] } = await chrome.storage.session.get([
    "capturedRpc",
    "activeModel",
    "availableModels"
  ]);
  return tabs.map((t) => {
    const meta = runtimeTabMeta.get(t.id) || {};
    return {
      tab_id: t.id,
      tab_url: t.url || "",
      tab_title: t.title || "",
      ready: Boolean(t.id),
      active_model: meta.activeModel || activeModel || "",
      available_models: Array.isArray(meta.availableModels) && meta.availableModels.length > 0
        ? meta.availableModels
        : (Array.isArray(availableModels) ? availableModels : []),
      captured_rpc: meta.capturedRpc !== undefined ? Boolean(meta.capturedRpc) : Boolean(capturedRpc)
    };
  });
}

async function reportTabStatusToBackend() {
  const cls = await classifyAllVertexTabs();
  const settings = cls.settings;
  const tabsSnapshot = await buildReportedTabsSnapshot(cls);
  const { availableModels = [] } = await chrome.storage.session.get(["availableModels"]);
  const primary = tabsSnapshot[0] || null;
  const capabilities = ["multi_tab", "reset_tab", "open_session", "session_busy_sync", "usage_push", "auto_update"];
  if (settings.autoSpawnTabs !== false) {
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
    driver_version: EXPECTED_DRIVER_VERSION,
    tab_id: primary ? primary.tab_id : null,
    tab_url: primary ? primary.tab_url : "",
    tab_title: primary ? primary.tab_title : "",
    ready: tabsSnapshot.length > 0,
    driver_mode: settings.driverMode,
    captured_rpc: tabsSnapshot.some((t) => t.captured_rpc),
    active_model: primary ? primary.active_model : "",
    available_models: Array.isArray(availableModels) ? availableModels : [],
    capabilities,
    max_parallel_tabs: normalizeMaxParallelTabs(settings.maxParallelTabs, 10),
    tabs: tabsSnapshot,
    user_agent: navigator.userAgent
  });

  await broadcastTabTakeoverStates(cls);
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

function scheduleReportTabStatus(delayMs = 150) {
  if (socketRuntime.reportStatusTimer !== null) return;
  socketRuntime.reportStatusTimer = setTimeout(() => {
    socketRuntime.reportStatusTimer = null;
    void reportTabStatusToBackend();
  }, Math.max(20, Number(delayMs) || 150));
}

function clearHeartbeatInterval() {
  if (socketRuntime.heartbeatTimer !== null) {
    clearInterval(socketRuntime.heartbeatTimer);
    socketRuntime.heartbeatTimer = null;
  }
}

function startHeartbeatInterval() {
  clearHeartbeatInterval();
  // Touch chrome.storage.session + send WS heartbeat every 18s to reset Chromium's 30s MV3 SW idle timer
  socketRuntime.heartbeatTimer = setInterval(() => {
    void (async () => {
      await chrome.storage.session.set({ lastSwHeartbeatAt: Date.now() }).catch(() => {});
      const settings = await getSettings();
      const tabsSnapshot = await buildReportedTabsSnapshot();
      const primary = tabsSnapshot[0] || null;
      const preferredTabId = await getPreferredTabId();
      const capabilities = ["multi_tab", "reset_tab", "open_session", "session_busy_sync", "usage_push", "auto_update"];
      if (settings.autoSpawnTabs !== false && !preferredTabId) {
        capabilities.push("spawn_tab");
      }
      sendWsJson({
        type: "heartbeat",
        extension_version: getExtensionVersion(),
        driver_version: EXPECTED_DRIVER_VERSION,
        tab_id: primary ? primary.tab_id : null,
        tab_url: primary ? primary.tab_url : "",
        tab_title: primary ? primary.tab_title : "",
        ready: tabsSnapshot.length > 0,
        driver_mode: settings.driverMode,
        captured_rpc: tabsSnapshot.some((t) => t.captured_rpc),
        capabilities,
        max_parallel_tabs: normalizeMaxParallelTabs(settings.maxParallelTabs, 10),
        tabs: tabsSnapshot
      });
    })();
  }, 18000);
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
  const baseDelay = attempt === 0 ? 150 : Math.min(400 * Math.pow(2, attempt), 10000);
  const jitter = Math.floor(Math.random() * 120);
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
  if (backendBusyTabIds.size > 0) {
    backendBusyTabIds.clear();
    void broadcastTabTakeoverStates();
  }
  await setSessionStatus({ wsConnected: false });
}

async function ensureWebSocketConnected(forceReconnect = false, fromBackoffTimer = false) {
  if (!forceReconnect) {
    if (socketRuntime.ws && socketRuntime.ws.readyState === WebSocket.OPEN) {
      return;
    }
    if (socketRuntime.connectPromise) {
      await socketRuntime.connectPromise;
      return;
    }
    if (!fromBackoffTimer && socketRuntime.reconnectTimer !== null) {
      return;
    }
  }

  const connectTask = (async () => {
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
      return;
    }

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
  })();

  socketRuntime.connectPromise = connectTask;
  try {
    await connectTask;
  } finally {
    if (socketRuntime.connectPromise === connectTask) {
      socketRuntime.connectPromise = null;
    }
  }
}

function hasVertexWorkspacePath(rawOrPath) {
  return /\/(?:cid|app)\/[^/?#]+/i.test(String(rawOrPath || ""));
}

function isUsableVertexTabUrl(rawUrl) {
  if (!rawUrl || typeof rawUrl !== "string") return false;
  try {
    const u = new URL(rawUrl.trim());
    if (!u.hostname.toLowerCase().endsWith("vertexaisearch.cloud.google.com")) {
      return false;
    }
    const cleanPath = (u.pathname || "").replace(/\/+$/, "");
    return Boolean(cleanPath);
  } catch (_) {
    return false;
  }
}

function normalizeVertexHomeUrl(rawUrl) {
  if (!rawUrl || typeof rawUrl !== "string") return "";
  try {
    const u = new URL(rawUrl.trim());
    if (!u.hostname.toLowerCase().endsWith("vertexaisearch.cloud.google.com")) {
      return "";
    }
    const nextPath = (u.pathname || "")
      .replace(/\/r\/session\/[^/?#]+.*$/i, "")
      .replace(/\/session\/[^/?#]+.*$/i, "")
      .replace(/\/c\/[^/?#]+.*$/i, "")
      .replace(/\/+$/, "");
    if (!nextPath) {
      return "";
    }
    u.pathname = nextPath;
    u.hash = "";
    return u.toString();
  } catch (_) {
    return "";
  }
}

async function rememberVertexHomeUrl(cleanUrl) {
  if (!cleanUrl || !hasVertexWorkspacePath(cleanUrl)) return;
  if (cleanUrl === lastSavedVertexHomeUrl) return;
  lastSavedVertexHomeUrl = cleanUrl;
  try {
    await chrome.storage.local.set({ lastVertexHomeUrl: cleanUrl });
  } catch (_) {}
}

async function resolvePreferredVertexHomeUrl(tabs = [], baseUrlHint = "") {
  const hintClean = normalizeVertexHomeUrl(baseUrlHint);
  if (hintClean && hasVertexWorkspacePath(hintClean)) {
    await rememberVertexHomeUrl(hintClean);
    return hintClean;
  }

  const orderedTabs = Array.isArray(tabs)
    ? tabs.filter((t) => t && t.active).concat(tabs.filter((t) => t && !t.active))
    : [];
  for (const t of orderedTabs) {
    const clean = normalizeVertexHomeUrl((t && t.url) || "");
    if (clean && hasVertexWorkspacePath(clean)) {
      await rememberVertexHomeUrl(clean);
      return clean;
    }
  }

  let storedClean = "";
  try {
    const data = await chrome.storage.local.get("lastVertexHomeUrl");
    storedClean = normalizeVertexHomeUrl(data && data.lastVertexHomeUrl ? data.lastVertexHomeUrl : "");
    if (storedClean && hasVertexWorkspacePath(storedClean)) {
      lastSavedVertexHomeUrl = storedClean;
      return storedClean;
    }
  } catch (_) {}

  if (hintClean) return hintClean;
  for (const t of orderedTabs) {
    const clean = normalizeVertexHomeUrl((t && t.url) || "");
    if (clean) return clean;
  }
  if (storedClean) return storedClean;
  return DEFAULT_VERTEX_HOME_URL;
}

function computeCleanVertexHomeUrl(rawUrl) {
  if (!rawUrl) return "";
  try {
    const u = new URL(rawUrl);
    if (!u.hostname.toLowerCase().endsWith("vertexaisearch.cloud.google.com")) {
      return "";
    }
    const nextPath = u.pathname
      .replace(/\/r\/session\/[^/?#]+.*$/i, "")
      .replace(/\/session\/[^/?#]+.*$/i, "")
      .replace(/\/c\/[^/?#]+.*$/i, "")
      .replace(/\/+$/, "");
    if (nextPath && nextPath !== u.pathname) {
      u.pathname = nextPath;
      u.hash = "";
      return u.toString();
    }
  } catch (_) {}
  return "";
}

function extractVertexSessionIdFromUrl(rawUrl) {
  if (!rawUrl || typeof rawUrl !== "string") return "";
  try {
    const u = new URL(rawUrl.trim());
    if (!u.hostname.toLowerCase().endsWith("vertexaisearch.cloud.google.com")) {
      return "";
    }
    const m = u.pathname.match(/\/(?:r\/session|session|c)\/([^/?#]+)/i);
    return m && m[1] ? decodeURIComponent(m[1]).trim() : "";
  } catch (_) {
    return "";
  }
}

async function waitForSessionPageReady(tabId, targetSessionId, timeoutMs = 12000) {
  const startedAt = Date.now();
  const deadline = startedAt + Math.max(1000, Number(timeoutMs) || 12000);
  let lastUrl = "";
  let lastTitle = "";
  let lastActiveModel = "";
  while (Date.now() < deadline) {
    let latestTab = null;
    try {
      latestTab = await chrome.tabs.get(tabId);
      if (latestTab) {
        lastUrl = latestTab.url || lastUrl;
        lastTitle = latestTab.title || lastTitle;
      }
    } catch (_) {}

    let diag = null;
    try {
      diag = await chrome.tabs.sendMessage(tabId, { type: "GEB_DIAGNOSE_PAGE" });
    } catch (_) {
      await ensureScriptsInjected(tabId);
    }

    if (diag && diag.ok) {
      lastUrl = diag.url || lastUrl;
      lastTitle = diag.title || lastTitle;
      if (diag.activeModel) {
        lastActiveModel = diag.activeModel;
      }
      const currentSid = extractVertexSessionIdFromUrl(lastUrl);
      if (targetSessionId) {
        if (!currentSid && Date.now() - startedAt >= 800) {
          return {
            ok: false,
            redirected: true,
            tab_url: lastUrl,
            tab_title: lastTitle,
            active_model: lastActiveModel,
            error: "历史会话已被重定向回空白首页"
          };
        }
        if (currentSid === targetSessionId && diag.inputFound) {
          return {
            ok: true,
            tab_url: lastUrl,
            tab_title: lastTitle,
            active_model: lastActiveModel
          };
        }
      } else if (diag.inputFound) {
        return {
          ok: true,
          tab_url: lastUrl,
          tab_title: lastTitle,
          active_model: lastActiveModel
        };
      }
    }
    await new Promise((r) => setTimeout(r, 250));
  }
  return {
    ok: false,
    tab_url: lastUrl,
    tab_title: lastTitle,
    active_model: lastActiveModel,
    error: "等待历史会话输入框就绪超时"
  };
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
    injectedTabVersions.set(freshTab.id, EXPECTED_DRIVER_VERSION);
    try {
      freshTab = await chrome.tabs.get(freshTab.id);
    } catch (_) {}
  }

  return freshTab;
}

async function spawnParallelWorkerTab(baseUrlHint = "", preserveSessionUrl = false) {
  const cls = await classifyAllVertexTabs();
  const allVertexTabs = cls.allTabs;
  const maxTabs = cls.maxTabs;
  if (cls.managedTabs.length >= maxTabs) {
    throw new Error(`已达到最大并行标签页上限 (${maxTabs})`);
  }

  let targetUrl = "";
  if (preserveSessionUrl && isUsableVertexTabUrl(baseUrlHint)) {
    targetUrl = baseUrlHint.trim();
  } else {
    targetUrl = await resolvePreferredVertexHomeUrl(allVertexTabs, baseUrlHint);
  }

  const createOpts = { url: targetUrl, active: false };
  if (allVertexTabs.length > 0 && allVertexTabs[0].windowId !== undefined) {
    createOpts.windowId = allVertexTabs[0].windowId;
  }

  const created = await chrome.tabs.create(createOpts);
  if (!created || !created.id) {
    throw new Error("无法创建新的后台工作标签页");
  }

  spawningTabIds.add(created.id);
  managedTabOrder.add(created.id);
  try {
    const loadPromise =
      created.status === "complete" ? Promise.resolve() : waitForTabComplete(created.id, 14000);
    await protectTabFromFreeze(created.id);
    await loadPromise;
    await new Promise((r) => setTimeout(r, 600));
    await ensureScriptsInjected(created.id);
    injectedTabVersions.set(created.id, EXPECTED_DRIVER_VERSION);

    const targetSid = preserveSessionUrl ? extractVertexSessionIdFromUrl(targetUrl) : "";
    let sessionProbe = null;
    if (targetSid) {
      sessionProbe = await waitForSessionPageReady(created.id, targetSid, 10000);
      if (sessionProbe.active_model) {
        const prevMeta = runtimeTabMeta.get(created.id) || {};
        runtimeTabMeta.set(created.id, { ...prevMeta, activeModel: sessionProbe.active_model });
      }
    }

    let latest = created;
    try {
      latest = await chrome.tabs.get(created.id);
    } catch (_) {}
    const prevMeta = runtimeTabMeta.get(latest.id) || {};
    runtimeTabMeta.set(latest.id, {
      ...prevMeta,
      knownVertexUrl: (sessionProbe && sessionProbe.tab_url) || latest.url || targetUrl
    });

    await appendLog("info", `Spawned parallel worker tab #${latest.id}`);
    await reportTabStatusToBackend();
    return {
      tab_id: latest.id,
      tab_url: (sessionProbe && sessionProbe.tab_url) || latest.url || targetUrl,
      tab_title: (sessionProbe && sessionProbe.tab_title) || latest.title || "Gemini Enterprise",
      active_model: (sessionProbe && sessionProbe.active_model) || "",
      ready: true
    };
  } finally {
    spawningTabIds.delete(created.id);
  }
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
function assembleChunks(entry, total) {
  if (!entry || !Number.isInteger(total) || total < 1 || total > 10000 || entry.total !== total || entry.parts.length !== total) {
    throw new Error("传输分段缺失或数量不一致，已停止处理不完整内容。");
  }
  const parts = Array.from({length: total}, (_, index) => entry.parts[index]);
  if (parts.some(part => typeof part !== "string")) {
    throw new Error("传输分段缺失，已停止处理不完整内容。");
  }
  return parts.join("");
}

const swPromptReassembly = new Map();
const swReplyReassembly = new Map();

function getExtensionVersion() {
  try {
    const mf = chrome.runtime?.getManifest?.();
    return mf && mf.version ? String(mf.version) : "1.5.2";
  } catch (_) {
    return "1.5.2";
  }
}

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

function resolveAttachmentUrl(att, wsUrl) {
  const raw = String((att && (att.fetch_url || att.url || att.path)) || "").trim();
  if (!raw) return "";
  if (/^https?:\/\//i.test(raw)) return raw;
  const base = deriveHttpBaseFromWs(wsUrl).replace(/\/+$/, "");
  return `${base}${raw.startsWith("/") ? raw : "/" + raw}`;
}

async function checkGitHubReleaseDirectly(repo = DEFAULT_GITHUB_REPO) {
  const cleanRepo = String(repo || DEFAULT_GITHUB_REPO).trim().replace(/^\/+|\/+$/g, "");
  try {
    const apiRes = await fetch(`https://api.github.com/repos/${cleanRepo}/releases/latest`);
    if (apiRes.ok) {
      const data = await apiRes.json();
      const tag = String(data.tag_name || "").replace(/^[vV]/, "");
      if (tag) {
        return { ok: true, version: tag, releaseUrl: data.html_url || `https://github.com/${cleanRepo}/releases` };
      }
    }
  } catch (_) {}
  try {
    const mfRes = await fetch(`https://github.com/${cleanRepo}/releases/latest/download/release-manifest.json`);
    if (mfRes.ok) {
      const mf = await mfRes.json();
      if (mf && mf.version) {
        return { ok: true, version: String(mf.version).replace(/^[vV]/, ""), releaseUrl: mf.release_url || `https://github.com/${cleanRepo}/releases` };
      }
    }
  } catch (err) {
    return { ok: false, error: err && err.message ? err.message : String(err) };
  }
  return { ok: false, error: "no_release" };
}

async function checkUpdatesAndAutoSync(force = false) {
  const settings = await getSettings();
  const httpBase = deriveHttpBaseFromWs(settings.wsUrl);
  let backendReturnedRemoteVersion = false;
  let latestGitHubVersion = "";
  let githubRepo = DEFAULT_GITHUB_REPO;
  try {
    const bRes = await fetch(`${httpBase}/api/update/status?force=${force ? "true" : "false"}`);
    if (bRes.ok) {
      const data = await bRes.json();
      if (data && data.repo) githubRepo = data.repo;
      if (data && data.latest_github_version) {
        latestGitHubVersion = String(data.latest_github_version).replace(/^[vV]/, "");
        backendReturnedRemoteVersion = true;
      }
    }
  } catch (_) {}
  if (!backendReturnedRemoteVersion) {
    const direct = await checkGitHubReleaseDirectly(githubRepo);
    if (direct.ok && direct.version) {
      latestGitHubVersion = direct.version;
    }
  }
  return { ok: true, latestGitHubVersion, extensionVersion: getExtensionVersion() };
}

function arrayBufferToBase64(buffer) {
  const bytes = new Uint8Array(buffer);
  const chunk = 0x8000;
  const parts = [];
  for (let i = 0; i < bytes.length; i += chunk) {
    parts.push(String.fromCharCode.apply(null, bytes.subarray(i, i + chunk)));
  }
  return btoa(parts.join(""));
}

const ATTACHMENT_IPC_CHUNK_SIZE = 196608;

function sanitizeMimeHeader(rawMime) {
  const cleaned = String(rawMime || "").split(";")[0].split(",")[0].trim();
  return cleaned && cleaned.includes("/") ? cleaned : "application/octet-stream";
}

async function prepareAttachmentsForTab(tabId, requestId, rawAttachments) {
  const list = Array.isArray(rawAttachments) ? rawAttachments : [];
  if (list.length === 0) return [];
  const prepared = [];
  for (let idx = 0; idx < list.length; idx += 1) {
    const att = { ...list[idx] };
    const declaredMime = sanitizeMimeHeader(att.mime_type);
    let dataUrl = String(att.data_url || "").trim();
    if (dataUrl && !/^data:/i.test(dataUrl) && !/^(?:https?|blob|file):/i.test(dataUrl)) {
      dataUrl = `data:${declaredMime};base64,${dataUrl.replace(/\s+/g, "")}`;
    }
    if (!dataUrl) {
      const rawB64 = att.data_b64 || att.b64_json || att.file_data || att.data || "";
      if (typeof rawB64 === "string" && rawB64.trim().length > 0) {
        dataUrl = /^data:/i.test(rawB64.trim())
          ? rawB64.trim()
          : `data:${declaredMime};base64,${rawB64.replace(/\s+/g, "")}`;
      }
    }
    if (!dataUrl && (att.fetch_url || att.url || att.path)) {
      const settingsForAtt = await getSettings();
      const targetFetchUrl = resolveAttachmentUrl(att, settingsForAtt.wsUrl);
      const resp = await fetch(targetFetchUrl);
      if (!resp.ok) {
        throw new Error(`拉取本地暂存附件失败 (${att.name || att.filename || idx}): HTTP ${resp.status}`);
      }
      const buf = await resp.arrayBuffer();
      const headerMime = resp.headers && resp.headers.get ? resp.headers.get("content-type") : "";
      const mime = sanitizeMimeHeader(att.mime_type || headerMime);
      dataUrl = `data:${mime};base64,${arrayBufferToBase64(buf)}`;
    }
    if (dataUrl.length > ATTACHMENT_IPC_CHUNK_SIZE) {
      const attId = String(att.id || `att-${idx}`);
      const attKey = `${requestId}:${idx}`;
      const totalChunks = Math.ceil(dataUrl.length / ATTACHMENT_IPC_CHUNK_SIZE);
      for (let c = 0; c < totalChunks; c += 1) {
        await chrome.tabs.sendMessage(tabId, {
          type: "GEB_ATTACHMENT_CHUNK",
          att_key: attKey,
          request_id: requestId,
          attachment_id: attId,
          chunk_index: c,
          total_chunks: totalChunks,
          data: dataUrl.slice(c * ATTACHMENT_IPC_CHUNK_SIZE, (c + 1) * ATTACHMENT_IPC_CHUNK_SIZE)
        });
      }
      prepared.push({
        ...att,
        data_url: "",
        chunked_att_key: attKey,
        total_chunks: totalChunks
      });
    } else {
      prepared.push({ ...att, data_url: dataUrl });
    }
  }
  return prepared;
}

let backendActiveRequestIds = new Set();
let backendActiveRequestIdsSynced = false;

async function syncBackendActiveRequestIds(rawReqIds) {
  if (!Array.isArray(rawReqIds)) return;
  backendActiveRequestIds = new Set(rawReqIds.map((id) => String(id || "").trim()).filter(Boolean));
  backendActiveRequestIdsSynced = true;
  let releasedAny = false;
  const now = Date.now();
  for (const [reqId, tabId] of Array.from(busyRequestsByTab.entries())) {
    if (preparingRequests.has(reqId)) continue;
    if (backendActiveRequestIds.has(reqId)) continue;
    const startedAt = busyRequestTimestamps.get(reqId) || 0;
    if (startedAt > 0 && now - startedAt < 2500) continue;
    deleteBusyRequest(reqId);
    releasedAny = true;
    if (tabId) {
      chrome.tabs.sendMessage(tabId, { type: "GEB_CANCEL_REQUEST", request_id: reqId }).catch(() => {});
    }
  }
  if (releasedAny) {
    void broadcastTabTakeoverStates();
    maybePerformIdleExtensionReload();
  }
}

async function postCallbackFallback(eventData) {
  if (typeof fetch !== "function") return false;
  try {
    const settings = await getSettings();
    const clientId = await getClientId();
    const callbackUrl = eventData.callback_url || deriveCallbackUrlFromWs(settings.wsUrl);
    const resp = await fetch(callbackUrl, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        client_id: clientId,
        tab_id: eventData.tab_id || null,
        request_id: eventData.request_id,
        reply: eventData.type === "request_complete" ? String(eventData.reply || "") : null,
        error: eventData.type === "request_error" ? String(eventData.error || "浏览器页面执行失败") : null,
        submitted: typeof eventData.submitted === "boolean" ? eventData.submitted : undefined,
        tab_reset: typeof eventData.tab_reset === "boolean" ? eventData.tab_reset : undefined,
        citations: eventData.citations || [],
        source: eventData.source || "http_callback",
        output_images: eventData.output_images || [],
        output_files: eventData.output_files || [],
        active_model: eventData.active_model || null,
        tab_url: eventData.tab_url || null,
        tab_title: eventData.tab_title || null
      })
    });
    return Boolean(resp && resp.ok);
  } catch (_) {
    return false;
  }
}

async function deliverResultToBackend(eventData) {
  const isTerminal =
    eventData &&
    (eventData.type === "request_complete" || eventData.type === "request_error") &&
    Boolean(eventData.request_id);
  if (isTerminal) {
    if (deleteBusyRequest(eventData.request_id)) {
      void broadcastTabTakeoverStates();
    }
  }
  const replyStr = String(eventData.reply || "");
  const hasMediaOutputs = (eventData.output_images || []).length > 0 || (eventData.output_files || []).length > 0;
  const wsOpenNow = Boolean(socketRuntime.ws && socketRuntime.ws.readyState === WebSocket.OPEN);

  // Always deliver large replies OR any terminal completion/error while WS is reconnecting via direct HTTP callback!
  if (
    (eventData.type === "request_complete" && (replyStr.length > LARGE_REPLY_THRESHOLD || hasMediaOutputs || !wsOpenNow)) ||
    (eventData.type === "request_error" && !wsOpenNow)
  ) {
    if (await postCallbackFallback(eventData)) {
      maybePerformIdleExtensionReload();
      return;
    }
  }

  if (eventData.type === "request_complete" && (replyStr.length > LARGE_REPLY_THRESHOLD || hasMediaOutputs)) {
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
    maybePerformIdleExtensionReload();
    return;
  }

  if (!socketRuntime.ws || socketRuntime.ws.readyState !== WebSocket.OPEN) {
    await ensureWebSocketConnected(false);
  }
  if (!sendWsJson(eventData) && isTerminal) {
    await postCallbackFallback(eventData);
  }
  maybePerformIdleExtensionReload();
}

async function handleBackendCommand(msg) {
  const type = msg.type || "";
  if (Array.isArray(msg.busy_tab_ids)) {
    applyBackendBusyTabIds(msg.busy_tab_ids);
  }
  if (Array.isArray(msg.active_request_ids)) {
    await syncBackendActiveRequestIds(msg.active_request_ids);
  }
  if (type === "sync_busy_tabs" || type === "welcome" || type === "pong" || type === "ack") {
    return;
  }
  if (type === "ping") {
    await reportTabStatusToBackend();
    return;
  }

  if (type === "set_max_parallel_tabs") {
    const nextMax = normalizeMaxParallelTabs(msg.max_parallel_tabs, 10);
    const patch = { maxParallelTabs: nextMax };
    if (typeof msg.auto_spawn_tabs === "boolean") {
      patch.autoSpawnTabs = msg.auto_spawn_tabs;
    }
    await chrome.storage.local.set(patch);
    await reportTabStatusToBackend();
    sendWsJson({
      type: "set_max_parallel_tabs_ack",
      ok: true,
      max_parallel_tabs: nextMax
    });
    return;
  }

  if (type === "extension_reload_available" || type === "reload_extension") {
    if (msg.when_idle && busyRequestsByTab.size > 0) {
      pendingIdleExtensionReload = true;
      if (type === "reload_extension") {
        sendWsJson({ type: "reload_extension_ack", ok: true, deferred: true });
      }
      return;
    }
    if (type === "reload_extension") {
      sendWsJson({ type: "reload_extension_ack", ok: true });
    } else if (busyRequestsByTab.size > 0) {
      pendingIdleExtensionReload = true;
      return;
    }
    if (typeof chrome.runtime?.reload === "function") {
      setTimeout(() => {
        try {
          chrome.runtime.reload();
        } catch (_) {}
      }, 120);
    }
    return;
  }

  if (type === "spawn_tab") {
    const spawnId = msg.spawn_id || "";
    try {
      const spawned = await spawnParallelWorkerTab(
        msg.base_url || "",
        Boolean(msg.preserve_session_url)
      );
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

  if (type === "reset_tab") {
    const resetId = msg.reset_id || "";
    try {
      const rawTab = await findTargetVertexTab(msg.tab_id);
      if (!rawTab || !rawTab.id) {
        throw new Error("未找到对应的 Google 工作标签页");
      }
      const tab = await prepareTabForBackgroundExecution(rawTab);
      const ok = await resetTabChatWithFallback(tab);
      await reportTabStatusToBackend();
      sendWsJson({
        type: "reset_tab_result",
        reset_id: resetId,
        ok: Boolean(ok),
        tab_id: tab.id
      });
    } catch (err) {
      sendWsJson({
        type: "reset_tab_result",
        reset_id: resetId,
        ok: false,
        error: err.message
      });
    }
    return;
  }

  if (type === "open_session") {
    const openId = msg.open_id || "";
    const sessionUrl = String(msg.session_url || "").trim();
    const targetSid = extractVertexSessionIdFromUrl(sessionUrl);
    try {
      if (!sessionUrl || !sessionUrl.startsWith("https://vertexaisearch.cloud.google.com") || !targetSid) {
        throw new Error("无效的历史会话地址");
      }
      const rawTab = await findTargetVertexTab(msg.tab_id);
      if (!rawTab || !rawTab.id) {
        throw new Error("未找到对应的 Google 工作标签页");
      }
      const tab = await prepareTabForBackgroundExecution(rawTab);
      const currentSid = extractVertexSessionIdFromUrl(tab.url || "");
      if (currentSid !== targetSid) {
        const loadPromise = waitForTabComplete(tab.id, 14000);
        await chrome.tabs.update(tab.id, { url: sessionUrl });
        await loadPromise;
        await new Promise((r) => setTimeout(r, 450));
      }
      await ensureTabBridgeReady(tab.id);
      const readyRes = await waitForSessionPageReady(tab.id, targetSid, 12000);
      if (readyRes.active_model) {
        const prevMeta = runtimeTabMeta.get(tab.id) || {};
        runtimeTabMeta.set(tab.id, { ...prevMeta, activeModel: readyRes.active_model });
      }
      await reportTabStatusToBackend();
      sendWsJson({
        type: "open_session_result",
        open_id: openId,
        ok: Boolean(readyRes.ok),
        tab_id: tab.id,
        tab_url: readyRes.tab_url || "",
        tab_title: readyRes.tab_title || "",
        active_model: readyRes.active_model || "",
        error: readyRes.ok ? undefined : (readyRes.error || "历史会话未能打开或已被重定向")
      });
    } catch (err) {
      await reportTabStatusToBackend();
      sendWsJson({
        type: "open_session_result",
        open_id: openId,
        ok: false,
        error: err && err.message ? err.message : String(err)
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
    if (preparingRequests.has(requestId) || busyRequestsByTab.has(requestId)) return;
    const preparation = { cancelled: false };
    preparingRequests.set(requestId, preparation);
    let tabResetDone = false;
    try {
      let assembledPrompt = String(msg.prompt || "");
      if (msg.chunked) {
        const entry = swPromptReassembly.get(requestId);
        swPromptReassembly.delete(requestId);
        const total = msg.total_chunks || (entry ? entry.total : 0);
        assembledPrompt = assembleChunks(entry, total);
      }

      const rawTab = await findTargetVertexTab(msg.tab_id);
      if (preparation.cancelled) return;
      if (!rawTab || !rawTab.id) {
        sendWsJson({
          type: "request_error",
          request_id: requestId,
          submitted: false,
          tab_reset: false,
          error: "未找到已打开的 https://vertexaisearch.cloud.google.com 标签页。"
        });
        return;
      }
      const conflictingReqIds = [];
      for (const [existingReqId, existingTabId] of busyRequestsByTab.entries()) {
        if (existingTabId === rawTab.id && existingReqId !== requestId) {
          conflictingReqIds.push(existingReqId);
        }
      }
      if (conflictingReqIds.length > 0) {
        const allConfirmedOrphans =
          backendActiveRequestIdsSynced &&
          conflictingReqIds.every(
            (oldId) => !preparingRequests.has(oldId) && !backendActiveRequestIds.has(oldId)
          );
        if (!allConfirmedOrphans) {
          throw new Error("该 Google 标签页仍在处理上一项请求");
        }
        for (const oldId of conflictingReqIds) {
          deleteBusyRequest(oldId);
          try {
            await chrome.tabs.sendMessage(rawTab.id, {
              type: "GEB_CANCEL_REQUEST",
              request_id: oldId
            });
          } catch (_) {}
        }
        await new Promise((r) => setTimeout(r, 180));
      }

      setBusyRequestForTab(requestId, rawTab.id);
      backendActiveRequestIds.add(requestId);
      sendWsJson({ type: "request_ack", request_id: requestId, tab_id: rawTab.id });
      void broadcastTabTakeoverStates();

      const tab = await prepareTabForBackgroundExecution(rawTab);

      await appendLog(
        "info",
        `Dispatching request ${String(requestId).slice(0, 8)} to tab #${tab.id}` +
          ` [len=${assembledPrompt.length}]` +
          (tab.active ? "" : " [bg]") +
          (msg.target_model ? ` [model=${msg.target_model}]` : "") +
          (msg.reset_chat ? " [new_chat]" : "")
      );

      if (preparation.cancelled) return;
      await ensureTabBridgeReady(tab.id);
      if (preparation.cancelled) return;

      let remainingResetChat = Boolean(msg.reset_chat);
      if (remainingResetChat) {
        const resetDone = await resetTabChatWithFallback(tab);
        if (resetDone) {
          tabResetDone = true;
          remainingResetChat = false;
        }
      }

      if (preparation.cancelled) return;
      await ensureTabBridgeReady(tab.id);
      const settings = await getSettings();
      if (preparation.cancelled) return;
      const callbackUrl = msg.callback_url || deriveCallbackUrlFromWs(settings.wsUrl);

      const dispatchToReadyTab = async () => {
        const tabAttachments = await prepareAttachmentsForTab(tab.id, requestId, msg.attachments || []);
        if (assembledPrompt.length > TAB_IPC_CHUNK_SIZE) {
          const totalChunks = Math.ceil(assembledPrompt.length / TAB_IPC_CHUNK_SIZE);
          for (let i = 0; i < totalChunks; i += 1) {
            if (preparation.cancelled) return;
            await chrome.tabs.sendMessage(tab.id, {
              type: "GEB_PROMPT_CHUNK",
              request_id: requestId,
              chunk_index: i,
              total_chunks: totalChunks,
              data: assembledPrompt.slice(i * TAB_IPC_CHUNK_SIZE, (i + 1) * TAB_IPC_CHUNK_SIZE)
            });
          }
          if (preparation.cancelled) return;
          await chrome.tabs.sendMessage(tab.id, {
            type: "GEB_EXECUTE_PROMPT",
            request_id: requestId,
            prompt: "",
            chunked: true,
            total_chunks: totalChunks,
            callback_url: callbackUrl,
            attachments: tabAttachments,
            image_generation: Boolean(msg.image_generation),
            video_generation: Boolean(msg.video_generation),
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
          if (preparation.cancelled) return;
          await chrome.tabs.sendMessage(tab.id, {
            type: "GEB_EXECUTE_PROMPT",
            request_id: requestId,
            prompt: assembledPrompt,
            chunked: false,
            total_chunks: 0,
            callback_url: callbackUrl,
            attachments: tabAttachments,
            image_generation: Boolean(msg.image_generation),
            video_generation: Boolean(msg.video_generation),
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

      await dispatchToReadyTab();
    } catch (err) {
      if (deleteBusyRequest(requestId)) {
        void broadcastTabTakeoverStates();
      }
      if (!preparation.cancelled) {
        const errPacket = {
          type: "request_error",
          request_id: requestId,
          submitted: false,
          tab_reset: tabResetDone,
          error: "Gemini 工作页面恢复或发送失败: " + err.message
        };
        if (!sendWsJson(errPacket)) {
          await postCallbackFallback(errPacket);
        }
      }
      maybePerformIdleExtensionReload();
    } finally {
      preparingRequests.delete(requestId);
      if (preparation.cancelled && deleteBusyRequest(requestId)) {
        void broadcastTabTakeoverStates();
        maybePerformIdleExtensionReload();
      }
    }
    return;
  }

  if (type === "cancel_request") {
    const requestId = msg.request_id;
    if (!requestId) return;
    swPromptReassembly.delete(requestId);
    backendActiveRequestIds.delete(requestId);
    const preparation = preparingRequests.get(requestId);
    if (preparation) preparation.cancelled = true;
    const tabId = busyRequestsByTab.get(requestId) || Number(msg.tab_id);
    if (tabId) {
      try {
        await chrome.tabs.sendMessage(tabId, {
          type: "GEB_CANCEL_REQUEST",
          request_id: requestId
        });
      } catch (_) {}
    }
    if (!preparation && deleteBusyRequest(requestId)) {
      void broadcastTabTakeoverStates();
    }
    maybePerformIdleExtensionReload();
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
  if (changeInfo.status === "loading") injectedTabVersions.delete(tabId);
  if (changeInfo.status === "complete" && tab.url && isUsableVertexTabUrl(tab.url)) {
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
  managedTabOrder.delete(tabId);
  pendingManualTabIds.delete(tabId);
  for (const [reqId, tid] of busyRequestsByTab) {
    if (tid === tabId) busyRequestsByTab.delete(reqId);
  }
  void (async () => {
    try {
      const { manualTabIds } = await getManualExclusions();
      if (manualTabIds.delete(tabId)) {
        await chrome.storage.local.set({ manualTabIds: Array.from(manualTabIds) });
      }
    } catch (_) {}
    await reportTabStatusToBackend();
  })();
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
      if (sender && sender.tab) {
        if (!eventData.tab_url && sender.tab.url) {
          eventData.tab_url = sender.tab.url;
        }
        if (!eventData.tab_title && sender.tab.title) {
          eventData.tab_title = sender.tab.title;
        }
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
        if (Array.isArray(eventData.available_models) && eventData.available_models.length > 0) {
          nextMeta.availableModels = eventData.available_models.slice(0, 64);
          const { availableModels: prevAvail = [] } = await chrome.storage.session.get(["availableModels"]);
          const merged = Array.isArray(prevAvail) ? prevAvail.slice() : [];
          const seenLow = new Set(merged.map((m) => String(m).toLowerCase()));
          for (const rawItem of eventData.available_models) {
            const lbl = String(rawItem || "").trim();
            if (lbl && !seenLow.has(lbl.toLowerCase())) {
              merged.push(lbl);
              seenLow.add(lbl.toLowerCase());
            }
          }
          await chrome.storage.session.set({ availableModels: merged.slice(-64) });
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
        const rkey = eventData.stream_key || eventData.request_id;
        let entry = swReplyReassembly.get(rkey);
        if (!entry) {
          entry = { parts: [], total: eventData.total_chunks || 0 };
          swReplyReassembly.set(rkey, entry);
        }
        entry.parts[eventData.chunk_index] = eventData.data || "";
        if (eventData.total_chunks) entry.total = eventData.total_chunks;
      } else {
        // stream_chunk | request_complete | request_error
        if (eventData.type === "request_complete" && eventData.json_envelope_chunked) {
          const rkey = eventData.stream_key || (eventData.request_id + ":json");
          const entry = swReplyReassembly.get(rkey);
          swReplyReassembly.delete(rkey);
          const total = eventData.total_chunks || (entry ? entry.total : 0);
          const mergedJson = assembleChunks(entry, total);
          const unpacked = JSON.parse(mergedJson);
          if (senderTabId !== null && !unpacked.tab_id) unpacked.tab_id = senderTabId;
          if (sender && sender.tab) {
            if (!unpacked.tab_url && sender.tab.url) unpacked.tab_url = sender.tab.url;
            if (!unpacked.tab_title && sender.tab.title) unpacked.tab_title = sender.tab.title;
          }
          await deliverResultToBackend(unpacked);
        } else if (eventData.type === "request_complete" && eventData.chunked) {
          const entry = swReplyReassembly.get(eventData.request_id);
          swReplyReassembly.delete(eventData.request_id);
          const total = eventData.total_chunks || (entry ? entry.total : 0);
          const merged = assembleChunks(entry, total);
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
      const cls = await classifyAllVertexTabs();
      const settings = cls.settings;
      const tab = cls.managedTabs[0] || null;
      const session = await chrome.storage.session.get([
        "wsConnected",
        "lastError",
        "capturedRpc",
        "activeModel",
        "logs"
      ]);
      let focusedTabInfo = null;
      try {
        const activeTabs = await chrome.tabs.query({ active: true, currentWindow: true });
        const fTab = activeTabs && activeTabs[0] ? activeTabs[0] : null;
        if (fTab && fTab.id) {
          const isVertex = isUsableVertexTabUrl(fTab.url || "");
          const tState = isVertex ? cls.perTabState.get(fTab.id) || null : null;
          focusedTabInfo = {
            id: fTab.id,
            url: fTab.url || "",
            title: fTab.title || "",
            isVertex,
            state: tState
          };
        }
      } catch (_) {}

      sendResponse({
        ok: true,
        settings,
        wsConnected: cls.wsConnected,
        lastError: session.lastError || "",
        capturedRpc: Boolean(session.capturedRpc),
        activeModel: session.activeModel || "",
        preferredTabId: null,
        focusedTab: focusedTabInfo,
        poolSummary: {
          totalVertexTabs: cls.allTabs.length,
          managedCount: cls.managedTabs.length,
          manualCount: cls.manualTabs.length,
          overflowCount: cls.overflowTabs.length,
          busyCount: cls.busyCount,
          maxTabs: cls.maxTabs
        },
        activeTab: tab
          ? { id: tab.id, url: tab.url || "", title: tab.title || "" }
          : null,
        vertexTabs: cls.allTabs.map((t) => ({
          id: t.id,
          url: t.url || "",
          title: t.title || "",
          active: Boolean(t.active),
          state: cls.perTabState.get(t.id) || null
        })),
        logs: session.logs || []
      });
      return;
    }

    if (msgType === "GEB_GET_MY_TAKEOVER_STATE") {
      const senderTabId = sender && sender.tab && sender.tab.id ? sender.tab.id : message.tabId;
      if (!senderTabId) {
        sendResponse({ ok: false });
        return;
      }
      const cls = await classifyAllVertexTabs();
      sendResponse({
        ok: true,
        state: cls.perTabState.get(Number(senderTabId)) || null
      });
      return;
    }

    if (msgType === "GEB_TOGGLE_TAB_MANUAL") {
      const targetTabId =
        message.tabId !== undefined && message.tabId !== null
          ? Number(message.tabId)
          : sender && sender.tab && sender.tab.id
            ? sender.tab.id
            : null;
      if (!targetTabId) {
        sendResponse({ ok: false, error: "未找到目标标签页" });
        return;
      }
      const manual = await setTabManualState(
        targetTabId,
        typeof message.manual === "boolean" ? message.manual : null
      );
      const cls = await classifyAllVertexTabs();
      sendResponse({
        ok: true,
        manual,
        state: cls.perTabState.get(targetTabId) || null
      });
      return;
    }

    if (
      msgType === "GEB_POPUP_OPEN_MANUAL_TAB" ||
      msgType === "GEB_POPUP_OPEN_VERTEX_TAB" ||
      msgType === "GEB_POPUP_OPEN_CLEAN_TAB"
    ) {
      const wantManual =
        msgType === "GEB_POPUP_OPEN_MANUAL_TAB" ||
        (msgType === "GEB_POPUP_OPEN_CLEAN_TAB" && Boolean(message.manual));
      const cls = await classifyAllVertexTabs();
      const targetUrl = await resolvePreferredVertexHomeUrl(cls.allTabs, message.url || "");
      const created = await chrome.tabs.create({ url: targetUrl, active: true });
      if (wantManual && created && created.id) {
        pendingManualTabIds.add(created.id);
        managedTabOrder.delete(created.id);
        await setTabManualState(created.id, true);
        waitForTabComplete(created.id, 14000)
          .then(() => ensureScriptsInjected(created.id))
          .then(() => reportTabStatusToBackend())
          .catch(() => {});
      }
      sendResponse({
        ok: true,
        handled: true,
        manual: wantManual,
        tabId: created ? created.id : null,
        url: targetUrl
      });
      return;
    }

    if (msgType === "GEB_POPUP_SAVE_SETTINGS") {
      const patch = { ...(message.settings || {}) };
      if (patch.maxParallelTabs !== undefined) {
        patch.maxParallelTabs = normalizeMaxParallelTabs(patch.maxParallelTabs, 10);
      }
      const current = await getSettings();
      const updated = { ...current, ...patch };
      await chrome.storage.local.set(updated);
      if (message.preferredTabId !== undefined) {
        await chrome.storage.local.set({ preferredTabId: message.preferredTabId });
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
  })().catch(async (error) => {
    if (msgType === "GEB_DRIVER_EVENT" && message.payload && message.payload.request_id) {
      await deliverResultToBackend({type: "request_error", request_id: message.payload.request_id,
        tab_id: sender && sender.tab ? sender.tab.id : undefined, error: error.message}).catch(() => {});
    }
    sendResponse({ok: false, error: error.message});
  });
  return true;
});

// Kick off connection check whenever the Service Worker wakes up
void (async () => {
  await setupAlarms();
  await ensureWebSocketConnected(false);
})();
