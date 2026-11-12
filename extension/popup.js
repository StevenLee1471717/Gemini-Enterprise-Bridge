// Popup controller for Gemini Enterprise Web-to-API Bridge (MV3 compliant, async/await)

const elEnabled = document.getElementById("enabledToggle");
const elExtVersionPill = document.getElementById("extVersionPill");
const elCheckUpdateBtn = document.getElementById("checkUpdateBtn");
const elUpdateBanner = document.getElementById("updateBanner");
const elUpdateTitle = document.getElementById("updateBannerTitle");
const elUpdateReleaseLink = document.getElementById("updateReleaseLink");
const elUpdateDesc = document.getElementById("updateBannerDesc");
const elApplyBackendUpdateBtn = document.getElementById("applyBackendUpdateBtn");
const elSyncExtUpdateBtn = document.getElementById("syncExtUpdateBtn");
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

const elActiveRateBadge = document.getElementById("activeRateBadge");
const elResetUsageBtn = document.getElementById("resetUsageBtn");
const elAllTimeUsd = document.getElementById("allTimeUsdText");
const elAllTimeCny = document.getElementById("allTimeCnyText");
const elCachedSubline = document.getElementById("cachedSublineText");
const elTodayUsd = document.getElementById("todayUsdText");
const elTodayMeta = document.getElementById("todayMetaText");
const elTotalPromptTokens = document.getElementById("totalPromptTokensText");
const elTotalCachedTokens = document.getElementById("totalCachedTokensText");
const elTotalCompletionTokens = document.getElementById("totalCompletionTokensText");
const elContextSummary = document.getElementById("contextSummaryText");
const elContextProgress = document.getElementById("contextProgressBar");
const elContextTurnMeta = document.getElementById("contextTurnMetaText");
const elContextBreakdown = document.getElementById("contextBreakdownText");
const elSlotPills = document.getElementById("slotContextPills");
const elModelCountHint = document.getElementById("modelCountHint");
const elModelBreakdownList = document.getElementById("modelBreakdownList");

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

function formatTokensCompact(n) {
  const num = Number(n || 0);
  if (!Number.isFinite(num) || num <= 0) return "0";
  if (num >= 1_000_000) return `${(num / 1_000_000).toFixed(2)}M`;
  if (num >= 1_000) return `${(num / 1_000).toFixed(1)}K`;
  return String(Math.round(num));
}

function formatUsd(val) {
  const num = Number(val || 0);
  if (!Number.isFinite(num) || num <= 0) return "$0.00";
  if (num < 0.01) return `$${num.toFixed(4)}`;
  return `$${num.toFixed(2)}`;
}

function formatCny(val) {
  const num = Number(val || 0);
  if (!Number.isFinite(num) || num <= 0) return "¥0.00";
  if (num < 0.01) return `¥${num.toFixed(3)}`;
  return `¥${num.toFixed(2)}`;
}

function formatStrategyLabel(strategy) {
  const map = {
    smart_incremental_delta: "增量续聊",
    smart_single_turn: "首轮/单轮",
    smart_packed_context: "全量重装",
    gcloud_direct: "GCloud 直连",
    gcloud_stream: "GCloud 流式",
    idle: "待命"
  };
  return map[strategy] || strategy || "待命";
}

