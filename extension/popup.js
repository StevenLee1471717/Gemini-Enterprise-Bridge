// Popup controller for Gemini Enterprise Bridge (MV3 compliant)

if (
  typeof window !== "undefined" &&
  typeof URLSearchParams === "function" &&
  new URLSearchParams(window.location?.search || "").get("geb_reload") === "1" &&
  typeof chrome !== "undefined" &&
  typeof chrome.runtime?.reload === "function"
) {
  chrome.runtime.reload();
}

const elEnabled = document.getElementById("enabledToggle");
const elHeroDot = document.getElementById("heroDot");
const elHeroTitle = document.getElementById("heroTitle");
const elHeroSub = document.getElementById("heroSub");
const elWsPill = document.getElementById("wsStatusPill");
const elTabStatus = document.getElementById("tabStatusText");
const elRpcStatus = document.getElementById("rpcStatusText");
const elOpenVertexBtn = document.getElementById("openVertexBtn");
const elErrorBanner = document.getElementById("errorBanner");
const elTabSelect = document.getElementById("tabSelect");
const elCurTabPanel = document.getElementById("curTabPanel");
const elCurTabTitle = document.getElementById("curTabTitle");
const elCurTabBadge = document.getElementById("curTabBadge");
const elCurTabDesc = document.getElementById("curTabDesc");
const elToggleManualBtn = document.getElementById("toggleManualBtn");
const elOpenManualTabBtn = document.getElementById("openManualTabBtn");
const elAutoSpawn = document.getElementById("autoSpawnSelect");
const elAutoSpawnCheck = document.getElementById("autoSpawnCheck");
const elMaxTabs = document.getElementById("maxTabsSelect");
const elMaxTabsSegments = document.getElementById("maxTabsSegments");
const elCustomTabsRow = document.getElementById("customTabsRow");
const elCustomTabsInput = document.getElementById("customTabsInput");
const elCustomTabsApplyBtn = document.getElementById("customTabsApplyBtn");
const elPoolControlsWrap = document.getElementById("poolControlsWrap");
const elModeSelect = document.getElementById("driverModeSelect");
const elWsUrl = document.getElementById("wsUrlInput");
const elModelSel = document.getElementById("modelSelector");
const elInputSel = document.getElementById("inputSelector");
const elSendSel = document.getElementById("sendSelector");
const elReplySel = document.getElementById("replySelector");
const elDiagOut = document.getElementById("diagOutput");
const elSaveToast = document.getElementById("saveToast");
let customCapEditing = false;

const POPUP_DEFAULT_VERTEX_HOME_URL =
  "https://vertexaisearch.cloud.google.com/home/cid/f8a26a7c-0f51-4098-bf58-52506661472b?hl=en_US";

function hasPopupVertexWorkspacePath(rawOrPath) {
  return /\/(?:cid|app)\/[^/?#]+/i.test(String(rawOrPath || ""));
}

function normalizePopupVertexHomeUrl(rawUrl) {
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

async function resolvePopupVertexHomeUrl() {
  let tabs = [];
  try {
    tabs = (await chrome.tabs.query({ url: "https://vertexaisearch.cloud.google.com/*" })) || [];
  } catch (_) {}
  const ordered = tabs.filter((t) => t && t.active).concat(tabs.filter((t) => t && !t.active));
  for (const t of ordered) {
    const clean = normalizePopupVertexHomeUrl(t.url || "");
    if (clean && hasPopupVertexWorkspacePath(clean)) {
      try {
        await chrome.storage.local.set({ lastVertexHomeUrl: clean });
      } catch (_) {}
      return clean;
    }
  }
  let storedClean = "";
  try {
    const data = await chrome.storage.local.get("lastVertexHomeUrl");
    storedClean = normalizePopupVertexHomeUrl(data && data.lastVertexHomeUrl ? data.lastVertexHomeUrl : "");
    if (storedClean && hasPopupVertexWorkspacePath(storedClean)) {
      return storedClean;
    }
  } catch (_) {}
  for (const t of ordered) {
    const clean = normalizePopupVertexHomeUrl(t.url || "");
    if (clean) return clean;
  }
  return storedClean || POPUP_DEFAULT_VERTEX_HOME_URL;
}

async function openCleanGeminiTab({ manual = true, active = true } = {}) {
  const targetUrl = await resolvePopupVertexHomeUrl();
  if (active) {
    const res = await chrome.runtime.sendMessage({
      type: "GEB_POPUP_OPEN_CLEAN_TAB",
      manual,
      url: targetUrl
    });
    if (res && res.handled) {
      return res;
    }
  }
  const created = await chrome.tabs.create({ url: targetUrl, active });
  if (manual && created && created.id) {
    await chrome.runtime.sendMessage({
      type: "GEB_TOGGLE_TAB_MANUAL",
      tabId: created.id,
      manual: true
    });
  }
  return { ok: true, tabId: created ? created.id : null, url: targetUrl };
}

let initialFormHydrated = false;
let toastTimer = null;

function flashSavedToast() {
  if (!elSaveToast) return;
  elSaveToast.hidden = false;
  if (toastTimer) clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    elSaveToast.hidden = true;
  }, 1500);
}

