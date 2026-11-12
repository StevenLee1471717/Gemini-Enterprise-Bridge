// Popup controller for Gemini Enterprise Web-to-API Bridge (MV3 compliant, async/await)

const elEnabled = document.getElementById("enabledToggle");
const elWsPill = document.getElementById("wsStatusPill");
const elTabStatus = document.getElementById("tabStatusText");
const elRpcStatus = document.getElementById("rpcStatusText");
const elErrorBanner = document.getElementById("errorBanner");
const elTabSelect = document.getElementById("tabSelect");
const elAutoSpawn = document.getElementById("autoSpawnSelect");
const elMaxTabs = document.getElementById("maxTabsSelect");
const elModeSelect = document.getElementById("driverModeSelect");
const elWsUrl = document.getElementById("wsUrlInput");
const elModelSel = document.getElementById("modelSelector");
const elInputSel = document.getElementById("inputSelector");
const elSendSel = document.getElementById("sendSelector");
const elReplySel = document.getElementById("replySelector");
const elDiagOut = document.getElementById("diagOutput");

let initialFormHydrated = false;

function formatLogs(logs) {
  if (!Array.isArray(logs) || logs.length === 0) {
    return "暂无日志。打开 vertexaisearch.cloud.google.com 并启动 server.py 即可开始。";
  }
  return logs
    .slice()
    .reverse()
    .map((item) => {
      const t = new Date(item.ts).toLocaleTimeString();
      return `[${t}] [${String(item.level || "info").toUpperCase()}] ${item.message}`;
    })
    .join("\n");
}

function deriveHttpConsoleUrl(wsUrl) {
  try {
    const u = new URL(wsUrl || "ws://127.0.0.1:8000/ws/browser");
    const proto = u.protocol === "wss:" ? "https:" : "http:";
    return `${proto}//${u.host}/`;
  } catch (_) {
    return "http://127.0.0.1:8000/";
  }
}