function renderUsageAndContext(snapshot) {
  if (!snapshot || typeof snapshot !== "object") {
    return;
  }
  const usage = snapshot.usage || {};
  const ctxMon = snapshot.context_monitor || {};
  const allTime = usage.all_time || {};
  const today = usage.today || {};
  const activePricing = usage.active_pricing || {};

  if (elActiveRateBadge && activePricing.display_name) {
    const shortName = String(activePricing.display_name).replace(/^Gemini\s+/i, "");
    elActiveRateBadge.textContent = `${shortName} 官方价: $${activePricing.input_per_1m}/$${activePricing.output_per_1m} / 1M`;
    elActiveRateBadge.title = `${activePricing.display_name} · 输入 $${activePricing.input_per_1m}/1M · 缓存 $${activePricing.cached_input_per_1m}/1M · 输出 $${activePricing.output_per_1m}/1M (${activePricing.rate_note || ""})`;
  }

  if (elAllTimeUsd) elAllTimeUsd.textContent = formatUsd(allTime.equivalent_cost_usd);
  if (elAllTimeCny) elAllTimeCny.textContent = `≈ ${formatCny(allTime.equivalent_cost_cny)}`;
  if (elCachedSubline) {
    elCachedSubline.textContent = `含缓存折算: ${formatUsd(allTime.cached_equivalent_cost_usd)} · 累计 ${allTime.requests || 0} 次`;
  }

  if (elTodayUsd) elTodayUsd.textContent = formatUsd(today.equivalent_cost_usd);
  if (elTodayMeta) {
    elTodayMeta.textContent = `≈ ${formatCny(today.equivalent_cost_cny)} · ${today.requests || 0} 次调用`;
  }

  if (elTotalPromptTokens) elTotalPromptTokens.textContent = formatTokensCompact(allTime.prompt_tokens);
  if (elTotalCachedTokens) elTotalCachedTokens.textContent = formatTokensCompact(allTime.cached_tokens);
  if (elTotalCompletionTokens) elTotalCompletionTokens.textContent = formatTokensCompact(allTime.completion_tokens);

  const primary = ctxMon.primary || {};
  const totalCtx = Number(primary.total_context_tokens || 0);
  const limitCtx = Number(primary.context_limit_tokens || 1048576);
  const utilPct = Number(primary.utilization_pct || 0);

  if (elContextSummary) {
    elContextSummary.textContent = `${formatTokensCompact(totalCtx)} / ${formatTokensCompact(limitCtx)} (${utilPct.toFixed(1)}%)`;
  }
  if (elContextProgress) {
    elContextProgress.value = Math.min(100, Math.max(0, utilPct));
    elContextProgress.classList.remove("tier-low", "tier-mid", "tier-high");
    if (utilPct >= 85) {
      elContextProgress.classList.add("tier-high");
    } else if (utilPct >= 60) {
      elContextProgress.classList.add("tier-mid");
    } else {
      elContextProgress.classList.add("tier-low");
    }
  }

  if (elContextTurnMeta) {
    if ((primary.turn_count || 0) > 0) {
      const tabTag = primary.tab_id ? `#${primary.tab_id} ` : "";
      const modelTag = primary.active_model ? `${primary.active_model} · ` : "";
      const compactTag = primary.compaction_count ? ` · 重装${primary.compaction_count}次` : "";
      elContextTurnMeta.textContent = `${tabTag}${modelTag}第 ${primary.user_turns || primary.turn_count} 轮 · ${formatStrategyLabel(primary.last_strategy)}${compactTag}`;
    } else {
      elContextTurnMeta.textContent = "空闲待命 · 发送首条请求后实时追踪";
    }
  }
  if (elContextBreakdown) {
    elContextBreakdown.textContent = `Sys ${formatTokensCompact(primary.system_tokens)} · Tools ${formatTokensCompact(primary.tools_tokens)} · Hist ${formatTokensCompact(primary.history_tokens)}`;
  }

  if (elSlotPills) {
    const slots = Array.isArray(ctxMon.slots) ? ctxMon.slots : [];
    elSlotPills.replaceChildren();
    if (slots.length > 1 || (slots.length === 1 && (slots[0].turn_count || 0) > 0)) {
      elSlotPills.hidden = false;
      for (const s of slots.slice(0, 6)) {
        const pill = document.createElement("span");
        pill.className = s.busy ? "slot-pill busy" : "slot-pill";
        const tid = s.tab_id ? `Tab #${s.tab_id}` : s.slot_key || "Slot";
        const mShort = String(s.active_model || "Auto").replace(/^Gemini\s+/i, "");
        pill.textContent = `${tid} (${mShort}): ${formatTokensCompact(s.total_context_tokens)} (${Number(s.utilization_pct || 0).toFixed(1)}%)`;
        pill.title = `轮次: ${s.turn_count || 0} | 策略: ${formatStrategyLabel(s.last_strategy)} | 增量: ${formatTokensCompact(s.last_delta_tokens)}`;
        elSlotPills.appendChild(pill);
      }
    } else {
      elSlotPills.hidden = true;
    }
  }

  if (elModelBreakdownList && elModelCountHint) {
    const byModel = usage.by_model || {};
    const entries = Object.entries(byModel);
    elModelCountHint.textContent = entries.length > 0 ? `${entries.length} 个模型` : "暂无记录";
    elModelBreakdownList.replaceChildren();
    if (entries.length === 0) {
      const emptyRow = document.createElement("div");
      emptyRow.className = "cost-subline";
      emptyRow.textContent = "发起对话后自动按模型统计官方 API 等价账单。";
      elModelBreakdownList.appendChild(emptyRow);
    } else {
      for (const [modelKey, info] of entries) {
        const row = document.createElement("div");
        row.className = "model-item";

        const left = document.createElement("div");
        left.className = "model-item-left";
        const nameEl = document.createElement("span");
        nameEl.className = "model-item-name";
        nameEl.textContent = info.display_name || modelKey;
        const subEl = document.createElement("span");
        subEl.className = "model-item-sub";
        subEl.textContent = `$${info.input_rate_per_1m}/$${info.output_rate_per_1m} · ${info.requests || 0}次 · In ${formatTokensCompact(info.prompt_tokens)} / Out ${formatTokensCompact(info.completion_tokens)}`;
        left.appendChild(nameEl);
        left.appendChild(subEl);

        const right = document.createElement("div");
        right.className = "model-item-right";
        const usdEl = document.createElement("span");
        usdEl.className = "model-item-usd";
        usdEl.textContent = formatUsd(info.equivalent_cost_usd);
        const cnyEl = document.createElement("span");
        cnyEl.className = "model-item-sub";
        cnyEl.textContent = `Cached ${formatUsd(info.cached_equivalent_cost_usd)}`;
        right.appendChild(usdEl);
        right.appendChild(cnyEl);

        row.appendChild(left);
        row.appendChild(right);
        elModelBreakdownList.appendChild(row);
      }
    }
  }
}