function friendlyModelName(raw) {
  const str = String(raw || "").trim();
  const s = str.toLowerCase();
  if (!s) return "Carbon (默认)";
  if (s.includes("hydrogen")) return "Hydrogen";
  if (s.includes("carbon")) return "Carbon";
  if (s.includes("argon")) return "Argon";
  if (s.includes("3.1")) return "3.1 Pro";
  if (s.includes("3.8")) return "3.8 Flash";
  if (s.includes("2.5") && s.includes("pro")) return "2.5 Pro";
  if (s.includes("2.5") && s.includes("flash")) return "2.5 Flash";
  const stripped = str.replace(/^(?:gemini-web:|web:|gemini-)/i, "").trim();
  if (stripped && stripped !== str) {
    return stripped
      .split(/[\s_\-]+/)
      .filter(Boolean)
      .map((p) => (/^\d/.test(p) ? p : p.charAt(0).toUpperCase() + p.slice(1)))
      .join(" ");
  }
  return str;
}

function cleanTabTitle(rawTitle, index) {
  const text = String(rawTitle || "")
    .replace(/\s*[-·|]\s*Gemini Enterprise\s*$/i, "")
    .trim();
  const low = text.toLowerCase();
  if (
    !text ||
    low === "gemini enterprise" ||
    low === "new chat" ||
    low.startsWith("[system instructions]") ||
    low.includes("you are codex") ||
    low.includes("original_stream_") ||
    low.startsWith("tool_call") ||
    low.includes("stream disconnected") ||
    text.length > 42
  ) {
    return `${index + 1} 号工作页`;
  }
  return `${index + 1} 号页 · ${text.slice(0, 16)}`;
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

function parseCustomParallelTabsInput(raw) {
  const trimmed = String(raw ?? "").trim();
  if (!/^\d+$/.test(trimmed)) return null;
  const parsed = Number(trimmed);
  if (!Number.isInteger(parsed) || parsed < 1 || parsed > 200) return null;
  return parsed;
}

function ensureSelectHasValue(selectEl, valStr) {
  if (!selectEl) return;
  let opt = selectEl.querySelector(`option[value="${valStr}"]`);
  if (!opt) {
    opt = document.createElement("option");
    opt.value = valStr;
    opt.textContent = valStr;
    selectEl.appendChild(opt);
  }
  selectEl.value = valStr;
}

function syncSegmentUI(maxTabsVal, autoSpawnEnabled, isFixedSingleTab) {
  if (elAutoSpawnCheck) {
    elAutoSpawnCheck.checked = Boolean(autoSpawnEnabled);
    elAutoSpawnCheck.disabled = Boolean(isFixedSingleTab);
  }
  if (elPoolControlsWrap) {
    elPoolControlsWrap.classList.toggle("disabled", Boolean(isFixedSingleTab));
  }
  if (elMaxTabsSegments) {
    const buttons = elMaxTabsSegments.querySelectorAll(".seg-btn");
    const normalizedNum = normalizeMaxParallelTabs(maxTabsVal, 10);
    const target = String(normalizedNum);
    const isPreset = target === "1" || target === "5" || target === "10";
    const editingCustom = typeof customCapEditing !== "undefined" && Boolean(customCapEditing);
    buttons.forEach((btn) => {
      const dVal = btn.getAttribute("data-val");
      if (dVal === "custom") {
        btn.classList.toggle("active", editingCustom || !isPreset);
        btn.textContent = !isPreset ? `自定义 (${normalizedNum})` : "自定义";
      } else {
        btn.classList.toggle("active", !editingCustom && dVal === target);
      }
      btn.disabled = Boolean(isFixedSingleTab);
    });
    if (typeof elCustomTabsRow !== "undefined" && elCustomTabsRow) {
      elCustomTabsRow.hidden = !editingCustom || Boolean(isFixedSingleTab);
    }
  }
}

function renderCurrentTabControl(focusedTab, settings, maxCap) {
  if (!elCurTabPanel || !elCurTabBadge || !elCurTabDesc || !elToggleManualBtn) {
    return;
  }
  const isUsableFocused = Boolean(
    focusedTab &&
    focusedTab.isVertex &&
    focusedTab.id &&
    (!focusedTab.url || normalizePopupVertexHomeUrl(focusedTab.url))
  );
  if (isUsableFocused) {
    const st = focusedTab.state || {};
    if (elOpenManualTabBtn) elOpenManualTabBtn.hidden = false;
    if (elCurTabTitle) {
      elCurTabTitle.textContent = st.seatIndex
        ? `当前标签页（席位 #${st.seatIndex}）`
        : "当前 Gemini 标签页";
    }
    elToggleManualBtn.dataset.action = "toggle";
    elToggleManualBtn.dataset.tabId = String(focusedTab.id);

    if (st.manual) {
      elCurTabPanel.className = "cur-tab-panel is-manual";
      elCurTabBadge.textContent = "手动自用 · 免接管";
      elCurTabBadge.className = "pill warn";
      elCurTabDesc.textContent = "此页面与当前会话已锁定为你手动操作，Bridge 不会接管或清空。";
      elToggleManualBtn.textContent = "恢复交给 Bridge 自动接管";
      elToggleManualBtn.className = "btn manual-toggle-btn to-bridge";
    } else if (st.overflow) {
      elCurTabPanel.className = "cur-tab-panel";
      elCurTabBadge.textContent = "超出上限 · 未接管";
      elCurTabBadge.className = "pill";
      elCurTabDesc.textContent =
        maxCap > 0
          ? `已达 ${maxCap} 页接管上限，此页暂未接入；也可锁定为你的专属自用页。`
          : "此页暂未接入；也可锁定为你的专属自用页。";
      elToggleManualBtn.textContent = "设为我自己用 · 不接管此会话";
      elToggleManualBtn.className = "btn manual-toggle-btn to-manual";
    } else {
      elCurTabPanel.className = "cur-tab-panel is-managed";
      if (!settings.enabled) {
        elCurTabBadge.textContent = "桥接已暂停";
        elCurTabBadge.className = "pill";
      } else if (st.busy) {
        elCurTabBadge.textContent = "Bridge 工作中";
        elCurTabBadge.className = "pill ok";
      } else {
        elCurTabBadge.textContent = "Bridge 已接管";
        elCurTabBadge.className = "pill ok";
      }
      elCurTabDesc.textContent = "此页面已接入 Bridge 工作池；如需自己手动聊天，点击下方按钮即可免除接管。";
      elToggleManualBtn.textContent = "设为我自己用 · 不接管此会话";
      elToggleManualBtn.className = "btn manual-toggle-btn to-manual";
    }
  } else {
    if (elOpenManualTabBtn) elOpenManualTabBtn.hidden = true;
    if (elCurTabTitle) elCurTabTitle.textContent = "当前浏览标签页";
    elCurTabPanel.className = "cur-tab-panel";
    elCurTabBadge.textContent = "非 Gemini 页";
    elCurTabBadge.className = "pill";
    elCurTabDesc.textContent = "切换到任意 Gemini 页面可随时免除接管，或直接新开一个免接管的自用页面：";
    elToggleManualBtn.textContent = "+ 新开自用 Gemini 聊天页（免接管）";
    elToggleManualBtn.className = "btn manual-toggle-btn to-bridge";
    elToggleManualBtn.dataset.action = "open_manual";
    elToggleManualBtn.dataset.tabId = "";
  }
}

function formatLogs(logs) {
  if (!Array.isArray(logs) || logs.length === 0) {
    return "暂无连接记录";
  }
  return logs
    .slice()
    .reverse()
    .map((item) => {
      const t = new Date(item.ts).toLocaleTimeString();
      return `[${t}] ${item.message}`;
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

    const {
      settings,
      wsConnected,
      lastError,
      vertexTabs,
      focusedTab,
      poolSummary,
      logs
    } = state;

    const maxCap = normalizeMaxParallelTabs(settings.maxParallelTabs, 10);
    const autoSpawnOn = settings.autoSpawnTabs !== false;

    if (elMaxTabs && (!customCapEditing || overwriteInputs)) {
      ensureSelectHasValue(elMaxTabs, String(maxCap));
    }
    if (!initialFormHydrated || overwriteInputs) {
      elEnabled.checked = Boolean(settings.enabled);
      elModeSelect.value = settings.driverMode || "auto";
      if (elAutoSpawn) elAutoSpawn.value = autoSpawnOn ? "true" : "false";
      if (elMaxTabs) ensureSelectHasValue(elMaxTabs, String(maxCap));
      elWsUrl.value = settings.wsUrl || "ws://127.0.0.1:8000/ws/browser";
      if (elModelSel) elModelSel.value = settings.customModelSelector || "";
      elInputSel.value = settings.customInputSelector || "";
      elSendSel.value = settings.customSendSelector || "";
      elReplySel.value = settings.customReplySelector || "";
      initialFormHydrated = true;
    }

    const tabList = Array.isArray(vertexTabs) ? vertexTabs : [];
    const summary = poolSummary || {
      totalVertexTabs: tabList.length,
      managedCount: Math.min(tabList.length, maxCap),
      manualCount: 0,
      overflowCount: Math.max(0, tabList.length - maxCap),
      busyCount: 0,
      maxTabs: maxCap
    };

    syncSegmentUI(
      elMaxTabs ? elMaxTabs.value : maxCap,
      elAutoSpawn ? elAutoSpawn.value !== "false" : autoSpawnOn,
      false
    );
    renderCurrentTabControl(focusedTab, settings, maxCap);

    const managedTotal = Number(summary.managedCount || 0);
    const manualTotal = Number(summary.manualCount || 0);
    const overflowTotal = Number(summary.overflowCount || 0);
    const busyTotal = Number(summary.busyCount || 0);
    const allOpenTotal = Number(summary.totalVertexTabs || tabList.length);

    elRpcStatus.textContent = manualTotal > 0 ? `${manualTotal} 页免接管` : "0 页";

    if (!settings.enabled) {
      if (elHeroDot) elHeroDot.className = "status-dot err";
      if (elHeroTitle) elHeroTitle.textContent = "桥接已暂停";
      if (elHeroSub) elHeroSub.textContent = "打开右上角开关即可恢复本地接口服务";
      elWsPill.textContent = "已暂停";
      elWsPill.className = "pill err";
      elTabStatus.textContent = allOpenTotal > 0 ? `已暂停 (${allOpenTotal}页)` : "未挂载";
      if (elOpenVertexBtn) elOpenVertexBtn.hidden = true;
    } else if (!wsConnected) {
      if (elHeroDot) elHeroDot.className = "status-dot err";
      if (elHeroTitle) elHeroTitle.textContent = "本地服务未连接";
      if (elHeroSub) elHeroSub.textContent = "请确认本机 8000 服务已启动，插件会自动重连";
      elWsPill.textContent = "未连接";
      elWsPill.className = "pill err";
      elTabStatus.textContent =
        managedTotal > 0 ? `${managedTotal} / ${maxCap} 页` : "0 页";
      if (elOpenVertexBtn) elOpenVertexBtn.hidden = allOpenTotal > 0;
    } else if (managedTotal === 0) {
      if (elHeroDot) elHeroDot.className = "status-dot";
      if (elHeroTitle) {
        elHeroTitle.textContent = manualTotal > 0
          ? "所有已开页面均设为手动自用"
          : "还差一步：未打开 Google 登录页";
      }
      if (elHeroSub) {
        elHeroSub.textContent = manualTotal > 0
          ? "如需恢复接口调用，可新开一个页面或将现有页恢复接管"
          : "本地服务已连通，打开并登录 Google 页面即自动接管";
      }
      elWsPill.textContent = "已在线";
      elWsPill.className = "pill ok";
      elTabStatus.textContent = `0 / ${maxCap} 页`;
      if (elOpenVertexBtn) elOpenVertexBtn.hidden = false;
    } else {
      if (elHeroDot) elHeroDot.className = "status-dot ok";
      elWsPill.textContent = "已在线";
      elWsPill.className = "pill ok";
      if (elOpenVertexBtn) elOpenVertexBtn.hidden = true;
      const busySuffix = busyTotal > 0 ? `（${busyTotal} 页工作中）` : "";
      if (elHeroTitle) {
        elHeroTitle.textContent = `运行正常 · 已接管 ${managedTotal} 页${busySuffix}`;
      }
      if (elHeroSub) {
        const extraParts = [];
        if (manualTotal > 0) extraParts.push(`${manualTotal} 页自用免接管`);
        if (overflowTotal > 0) extraParts.push(`${overflowTotal} 页超上限未接入`);
        if (extraParts.length > 0) {
          const capLabel = `接管上限 ${maxCap} 页`;
          elHeroSub.textContent = extraParts.join(" · ") + ` · ${capLabel}`;
        } else {
          const inlineCapLabel = `上限 ${maxCap} 页`;
          elHeroSub.textContent = autoSpawnOn
            ? `新开页面自动接入（${inlineCapLabel}），繁忙时可自动扩容`
            : `新开页面自动接入（${inlineCapLabel}），仅用已打开页面`;
        }
      }
      elTabStatus.textContent = `${managedTotal} / ${maxCap} 页`;
    }

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
    if (elHeroDot) elHeroDot.className = "status-dot err";
    if (elHeroTitle) elHeroTitle.textContent = "插件状态读取异常";
    elWsPill.textContent = "异常";
    elWsPill.className = "pill err";
    elDiagOut.textContent = "读取状态失败: " + err.message;
  }
}

async function saveSettings() {
  const chosenTabVal =
    typeof elTabSelect !== "undefined" && elTabSelect && elTabSelect.value
      ? elTabSelect.value
      : "";
  const preferredTabId = chosenTabVal ? Number(chosenTabVal) : null;
  const settings = {
    enabled: elEnabled.checked,
    driverMode: elModeSelect.value,
    autoSpawnTabs: elAutoSpawn ? elAutoSpawn.value !== "false" : true,
    maxParallelTabs: elMaxTabs ? normalizeMaxParallelTabs(elMaxTabs.value, 10) : 10,
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
  if (typeof flashSavedToast === "function") {
    flashSavedToast();
  }
  await loadState(true);
}

async function runPageDiagnosis() {
  elDiagOut.dataset.showingDiag = "1";
  elDiagOut.textContent = "正在检查当前页面结构...";
  const res = await chrome.runtime.sendMessage({ type: "GEB_POPUP_DIAGNOSE_TAB" });
  if (!res || !res.ok) {
    elDiagOut.textContent =
      "检测未通过: " + ((res && res.error) || "请先打开 https://vertexaisearch.cloud.google.com");
    return;
  }
  elDiagOut.textContent = JSON.stringify(res.diagnosis, null, 2);
}

async function triggerNewChat() {
  elDiagOut.dataset.showingDiag = "1";
  const res = await chrome.runtime.sendMessage({ type: "GEB_POPUP_NEW_CHAT" });
  elDiagOut.textContent = res && res.ok ? "已触发网页开启新对话" : JSON.stringify(res, null, 2);
}

document.getElementById("saveBtn").addEventListener("click", () => {
  void saveSettings();
});

elEnabled.addEventListener("change", () => {
  void saveSettings();
});

if (elTabSelect) {
  elTabSelect.addEventListener("change", () => {
    void saveSettings();
  });
}

if (elToggleManualBtn) {
  elToggleManualBtn.addEventListener("click", () => {
    void (async () => {
      const act = elToggleManualBtn.dataset.action || "toggle";
      elToggleManualBtn.disabled = true;
      try {
        if (act === "open_manual") {
          await openCleanGeminiTab({ manual: true, active: true });
          window.close();
          return;
        }
        const tid = Number(elToggleManualBtn.dataset.tabId || 0);
        if (tid > 0) {
          await chrome.runtime.sendMessage({ type: "GEB_TOGGLE_TAB_MANUAL", tabId: tid });
          flashSavedToast();
          await loadState(false);
        }
      } finally {
        elToggleManualBtn.disabled = false;
      }
    })();
  });
}

if (elOpenManualTabBtn) {
  elOpenManualTabBtn.addEventListener("click", () => {
    void (async () => {
      elOpenManualTabBtn.disabled = true;
      try {
        await openCleanGeminiTab({ manual: true, active: true });
        window.close();
      } finally {
        elOpenManualTabBtn.disabled = false;
      }
    })();
  });
}

if (elModeSelect) {
  elModeSelect.addEventListener("change", () => {
    void saveSettings();
  });
}

if (elAutoSpawnCheck) {
  elAutoSpawnCheck.addEventListener("change", () => {
    if (elAutoSpawn) {
      elAutoSpawn.value = elAutoSpawnCheck.checked ? "true" : "false";
    }
    void saveSettings();
  });
}

function submitCustomParallelTabs() {
  if (!elCustomTabsInput) return;
  const parsed = parseCustomParallelTabsInput(elCustomTabsInput.value);
  if (parsed === null) {
    if (elErrorBanner) {
      elErrorBanner.hidden = false;
      elErrorBanner.textContent = "请输入 1~200 之间的正整数席位数";
    }
    elCustomTabsInput.focus();
    return;
  }
  if (elErrorBanner) {
    elErrorBanner.hidden = true;
    elErrorBanner.textContent = "";
  }
  customCapEditing = false;
  if (elCustomTabsRow) elCustomTabsRow.hidden = true;
  ensureSelectHasValue(elMaxTabs, String(parsed));
  void saveSettings();
}

if (elMaxTabsSegments) {
  elMaxTabsSegments.addEventListener("click", (ev) => {
    const btn = ev.target && ev.target.closest(".seg-btn");
    if (!btn || btn.disabled) return;
    const val = btn.getAttribute("data-val");
    if (val === null || val === "") return;
    if (val === "custom") {
      const curNum = elMaxTabs ? normalizeMaxParallelTabs(elMaxTabs.value, 10) : 10;
      const isPreset = curNum === 1 || curNum === 5 || curNum === 10;
      if (customCapEditing && elCustomTabsRow && !elCustomTabsRow.hidden) {
        customCapEditing = false;
        elCustomTabsRow.hidden = true;
        syncSegmentUI(curNum, elAutoSpawn ? elAutoSpawn.value !== "false" : true, false);
        return;
      }
      customCapEditing = true;
      if (elCustomTabsRow) elCustomTabsRow.hidden = false;
      if (elCustomTabsInput) {
        elCustomTabsInput.value = isPreset ? "" : String(curNum);
        elCustomTabsInput.focus();
        if (!isPreset) elCustomTabsInput.select();
      }
      syncSegmentUI(curNum, elAutoSpawn ? elAutoSpawn.value !== "false" : true, false);
      return;
    }
    customCapEditing = false;
    if (elCustomTabsRow) elCustomTabsRow.hidden = true;
    ensureSelectHasValue(elMaxTabs, val);
    void saveSettings();
  });
}

if (elCustomTabsApplyBtn) {
  elCustomTabsApplyBtn.addEventListener("click", () => {
    submitCustomParallelTabs();
  });
}

if (elCustomTabsInput) {
  elCustomTabsInput.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter") {
      ev.preventDefault();
      submitCustomParallelTabs();
    } else if (ev.key === "Escape") {
      ev.preventDefault();
      customCapEditing = false;
      if (elCustomTabsRow) elCustomTabsRow.hidden = true;
      const curNum = elMaxTabs ? normalizeMaxParallelTabs(elMaxTabs.value, 10) : 10;
      syncSegmentUI(curNum, elAutoSpawn ? elAutoSpawn.value !== "false" : true, false);
    }
  });
}

