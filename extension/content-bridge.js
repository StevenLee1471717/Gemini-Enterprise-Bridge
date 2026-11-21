// ISOLATED world content script bridging chrome.runtime <-> MAIN world page-driver.js
// Hot-swappable: re-injecting on extension reload cleanly replaces stale listeners without page refresh.

(() => {
  if (typeof globalThis.__GEB_ISOLATED_CLEANUP__ === "function") {
    try {
      globalThis.__GEB_ISOLATED_CLEANUP__();
    } catch (_) {}
  }

  const TO_MAIN_CHANNEL = "GEB_ISOLATED_TO_MAIN";
  const FROM_MAIN_CHANNEL = "GEB_MAIN_TO_ISOLATED";
  const MAIN_POSTMSG_CHUNK_SIZE = 32768;
  const pendingRpcCalls = new Map();
  const msgQueue = [];
  let mainReady = false;
  let mainDriverVersion = "";
  let keepalivePort = null;
  let portRetryAttempts = 0;
  let portRetryTimer = null;
  let hiddenKeepaliveTimer = null;
  let disposed = false;
  let indicatorUi = null;

  function initTakeoverIndicator() {
    if (
      disposed ||
      typeof document === "undefined" ||
      typeof document.createElement !== "function"
    ) {
      return null;
    }
    const mountRoot = document.documentElement || document.body;
    if (!mountRoot || typeof mountRoot.appendChild !== "function") {
      return null;
    }
    try {
      const oldHost = document.getElementById("geb-takeover-indicator-host");
      if (oldHost && typeof oldHost.remove === "function") {
        oldHost.remove();
      }
    } catch (_) {}

    const host = document.createElement("div");
    host.id = "geb-takeover-indicator-host";
    host.setAttribute("data-geb-ui", "1");
    host.style.cssText =
      "position:fixed;right:18px;bottom:18px;z-index:2147483640;pointer-events:none;margin:0;padding:0;border:0;background:transparent;";

    if (typeof host.attachShadow !== "function") {
      return null;
    }
    // Closed ShadowRoot keeps page-driver.js Shadow DOM traversal 100% blind to our UI!
    const shadow = host.attachShadow({ mode: "closed" });
    const style = document.createElement("style");
    style.textContent = `
      :host { all: initial; }
      .pill {
        pointer-events: auto;
        display: inline-flex;
        align-items: center;
        gap: 7px;
        padding: 5px 11px;
        border-radius: 999px;
        font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", "SF Pro Text", "Segoe UI", system-ui, sans-serif;
        font-size: 12px;
        font-weight: 600;
        line-height: 1.2;
        color: #0f172a;
        background: rgba(255, 255, 255, 0.94);
        border: 1px solid rgba(16, 185, 129, 0.36);
        box-shadow: 0 8px 24px -6px rgba(15, 23, 42, 0.14), 0 2px 6px rgba(15, 23, 42, 0.05);
        backdrop-filter: blur(12px);
        -webkit-backdrop-filter: blur(12px);
        user-select: none;
        transition: all 0.18s ease;
      }
      .pill:hover {
        box-shadow: 0 10px 28px -6px rgba(15, 23, 42, 0.2), 0 2px 8px rgba(15, 23, 42, 0.07);
      }
      .dot {
        width: 8px;
        height: 8px;
        border-radius: 50%;
        background: #10b981;
        box-shadow: 0 0 0 3px rgba(16, 185, 129, 0.18);
        flex-shrink: 0;
      }
      @keyframes gebPulse {
        0%, 100% { transform: scale(1); opacity: 1; }
        50% { transform: scale(1.32); opacity: 0.55; }
      }
      .pill.working {
        background: rgba(239, 246, 255, 0.96);
        border-color: rgba(37, 99, 235, 0.45);
        color: #1e3a8a;
      }
      .pill.working .dot {
        background: #2563eb;
        box-shadow: 0 0 0 3px rgba(37, 99, 235, 0.2);
        animation: gebPulse 1.3s ease-in-out infinite;
      }
      .pill.manual {
        background: rgba(255, 251, 235, 0.96);
        border-color: rgba(217, 119, 6, 0.42);
        color: #92400e;
      }
      .pill.manual .dot {
        background: #f59e0b;
        box-shadow: 0 0 0 3px rgba(245, 158, 11, 0.2);
      }
      .pill.overflow, .pill.paused {
        background: rgba(248, 250, 252, 0.95);
        border-color: rgba(148, 163, 184, 0.48);
        color: #475569;
      }
      .pill.overflow .dot, .pill.paused .dot {
        background: #94a3b8;
        box-shadow: none;
      }
      .pill.disconnected {
        background: rgba(255, 241, 242, 0.95);
        border-color: rgba(244, 63, 94, 0.36);
        color: #9f1239;
      }
      .pill.disconnected .dot {
        background: #f43f5e;
        box-shadow: 0 0 0 3px rgba(244, 63, 94, 0.16);
      }
      .sep {
        width: 1px;
        height: 11px;
        background: rgba(148, 163, 184, 0.35);
      }
      .act {
        border: 0;
        background: transparent;
        padding: 1px 4px;
        border-radius: 5px;
        font: inherit;
        font-size: 11px;
        font-weight: 650;
        color: #2563eb;
        cursor: pointer;
        transition: background 0.15s ease;
      }
      .act:hover {
        background: rgba(37, 99, 235, 0.1);
      }
      .pill.manual .act {
        color: #b45309;
      }
      .pill.manual .act:hover {
        background: rgba(217, 119, 6, 0.12);
      }
    `;

    const pill = document.createElement("div");
    pill.className = "pill";
    const dot = document.createElement("span");
    dot.className = "dot";
    const label = document.createElement("span");
    label.textContent = "Bridge 检测中...";
    const sep = document.createElement("span");
    sep.className = "sep";
    const actionBtn = document.createElement("button");
    actionBtn.type = "button";
    actionBtn.className = "act";
    actionBtn.textContent = "设为自用";

    actionBtn.addEventListener("click", (ev) => {
      ev.preventDefault();
      ev.stopPropagation();
      if (disposed || !chrome.runtime?.id) return;
      actionBtn.disabled = true;
      void (async () => {
        try {
          const res = await chrome.runtime.sendMessage({ type: "GEB_TOGGLE_TAB_MANUAL" });
          if (res && res.ok && res.state) {
            renderTakeoverState(res.state);
          }
        } catch (_) {
        } finally {
          actionBtn.disabled = false;
        }
      })();
    });

    pill.appendChild(dot);
    pill.appendChild(label);
    pill.appendChild(sep);
    pill.appendChild(actionBtn);
    shadow.appendChild(style);
    shadow.appendChild(pill);
    mountRoot.appendChild(host);

    return { host, pill, label, sep, actionBtn };
  }

  function renderTakeoverState(state) {
    if (!indicatorUi) {
      indicatorUi = initTakeoverIndicator();
    }
    if (!indicatorUi || !state) return;
    const mode = state.mode || "managed";
    const seatTag = state.seatIndex ? ` · #${state.seatIndex}` : "";
    indicatorUi.pill.className = `pill ${mode}`;

    if (mode === "manual") {
      indicatorUi.label.textContent = "手动自用 · 免接管";
      indicatorUi.actionBtn.textContent = "恢复接管";
      indicatorUi.sep.hidden = false;
      indicatorUi.actionBtn.hidden = false;
      indicatorUi.pill.title = "当前页面与会话已锁定为你个人手动使用，Bridge 不会接管或清空此页";
    } else if (mode === "working") {
      indicatorUi.label.textContent = `Bridge 工作中${seatTag}`;
      indicatorUi.actionBtn.textContent = "设为自用";
      indicatorUi.sep.hidden = false;
      indicatorUi.actionBtn.hidden = false;
      indicatorUi.pill.title = "Bridge 正在此标签页执行任务；点击“设为自用”可将该页移出接管池";
    } else if (mode === "overflow") {
      indicatorUi.label.textContent =
        state.maxTabs > 0 ? `超出上限 (${state.maxTabs}页) · 未接管` : "超出上限 · 未接管";
      indicatorUi.actionBtn.textContent = "锁定自用";
      indicatorUi.sep.hidden = false;
      indicatorUi.actionBtn.hidden = false;
      indicatorUi.pill.title = "当前已接管页面数已达上限，此页面暂不接入 Bridge";
    } else if (mode === "paused") {
      indicatorUi.label.textContent = "Bridge 已暂停 · 未接管";
      indicatorUi.sep.hidden = true;
      indicatorUi.actionBtn.hidden = true;
      indicatorUi.pill.title = "右上角插件总开关已暂停";
    } else if (mode === "disconnected") {
      indicatorUi.label.textContent = `待连本地服务${seatTag}`;
      indicatorUi.actionBtn.textContent = "设为自用";
      indicatorUi.sep.hidden = false;
      indicatorUi.actionBtn.hidden = false;
      indicatorUi.pill.title = "已纳入接管池，正在等待本机 8000 服务连接";
    } else {
      indicatorUi.label.textContent = `Bridge 已接管${seatTag}`;
      indicatorUi.actionBtn.textContent = "设为自用";
      indicatorUi.sep.hidden = false;
      indicatorUi.actionBtn.hidden = false;
      indicatorUi.pill.title = "此页面已接入 Bridge 自动工作池；如需自己聊天可点“设为自用”";
    }
  }

  async function refreshMyTakeoverState() {
    if (
      disposed ||
      !chrome.runtime?.id ||
      typeof document === "undefined" ||
      typeof document.createElement !== "function"
    ) {
      return;
    }
    try {
      const res = await chrome.runtime.sendMessage({ type: "GEB_GET_MY_TAKEOVER_STATE" });
      if (res && res.ok && res.state) {
        renderTakeoverState(res.state);
      }
    } catch (_) {}
  }

  // Guarded Trusted Types policy for ISOLATED world
  try {
    if (window.trustedTypes && typeof window.trustedTypes.createPolicy === "function") {
      window.trustedTypes.createPolicy("geb-bridge-isolated", { createHTML: (s) => String(s || "") });
    }
  } catch (_) {}

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

  class ReassemblyBuffer {
    constructor() {
      this._store = new Map();
    }
    addChunk(requestId, chunkIndex, totalChunks, data) {
      let entry = this._store.get(requestId);
      if (!entry) {
        entry = { parts: [], total: totalChunks || 0 };
        this._store.set(requestId, entry);
      }
      entry.parts[chunkIndex] = String(data || "");
      if (totalChunks) entry.total = totalChunks;
    }
    has(requestId) {
      return Boolean(requestId) && this._store.has(requestId);
    }
    assemble(requestId, expectedTotal = 0) {
      const entry = this._store.get(requestId);
      this._store.delete(requestId);
      const total = expectedTotal || (entry ? entry.total : 0);
      return assembleChunks(entry, total);
    }
    clear() {
      this._store.clear();
    }
  }

  const promptReassembly = new ReassemblyBuffer();
  const replyReassembly = new ReassemblyBuffer();
  const attachmentReassembly = new ReassemblyBuffer();

  function normalizeAttachmentDataUrl(rawPayload, mimeType) {
    const raw = String(rawPayload || "").trim();
    if (!raw) return "";
    if (/^data:/i.test(raw)) return raw;
    const cleanMime =
      String(mimeType || "application/octet-stream")
        .split(";")[0]
        .split(",")[0]
        .trim() || "application/octet-stream";
    return `data:${cleanMime};base64,${raw.replace(/\s+/g, "")}`;
  }

  function unwrapInlineToolChunks(text) {
    if (!text || !text.includes("[TOOL_CHUNK:")) return text;
    return text.replace(/\[TOOL_CHUNK:part_\d+_of_\d+\]([\s\S]*?)\[\/TOOL_CHUNK\]/g, "$1");
  }

  function dispatchPromptToMain(message) {
    try {
      if (message.driver_config) {
        postToMain("configure", message.driver_config);
      }
      const rawPrompt = message.chunked
        ? promptReassembly.assemble(message.request_id, message.total_chunks)
        : String(message.prompt || "");
      const fullPrompt = unwrapInlineToolChunks(rawPrompt);

      const hydratedAttachments = (message.attachments || []).map((att) => {
        if (!att || typeof att !== "object") return att;
        const legacyId = att.id || att.attachment_id || "";
        const candidateKeys = [
          att.chunked_att_key,
          legacyId && message.request_id ? `${message.request_id}:${legacyId}` : "",
          legacyId
        ].filter(Boolean);
        const matchedKey = candidateKeys.find((k) => attachmentReassembly.has(k));
        if (matchedKey || att.chunked_att_key) {
          const keyToAssemble = matchedKey || att.chunked_att_key;
          const rawReassembled = attachmentReassembly.assemble(keyToAssemble, att.total_chunks);
          const dataUrl = normalizeAttachmentDataUrl(rawReassembled, att.mime_type);
          const copy = { ...att, data_url: dataUrl };
          delete copy.chunked_att_key;
          delete copy.total_chunks;
          return copy;
        }
        const inlineRaw = att.data_url || att.data_b64 || att.b64_json || att.file_data || att.data || "";
        if (inlineRaw) {
          return { ...att, data_url: normalizeAttachmentDataUrl(inlineRaw, att.mime_type) };
        }
        return att;
      });

      const mainAttachments = hydratedAttachments.map((att, idx) => {
        const dUrl = String((att && att.data_url) || "");
        if (dUrl.length > 131072) {
          const attKey = `${message.request_id}:att:${idx}`;
          const aTotal = Math.ceil(dUrl.length / 131072);
          for (let i = 0; i < aTotal; i += 1) {
            postToMain("attachment_chunk", {
              att_key: attKey,
              chunk_index: i,
              total_chunks: aTotal,
              data: dUrl.slice(i * 131072, (i + 1) * 131072)
            });
          }
          return { ...att, data_url: "", chunked_att_key: attKey, total_chunks: aTotal };
        }
        return att;
      });

      if (fullPrompt.length > MAIN_POSTMSG_CHUNK_SIZE) {
        const total = Math.ceil(fullPrompt.length / MAIN_POSTMSG_CHUNK_SIZE);
        for (let i = 0; i < total; i += 1) {
          postToMain("prompt_chunk", {
            request_id: message.request_id,
            chunk_index: i,
            total_chunks: total,
            data: fullPrompt.slice(i * MAIN_POSTMSG_CHUNK_SIZE, (i + 1) * MAIN_POSTMSG_CHUNK_SIZE)
          });
        }
        postToMain("execute_prompt", {
          request_id: message.request_id,
          prompt: "",
          chunked: true,
          total_chunks: total,
          callback_url: message.callback_url || "",
          attachments: mainAttachments,
          image_generation: Boolean(message.image_generation),
          video_generation: Boolean(message.video_generation),
          target_model: message.target_model || null,
          reset_chat: Boolean(message.reset_chat),
          timeout_ms: message.timeout_ms || 600000
        });
      } else {
        postToMain("execute_prompt", {
          request_id: message.request_id,
          prompt: fullPrompt,
          callback_url: message.callback_url || "",
          attachments: mainAttachments,
          image_generation: Boolean(message.image_generation),
          video_generation: Boolean(message.video_generation),
          target_model: message.target_model || null,
          reset_chat: Boolean(message.reset_chat),
          timeout_ms: message.timeout_ms || 600000
        });
      }
    } catch (error) {
      void chrome.runtime.sendMessage({type: "GEB_DRIVER_EVENT", payload: {
        type: "request_error", request_id: message.request_id, submitted: false, tab_reset: false, error: error.message
      }}).catch(() => {});
    }
  }

  function postToMain(action, payload = {}, callId = null) {
    window.postMessage(
      {
        channel: TO_MAIN_CHANNEL,
        action,
        callId,
        payload
      },
      window.location.origin
    );
  }

  function flushQueuedPrompts() {
    if (disposed || msgQueue.length === 0) return;
    mainReady = true;
    while (msgQueue.length > 0) {
      const queued = msgQueue.shift();
      dispatchPromptToMain(queued);
    }
  }

  function schedulePortReconnect() {
    if (disposed || !chrome.runtime?.id || portRetryTimer !== null) return;
    const attempt = portRetryAttempts;
    portRetryAttempts = Math.min(attempt + 1, 6);
    const delayMs = Math.min(500 * Math.pow(2, attempt), 10000);
    portRetryTimer = setTimeout(() => {
      portRetryTimer = null;
      ensureKeepalivePort();
    }, delayMs);
  }

  function ensureKeepalivePort() {
    if (disposed || !chrome.runtime?.id || keepalivePort) return;
    try {
      keepalivePort = chrome.runtime.connect({ name: "geb-keepalive" });
      portRetryAttempts = 0;
      if (portRetryTimer !== null) {
        clearTimeout(portRetryTimer);
        portRetryTimer = null;
      }
      keepalivePort.onDisconnect.addListener(() => {
        // Page navigation closes this port normally; consume Chrome's diagnostic.
        void chrome.runtime.lastError;
        keepalivePort = null;
        if (!disposed && chrome.runtime?.id) {
          schedulePortReconnect();
        }
      });
    } catch (_) {
      keepalivePort = null;
      schedulePortReconnect();
    }
  }

  function updateVisibilityKeepalive() {
    if (disposed || !chrome.runtime?.id) return;
    const isHidden = Boolean(document.hidden || document.visibilityState === "hidden");

    void (async () => {
      try {
        await chrome.runtime.sendMessage({
          type: "GEB_DRIVER_EVENT",
          payload: {
            type: "tab_visibility",
            hidden: isHidden,
            visibility_state: document.visibilityState || (isHidden ? "hidden" : "visible")
          }
        });
      } catch (_) {}
    })();

    if (isHidden) {
      ensureKeepalivePort();
      if (hiddenKeepaliveTimer === null) {
        hiddenKeepaliveTimer = setInterval(() => {
          if (disposed || !chrome.runtime?.id || !document.hidden) {
            if (hiddenKeepaliveTimer !== null) {
              clearInterval(hiddenKeepaliveTimer);
              hiddenKeepaliveTimer = null;
            }
            return;
          }
          ensureKeepalivePort();
          if (keepalivePort) {
            try {
              keepalivePort.postMessage({ type: "keepalive_ping", ts: Date.now() });
            } catch (_) {
              keepalivePort = null;
            }
          }
        }, 15000);
      }
    } else {
      if (hiddenKeepaliveTimer !== null) {
        clearInterval(hiddenKeepaliveTimer);
        hiddenKeepaliveTimer = null;
      }
      flushQueuedPrompts();
    }
  }

  function callMainAsync(action, payload = {}, timeoutMs = 10000) {
    return new Promise((resolve, reject) => {
      const callId = "call-" + Math.random().toString(36).slice(2, 10) + "-" + Date.now();
      const timer = setTimeout(() => {
        pendingRpcCalls.delete(callId);
        reject(new Error("MAIN world driver timed out (" + action + ")"));
      }, timeoutMs);
      pendingRpcCalls.set(callId, { resolve, reject, timer });
      postToMain(action, payload, callId);
    });
  }

  async function syncDriverConfigFromStorage() {
    if (disposed || !chrome.runtime?.id) return;
    try {
      const cfg = await chrome.storage.local.get([
        "driverMode",
        "customModelSelector",
        "customInputSelector",
        "customSendSelector",
        "customReplySelector"
      ]);
      postToMain("configure", cfg);
    } catch (_) {}
  }

  function onWindowMessage(event) {
    if (disposed || event.source !== window || event.origin !== window.location.origin) {
      return;
    }
    const data = event.data;
    if (!data || data.channel !== FROM_MAIN_CHANNEL) {
      return;
    }

    if (data.replyTo && pendingRpcCalls.has(data.replyTo)) {
      const pending = pendingRpcCalls.get(data.replyTo);
      pendingRpcCalls.delete(data.replyTo);
      clearTimeout(pending.timer);
      pending.resolve(data.payload);
      return;
    }

    if (data.payload && chrome.runtime?.id) {
      // Unthrottled MV3 Service Worker sleep relay for hidden/background tabs
      if (data.payload.type === "sw_sleep") {
        const sleepId = data.payload.sleep_id;
        const ms = Math.max(0, Math.min(Number(data.payload.ms) || 0, 10000));
        void (async () => {
          try {
            await chrome.runtime.sendMessage({ type: "GEB_SW_SLEEP", ms });
          } catch (_) {}
          postToMain("sw_sleep_done", { sleep_id: sleepId });
        })();
        return;
      }

      if (data.payload.type === "briefly_activate_tab") {
        const reqId = data.payload.req_id;
        const holdMs = Math.max(120, Math.min(Number(data.payload.hold_ms) || 240, 1500));
        void (async () => {
          let res = { ok: false };
          try {
            res = await chrome.runtime.sendMessage({
              type: "GEB_BRIEFLY_ACTIVATE_TAB",
              hold_ms: holdMs
            });
          } catch (_) {}
          postToMain("briefly_activate_tab_done", { req_id: reqId, ...(res || {}) });
        })();
        return;
      }

      if (data.payload.type === "driver_ready") {
        mainReady = true;
        if (typeof data.payload.driver_version === "string" && data.payload.driver_version) {
          mainDriverVersion = data.payload.driver_version;
        }
        flushQueuedPrompts();
      }

      if (data.payload.type === "reply_chunk") {
        replyReassembly.addChunk(
          data.payload.request_id,
          data.payload.chunk_index,
          data.payload.total_chunks,
          data.payload.data
        );
        return;
      }

      let outgoingPayload = data.payload;
      if (outgoingPayload.type === "request_complete" && outgoingPayload.chunked) {
        try {
          const mergedReply = replyReassembly.assemble(outgoingPayload.request_id, outgoingPayload.total_chunks);
          outgoingPayload = { ...outgoingPayload, reply: mergedReply, chunked: false };
        } catch (error) {
          outgoingPayload = {type: "request_error", request_id: outgoingPayload.request_id, error: error.message};
        }
      }

      void (async () => {
        try {
          // Chunk large replies (>24KB) across chrome.runtime.sendMessage to SW for HTTP callback upload
          const hasLargeMedia = ((outgoingPayload.output_images || []).length > 0) || ((outgoingPayload.output_files || []).length > 0);
          if (outgoingPayload.type === "request_complete" && hasLargeMedia) {
            const fullJson = JSON.stringify(outgoingPayload);
            if (fullJson.length > 196608) {
              const sliceLen = 196608;
              const totalJsonChunks = Math.ceil(fullJson.length / sliceLen);
              const streamKey = outgoingPayload.request_id + ":json";
              for (let i = 0; i < totalJsonChunks; i += 1) {
                await chrome.runtime.sendMessage({
                  type: "GEB_DRIVER_EVENT",
                  payload: {
                    type: "sw_reply_chunk",
                    request_id: outgoingPayload.request_id,
                    stream_key: streamKey,
                    chunk_index: i,
                    total_chunks: totalJsonChunks,
                    data: fullJson.slice(i * sliceLen, (i + 1) * sliceLen)
                  }
                });
              }
              await chrome.runtime.sendMessage({
                type: "GEB_DRIVER_EVENT",
                payload: {
                  type: "request_complete",
                  request_id: outgoingPayload.request_id,
                  stream_key: streamKey,
                  json_envelope_chunked: true,
                  total_chunks: totalJsonChunks
                }
              });
              return;
            }
          }
          const fullReplyStr = String(outgoingPayload.reply || "");
          if (outgoingPayload.type === "request_complete" && fullReplyStr.length > 24576) {
            const swSliceSize = 24576;
            const totalSwChunks = Math.ceil(fullReplyStr.length / swSliceSize);
            for (let i = 0; i < totalSwChunks; i += 1) {
              await chrome.runtime.sendMessage({
                type: "GEB_DRIVER_EVENT",
                payload: {
                  type: "sw_reply_chunk",
                  request_id: outgoingPayload.request_id,
                  chunk_index: i,
                  total_chunks: totalSwChunks,
                  data: fullReplyStr.slice(i * swSliceSize, (i + 1) * swSliceSize)
                }
              });
            }
            await chrome.runtime.sendMessage({
              type: "GEB_DRIVER_EVENT",
              payload: {
                ...outgoingPayload,
                reply: "",
                chunked: true,
                total_chunks: totalSwChunks
              }
            });
            return;
          }

          await chrome.runtime.sendMessage({
            type: "GEB_DRIVER_EVENT",
            payload: outgoingPayload
          });
        } catch (_) {}
      })();
    }
  }

  window.addEventListener("message", onWindowMessage);
  document.addEventListener("visibilitychange", updateVisibilityKeepalive);

  chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    if (disposed) return false;
    (async () => {
      const msgType = message && message.type ? message.type : "";

      if (msgType === "GEB_ATTACHMENT_CHUNK") {
        const chunkKey =
          message.att_key ||
          (message.request_id && message.attachment_id
            ? `${message.request_id}:${message.attachment_id}`
            : message.attachment_id || "");
        if (chunkKey) {
          attachmentReassembly.addChunk(
            chunkKey,
            message.chunk_index,
            message.total_chunks,
            message.data
          );
        }
        sendResponse({ ok: true });
        return;
      }

      if (msgType === "GEB_PROMPT_CHUNK") {
        promptReassembly.addChunk(
          message.request_id,
          message.chunk_index,
          message.total_chunks,
          message.data
        );
        sendResponse({ ok: true });
        return;
      }

      if (msgType === "GEB_EXECUTE_PROMPT") {
        if (!mainReady && document.readyState === "loading") {
          msgQueue.push(message);
          setTimeout(() => flushQueuedPrompts(), 500);
          sendResponse({ accepted: true, queued: true });
          return;
        }
        mainReady = true;
        dispatchPromptToMain(message);
        sendResponse({ accepted: true, queued: false });
        return;
      }

      if (msgType === "GEB_GET_VISIBILITY") {
        if (!mainDriverVersion) {
          try {
            const diag = await callMainAsync("diagnose", {}, 600);
            if (diag && typeof diag.driverVersion === "string" && diag.driverVersion) {
              mainDriverVersion = diag.driverVersion;
            }
          } catch (_) {}
        }
        sendResponse({
          ok: true,
          hidden: Boolean(document.hidden || document.visibilityState === "hidden"),
          visibilityState: document.visibilityState || "visible",
          driverVersion: mainDriverVersion || undefined
        });
        return;
      }

      if (msgType === "GEB_CANCEL_REQUEST") {
        const queuedIndex = msgQueue.findIndex((queued) => queued.request_id === message.request_id);
        if (queuedIndex !== -1) {
          msgQueue.splice(queuedIndex, 1);
        }
        postToMain("cancel_request", { request_id: message.request_id });
        sendResponse({ cancelled: true });
        return;
      }

      if (msgType === "GEB_DIAGNOSE_PAGE") {
        try {
          const result = await callMainAsync("diagnose", {}, 6000);
          sendResponse(result);
        } catch (err) {
          sendResponse({ ok: false, error: err.message });
        }
        return;
      }

      if (msgType === "GEB_TRIGGER_NEW_CHAT") {
        try {
          const result = await callMainAsync("new_chat", {}, 9000);
          sendResponse(result);
        } catch (err) {
          sendResponse({ ok: false, error: err.message });
        }
        return;
      }

      if (msgType === "GEB_SELECT_MODEL") {
        try {
          const result = await callMainAsync(
            "select_model",
            { target_model: message.target_model },
            8000
          );
          sendResponse(result);
        } catch (err) {
          sendResponse({ ok: false, error: err.message });
        }
        return;
      }

      if (msgType === "GEB_TAB_TAKEOVER_STATE") {
        if (message.state) {
          renderTakeoverState(message.state);
        }
        sendResponse({ ok: true });
        return;
      }

      sendResponse({ ok: true });
    })();
    return true;
  });

  chrome.storage.onChanged.addListener((changes, areaName) => {
    if (disposed || !chrome.runtime?.id) return;
    if (areaName === "local") {
      if (
        changes.driverMode ||
        changes.customModelSelector ||
        changes.customInputSelector ||
        changes.customSendSelector ||
        changes.customReplySelector
      ) {
        void syncDriverConfigFromStorage();
      }
      if (
        changes.enabled ||
        changes.maxParallelTabs ||
        changes.manualTabIds ||
        changes.manualSessionKeys
      ) {
        void refreshMyTakeoverState();
      }
    }
  });

  const heartbeatTimer = setInterval(() => {
    if (disposed || !chrome.runtime?.id) {
      clearInterval(heartbeatTimer);
      return;
    }
    void (async () => {
      try {
        await chrome.runtime.sendMessage({
          type: "GEB_DRIVER_EVENT",
          payload: { type: "driver_heartbeat" }
        });
      } catch (_) {}
    })();
  }, 25000);

  globalThis.__GEB_ISOLATED_CLEANUP__ = () => {
    disposed = true;
    if (indicatorUi && indicatorUi.host && typeof indicatorUi.host.remove === "function") {
      try {
        indicatorUi.host.remove();
      } catch (_) {}
      indicatorUi = null;
    }
    window.removeEventListener("message", onWindowMessage);
    document.removeEventListener("visibilitychange", updateVisibilityKeepalive);
    clearInterval(heartbeatTimer);
    if (hiddenKeepaliveTimer !== null) {
      clearInterval(hiddenKeepaliveTimer);
      hiddenKeepaliveTimer = null;
    }
    if (portRetryTimer !== null) {
      clearTimeout(portRetryTimer);
      portRetryTimer = null;
    }
    if (keepalivePort) {
      try {
        keepalivePort.disconnect();
      } catch (_) {}
      keepalivePort = null;
    }
    for (const [, p] of pendingRpcCalls) {
      clearTimeout(p.timer);
    }
    pendingRpcCalls.clear();
  };

  // Probe MAIN driver readiness immediately & sync config + visibility state
  callMainAsync("diagnose", {}, 1500)
    .then((diag) => {
      mainReady = true;
      if (diag && typeof diag.driverVersion === "string" && diag.driverVersion) {
        mainDriverVersion = diag.driverVersion;
      }
      flushQueuedPrompts();
    })
    .catch(() => {});
  void syncDriverConfigFromStorage();
  updateVisibilityKeepalive();
  void refreshMyTakeoverState();
})();