function renderUpdateBanner(extVer, updateState, liveApiStatus = null) {
  if (elExtVersionPill && extVer) {
    elExtVersionPill.textContent = `v${String(extVer).replace(/^v/i, "")}`;
  }
  if (!elUpdateBanner) return;

  const u = updateState || {};
  const apiUpd = (liveApiStatus && liveApiStatus.update) || {};
  const cleanExtVer = String(extVer || u.extensionVersion || "1.2.0").replace(/^v/i, "");
  const backendVer = String(
    (liveApiStatus && liveApiStatus.backend_version) ||
      apiUpd.current_backend_version ||
      u.backendVersion ||
      ""
  ).replace(/^v/i, "");
  const remoteVer = String(
    apiUpd.latest_github_version || u.latestGitHubVersion || ""
  ).replace(/^v/i, "");
  const releaseUrl =
    apiUpd.release_url ||
    u.releaseUrl ||
    "https://github.com/StevenLee1471717/Gemini-Enterprise-Bridge/releases";

  if (elUpdateReleaseLink) {
    elUpdateReleaseLink.href = releaseUrl;
  }

  const backendNeedsUpdate = Boolean(
    (liveApiStatus && liveApiStatus.backend_update_required) ||
      apiUpd.backend_outdated ||
      u.backendUpdateRequired
  );
  const extNeedsUpdate = Boolean(apiUpd.extension_outdated || u.extensionOutdated);

  if (backendNeedsUpdate) {
    elUpdateBanner.hidden = false;
    const targetV = remoteVer || cleanExtVer;
    if (elUpdateTitle) {
      elUpdateTitle.textContent = `请更新本地后端 -> v${targetV}`;
    }
    if (elUpdateDesc) {
      const prefix =
        backendVer && cleanExtVer !== backendVer
          ? `插件已更新至 v${cleanExtVer}`
          : `检测到 GitHub 新版本 v${targetV}`;
      elUpdateDesc.textContent = `${prefix}，当前本地后端为 v${backendVer || "旧版"} — 点击下方按钮即可从 GitHub 自动更新并平滑重启后端（保留 .env 与统计）。`;
    }
    if (elApplyBackendUpdateBtn) elApplyBackendUpdateBtn.hidden = false;
    if (elSyncExtUpdateBtn) elSyncExtUpdateBtn.hidden = !extNeedsUpdate;
  } else if (extNeedsUpdate) {
    elUpdateBanner.hidden = false;
    const targetV = remoteVer || backendVer || "latest";
    if (elUpdateTitle) {
      elUpdateTitle.textContent = `检测到插件新版本 v${targetV}`;
    }
    if (elUpdateDesc) {
      elUpdateDesc.textContent = u.pendingExtensionReloadVersion
        ? `本地 extension/ 已同步至 v${targetV}，将在空闲时自动重载；也可点击立即重载。`
        : `当前浏览器插件为 v${cleanExtVer}，可从 GitHub Releases 自动同步并热重载至 v${targetV}。`;
    }
    if (elApplyBackendUpdateBtn) elApplyBackendUpdateBtn.hidden = true;
    if (elSyncExtUpdateBtn) elSyncExtUpdateBtn.hidden = false;
  } else {
    elUpdateBanner.hidden = true;
  }
}

async function refreshBackendUsageLive(wsUrl, fallbackSnapshot, extVer, cachedUpdateState) {
  const baseHttp = deriveHttpConsoleUrl(wsUrl);
  try {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), 1600);
    const resp = await fetch(`${baseHttp}api/status`, { signal: ctrl.signal });
    clearTimeout(timer);
    if (resp.ok) {
      const data = await resp.json();
      renderUpdateBanner(extVer, cachedUpdateState, data);
      if (data && data.usage) {
        renderUsageAndContext({
          usage: data.usage,
          context_monitor: data.context_monitor || {}
        });
        return;
      }
    }
  } catch (_) {
    // Fall back to cached WebSocket snapshot when backend HTTP is unreachable
  }
  renderUpdateBanner(extVer, cachedUpdateState, null);
  if (fallbackSnapshot) {
    renderUsageAndContext(fallbackSnapshot);
  }
}