const reconnectBtn = document.getElementById("reconnectBtn");
if (reconnectBtn) {
  reconnectBtn.addEventListener("click", () => {
    void (async () => {
      await chrome.runtime.sendMessage({ type: "GEB_POPUP_RECONNECT" });
      flashSavedToast();
      await loadState(false);
    })();
  });
}

const spawnBtn = document.getElementById("spawnTabBtn");
if (spawnBtn) {
  spawnBtn.addEventListener("click", () => {
    void (async () => {
      elDiagOut.dataset.showingDiag = "1";
      elDiagOut.textContent = "正在后台新开工作标签页...";
      const res = await chrome.runtime.sendMessage({ type: "GEB_POPUP_SPAWN_TAB" });
      elDiagOut.textContent = res && res.ok
        ? "已新增后台工作标签页"
        : "新增失败: " + ((res && res.error) || "未知原因");
      await loadState(false);
    })();
  });
}

if (elOpenVertexBtn) {
  elOpenVertexBtn.addEventListener("click", () => {
    void (async () => {
      await openCleanGeminiTab({ manual: false, active: true });
      window.close();
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

const popupParams =
  typeof window !== "undefined" && typeof URLSearchParams === "function"
    ? new URLSearchParams(window.location?.search || "")
    : null;
if (popupParams && popupParams.get("geb_open_manual") === "1") {
  void (async () => {
    await openCleanGeminiTab({
      manual: true,
      active: popupParams.get("bg") !== "1"
    });
    window.close();
  })();
} else {
  void loadState(true);
}
setInterval(() => {
  void loadState(false);
}, 2500);