async function loadState(overwriteInputs = false) {
  try {
    const state = await chrome.runtime.sendMessage({ type: "GEB_POPUP_GET_STATE" });
    if (!state || !state.ok) {
      return;
    }

    const { settings, wsConnected, lastError, capturedRpc, activeModel, activeTab, vertexTabs, preferredTabId, logs } = state;

    if (!initialFormHydrated || overwriteInputs) {
      elEnabled.checked = Boolean(settings.enabled);
      elModeSelect.value = settings.driverMode || "auto";
      if (elAutoSpawn) elAutoSpawn.value = settings.autoSpawnTabs === false ? "false" : "true";
      if (elMaxTabs) elMaxTabs.value = String(settings.maxParallelTabs || 4);
      elWsUrl.value = settings.wsUrl || "ws://127.0.0.1:8000/ws/browser";
      if (elModelSel) elModelSel.value = settings.customModelSelector || "";
      elInputSel.value = settings.customInputSelector || "";
      elSendSel.value = settings.customSendSelector || "";
      elReplySel.value = settings.customReplySelector || "";
      initialFormHydrated = true;
    }

    // Populate open Vertex AI Search tabs dropdown (Trusted-Types safe)
    const currentSelected = preferredTabId ? String(preferredTabId) : "";
    elTabSelect.replaceChildren();
    const autoOpt = document.createElement("option");
    autoOpt.value = "";
    autoOpt.textContent = `多标签页并行池（已挂载 ${(vertexTabs || []).length} 个标签页）`;
    elTabSelect.appendChild(autoOpt);

    for (const t of vertexTabs || []) {
      const opt = document.createElement("option");
      opt.value = String(t.id);
      const shortTitle = (t.title || "Gemini Enterprise").slice(0, 26);
      const acctMatch = (t.url || "").match(/\/u\/(\d+)\//);
      const acctTag = acctMatch ? ` [/u/${acctMatch[1]}]` : "";
      opt.textContent = `仅固定 #${t.id}${acctTag} ${shortTitle}`;
      if (String(t.id) === currentSelected) {
        opt.selected = true;
      }
      elTabSelect.appendChild(opt);
    }

    if (!settings.enabled) {
      elWsPill.textContent = "已暂停";
      elWsPill.className = "pill err";
    } else if (wsConnected) {
      elWsPill.textContent = "已连接 (WS)";
      elWsPill.className = "pill ok";
    } else {
      elWsPill.textContent = "未连接后端";
      elWsPill.className = "pill err";
    }

    const tabTotal = (vertexTabs || []).length;
    if (preferredTabId && activeTab) {
      elTabStatus.textContent = `固定单页 #${activeTab.id} · ${activeTab.title || activeTab.url}`;
      elTabStatus.title = activeTab.url;
    } else if (tabTotal > 0) {
      elTabStatus.textContent = `${tabTotal} 个工作页就绪 (上限 ${settings.maxParallelTabs || 4})`;
      elTabStatus.title = (vertexTabs || []).map((t) => `#${t.id}: ${t.url}`).join("\n");
    } else {
      elTabStatus.textContent = "未打开 vertexaisearch.cloud.google.com";
      elTabStatus.title = "";
    }

    const modelBadge = activeModel ? ` · ${activeModel}` : "";
    elRpcStatus.textContent =
      (capturedRpc ? "已捕获 RPC" : "流监听 + DOM") + modelBadge;

    if (lastError && !wsConnected && settings.enabled) {
      elErrorBanner.hidden = false;
      elErrorBanner.textContent = lastError;
    } else {
      elErrorBanner.hidden = true;
      elErrorBanner.textContent = "";
    }

    if (!elDiagOut.dataset.showingDiag) {
      elDiagOut.textContent = formatLogs(logs);
    }
  } catch (err) {
    elWsPill.textContent = "通信错误";
    elWsPill.className = "pill err";
    elDiagOut.textContent = "读取扩展状态失败: " + err.message;
  }
}

async function saveSettings() {
  const chosenTabVal = elTabSelect.value;
  const preferredTabId = chosenTabVal ? Number(chosenTabVal) : null;
  const settings = {
    enabled: elEnabled.checked,
    driverMode: elModeSelect.value,
    autoSpawnTabs: elAutoSpawn ? elAutoSpawn.value !== "false" : true,
    maxParallelTabs: elMaxTabs ? Number(elMaxTabs.value) || 4 : 4,
    wsUrl: elWsUrl.value.trim() || "ws://127.0.0.1:8000/ws/browser",
    customModelSelector: elModelSel ? elModelSel.value.trim() : "",
    customInputSelector: elInputSel.value.trim(),
    customSendSelector: elSendSel.value.trim(),
    customReplySelector: elReplySel.value.trim()
  };

  delete elDiagOut.dataset.showingDiag;
  await chrome.runtime.sendMessage({
    type: "GEB_POPUP_SAVE_SETTINGS",
    settings,
    preferredTabId
  });
  await chrome.runtime.sendMessage({ type: "GEB_POPUP_RECONNECT" });
  await loadState(true);
}

async function runPageDiagnosis() {
  elDiagOut.dataset.showingDiag = "1";
  elDiagOut.textContent = "正在扫描页面 Shadow DOM 与 RPC 状态...";
  const res = await chrome.runtime.sendMessage({ type: "GEB_POPUP_DIAGNOSE_TAB" });
  if (!res || !res.ok) {
    elDiagOut.textContent =
      "诊断失败: " + ((res && res.error) || "请先打开 https://vertexaisearch.cloud.google.com");
    return;
  }
  elDiagOut.textContent = JSON.stringify(res.diagnosis, null, 2);
}

async function triggerNewChat() {
  elDiagOut.dataset.showingDiag = "1";
  const res = await chrome.runtime.sendMessage({ type: "GEB_POPUP_NEW_CHAT" });
  elDiagOut.textContent = JSON.stringify(res, null, 2);
}

document.getElementById("saveBtn").addEventListener("click", () => {
  void saveSettings();
});

elEnabled.addEventListener("change", () => {
  void saveSettings();
});

const spawnBtn = document.getElementById("spawnTabBtn");
if (spawnBtn) {
  spawnBtn.addEventListener("click", () => {
    void (async () => {
      elDiagOut.dataset.showingDiag = "1";
      elDiagOut.textContent = "正在后台静默创建新的 Gemini Enterprise 并行工作标签页...";
      const res = await chrome.runtime.sendMessage({ type: "GEB_POPUP_SPAWN_TAB" });
      elDiagOut.textContent = JSON.stringify(res, null, 2);
      await loadState(false);
    })();
  });
}

document.getElementById("diagnoseBtn").addEventListener("click", () => {
  void runPageDiagnosis();
});

document.getElementById("newChatBtn").addEventListener("click", () => {
  void triggerNewChat();
});

document.getElementById("openConsoleBtn").addEventListener("click", () => {
  void (async () => {
    const url = deriveHttpConsoleUrl(elWsUrl.value);
    await chrome.tabs.create({ url });
  })();
});

document.getElementById("clearLogsBtn").addEventListener("click", () => {
  void (async () => {
    delete elDiagOut.dataset.showingDiag;
    await chrome.runtime.sendMessage({ type: "GEB_POPUP_CLEAR_LOGS" });
    await loadState(false);
  })();
});

void loadState(true);
setInterval(() => {
  void loadState(false);
}, 2500);