async function loadState(overwriteInputs = false) {
  try {
    const state = await chrome.runtime.sendMessage({ type: "GEB_POPUP_GET_STATE" });
    if (!state || !state.ok) {
      return;
    }
    const { extensionVersion, updateState, settings, wsConnected, lastError, capturedRpc, activeModel, activeTab, vertexTabs, preferredTabId, usageSnapshot, logs } = state;
    renderUpdateBanner(extensionVersion, updateState, null);

    if (!initialFormHydrated || overwriteInputs) {
      elEnabled.checked = Boolean(settings.enabled);
      elModeSelect.value = settings.driverMode || "auto";
      if (elAutoSpawn) elAutoSpawn.value = settings.autoSpawnTabs === false ? "false" : "true";
      if (elMaxTabs) elMaxTabs.value = String(settings.maxParallelTabs || 6);
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

    if (usageSnapshot) {
      renderUsageAndContext(usageSnapshot);
    }
    void refreshBackendUsageLive(settings.wsUrl, usageSnapshot, extensionVersion, updateState);

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

if (elResetUsageBtn) {
  elResetUsageBtn.addEventListener("click", () => {
    void (async () => {
      const baseHttp = deriveHttpConsoleUrl(elWsUrl.value);
      try {
        const resp = await fetch(`${baseHttp}api/usage/reset`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ scope: "all" })
        });
        if (resp.ok) {
          const data = await resp.json();
          renderUsageAndContext({
            usage: data.usage,
            context_monitor: data.context_monitor || {}
          });
        }
      } catch (err) {
        elDiagOut.textContent = "重置用量统计失败: " + err.message;
      }
    })();
  });
}

if (elCheckUpdateBtn) {
  elCheckUpdateBtn.addEventListener("click", () => {
    void (async () => {
      elCheckUpdateBtn.disabled = true;
      elCheckUpdateBtn.textContent = "检查中...";
      try {
        const res = await chrome.runtime.sendMessage({ type: "GEB_POPUP_CHECK_UPDATE" });
        await loadState(false);
        const u = (res && res.updateState) || {};
        if (!u.backendUpdateRequired && !u.extensionOutdated) {
          elCheckUpdateBtn.textContent = "已是最新";
          setTimeout(() => {
            elCheckUpdateBtn.textContent = "检查更新";
          }, 2200);
        } else {
          elCheckUpdateBtn.textContent = `发现 v${u.latestGitHubVersion || u.backendVersion}`;
        }
      } catch (_) {
        elCheckUpdateBtn.textContent = "检查失败";
        setTimeout(() => {
          elCheckUpdateBtn.textContent = "检查更新";
        }, 2200);
      } finally {
        elCheckUpdateBtn.disabled = false;
      }
    })();
  });
}

if (elApplyBackendUpdateBtn) {
  elApplyBackendUpdateBtn.addEventListener("click", () => {
    void (async () => {
      const baseHttp = deriveHttpConsoleUrl(elWsUrl.value);
      elApplyBackendUpdateBtn.disabled = true;
      elApplyBackendUpdateBtn.textContent = "正在更新并重启后端...";
      try {
        const resp = await fetch(`${baseHttp}api/update/apply`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ restart: true })
        });
        const data = await resp.json();
        if (resp.ok && data.ok) {
          if (elUpdateDesc) {
            elUpdateDesc.textContent =
              data.message || `已更新至 v${data.updated_version}，后端正在平滑重启...`;
          }
        } else if (elUpdateDesc) {
          elUpdateDesc.textContent = `自动更新失败: ${(data && data.error) || resp.status}`;
        }
      } catch (_) {
        if (elUpdateDesc) {
          elUpdateDesc.textContent = "后端已触发热重启更新，预计约 3 秒后自动恢复连接...";
        }
      } finally {
        setTimeout(() => {
          elApplyBackendUpdateBtn.disabled = false;
          elApplyBackendUpdateBtn.textContent = "立即自动更新并重启后端";
          void loadState(false);
        }, 3500);
      }
    })();
  });
}

if (elSyncExtUpdateBtn) {
  elSyncExtUpdateBtn.addEventListener("click", () => {
    void (async () => {
      elSyncExtUpdateBtn.disabled = true;
      elSyncExtUpdateBtn.textContent = "正在同步并重载...";
      const res = await chrome.runtime.sendMessage({ type: "GEB_POPUP_SYNC_EXTENSION" });
      if (!res || !res.ok) {
        elSyncExtUpdateBtn.disabled = false;
        elSyncExtUpdateBtn.textContent = "同步并重载插件";
        if (elUpdateDesc) {
          elUpdateDesc.textContent = "同步插件失败: " + ((res && res.error) || "请确认本地后端已启动");
        }
      }
    })();
  });
}

void loadState(true);
setInterval(() => {
  void loadState(false);
}, 2500);
