// MAIN-world Hybrid Driver for Google Cloud Vertex AI Search / Agentspace (Gemini Enterprise)
// Combines:
//   1) Passive fetch/XHR RPC stream sniffer (extracts real-time StreamAssist / converse chunks + grounding links)
//   2) Deep open-Shadow-DOM piercing composer & incremental reply watcher (works out-of-the-box on message #1)
//   3) Optional in-page fetch replay once a chat RPC template is observed

(() => {
  const DRIVER_VERSION = "2.6.5";

  // Optional Trusted Types policy (gracefully guarded if CSP `trusted-types` header restricts custom policy names;
  // all DOM mutations below strictly use `replaceChildren` + `createElement` + `textContent` and ZERO `innerHTML`!)
  let trustedPolicy = null;
  try {
    if (window.trustedTypes && typeof window.trustedTypes.createPolicy === "function") {
      trustedPolicy = window.trustedTypes.createPolicy("geb-bridge", {
        createHTML: (s) => String(s || "")
      });
    }
  } catch (_) {
    trustedPolicy = null;
  }

  // Clean up previous MAIN-world message listener on hot-reload without double-patching fetch/XHR
  if (typeof globalThis.__GEB_MAIN_DRIVER_CLEANUP__ === "function") {
    try {
      globalThis.__GEB_MAIN_DRIVER_CLEANUP__();
    } catch (_) {}
  }
  globalThis.__GEB_MAIN_DRIVER_ACTIVE__ = true;

  const sharedState = globalThis.__GEB_MAIN_STATE__ || {
    nativeFetch: window.fetch.bind(window),
    nativeXhrOpen: XMLHttpRequest.prototype.open,
    nativeXhrSend: XMLHttpRequest.prototype.send,
    networkPatched: false,
    lastCapturedRpc: null,
    lastSelectedModel: "",
    onRpcCaptured: null,
    getActiveJob: () => null,
    consumeStream: null,
    parsePacket: null,
    checkChatRpc: null
  };
  globalThis.__GEB_MAIN_STATE__ = sharedState;

  const TO_MAIN_CHANNEL = "GEB_ISOLATED_TO_MAIN";
  const FROM_MAIN_CHANNEL = "GEB_MAIN_TO_ISOLATED";

  const driverConfig = {
    driverMode: "auto", // auto | dom | replay
    customModelSelector: "",
    customInputSelector: "",
    customSendSelector: "",
    customReplySelector: ""
  };

  let activeJob = null; // { requestId, cancelled, networkText, citations, networkDone, onNetworkDelta, onNetworkDone }

  function emitToIsolated(payload, replyTo = null) {
    window.postMessage(
      {
        channel: FROM_MAIN_CHANNEL,
        replyTo,
        payload
      },
      window.location.origin
    );
  }

  function logDriver(level, message, detail = undefined) {
    const prefix = `[GEB:${DRIVER_VERSION}]`;
    try {
      if (level === "error") {
        console.error(prefix, message, detail !== undefined ? detail : "");
      } else if (level === "warn") {
        console.warn(prefix, message, detail !== undefined ? detail : "");
      } else {
        console.log(prefix, message, detail !== undefined ? detail : "");
      }
    } catch (_) {}

    emitToIsolated({
      type: "log",
      level,
      message,
      captured_rpc: Boolean(sharedState.lastCapturedRpc)
    });
  }

  const pendingIsolatedSleep = new Map();
  const pendingBriefActivate = new Map();
  function requestIsolatedSleep(ms) {
    return new Promise((resolve) => {
      const sleepId = "slp-" + Math.random().toString(36).slice(2, 10) + "-" + Date.now();
      pendingIsolatedSleep.set(sleepId, resolve);
      emitToIsolated({ type: "sw_sleep", sleep_id: sleepId, ms });
    });
  }

  function requestBriefTabActivation(holdMs = 240) {
    return new Promise((resolve) => {
      const reqId = "act-" + Math.random().toString(36).slice(2, 10) + "-" + Date.now();
      let settled = false;
      const finish = (val) => {
        if (settled) return;
        settled = true;
        pendingBriefActivate.delete(reqId);
        resolve(val || { ok: false });
      };
      pendingBriefActivate.set(reqId, finish);
      emitToIsolated({ type: "briefly_activate_tab", req_id: reqId, hold_ms: holdMs });
      sleep(2000).then(() => finish({ ok: false, timeout: true })).catch(() => {});
    });
  }

  function unthrottledMicroTurn() {
    if (typeof MessageChannel === "function") {
      return new Promise((resolve) => {
        const mc = new MessageChannel();
        mc.port1.onmessage = () => resolve();
        mc.port2.postMessage(null);
      });
    }
    return Promise.resolve();
  }

  function sleep(ms) {
    const delay = Math.max(0, Number(ms) || 0);
    const isHidden = Boolean(document.hidden || document.visibilityState === "hidden");
    if (!isHidden) {
      return new Promise((resolve) => setTimeout(resolve, delay));
    }
    if (delay <= 16) {
      return unthrottledMicroTurn();
    }
    // When tab is hidden, Chrome throttles window.setTimeout up to 1,000-60,000ms.
    // Race MV3 Service Worker timer (exempt from tab throttling) against local setTimeout.
    return new Promise((resolve) => {
      let settled = false;
      const done = () => {
        if (settled) return;
        settled = true;
        resolve();
      };
      setTimeout(done, delay);
      requestIsolatedSleep(delay).then(done).catch(() => {});
    });
  }

  function nextAnimationFrame() {
    // CRITICAL: requestAnimationFrame NEVER fires when tab is hidden or window is occluded!
    if (document.hidden || document.visibilityState === "hidden") {
      return unthrottledMicroTurn();
    }
    return new Promise((resolve) => {
      let settled = false;
      const done = () => {
        if (settled) return;
        settled = true;
        resolve();
      };
      try {
        requestAnimationFrame(done);
      } catch (_) {
        done();
        return;
      }
      // Guard against Windows NativeWindowOcclusion pausing rAF when Chrome is behind Cursor/IDE
      setTimeout(done, 24);
    });
  }

  // =========================================================================
  // 1. Passive Network Stream Sniffer (fetch + XMLHttpRequest)
  // =========================================================================

  const CHAT_RPC_PATTERNS = [
    /streamAssist/i,
    /widgetStreamAssist/i,
    /:converse/i,
    /:assist/i,
    /:answer/i,
    /ConversationalSearch/i,
    /Agentspace.*batchexecute/i,
    /discoveryengine.*assist/i
  ];

  function isLikelyChatRpc(url, method, bodyText) {
    if (!url || String(method || "GET").toUpperCase() !== "POST") {
      return false;
    }
    if (CHAT_RPC_PATTERNS.some((re) => re.test(url))) {
      return true;
    }
    if (/batchexecute/i.test(url) && bodyText && bodyText.length > 20) {
      return /query|assist|conversation|message/i.test(bodyText);
    }
    return false;
  }

  function extractStructuredPieces(node, outTexts, outCitations) {
    if (!node || typeof node !== "object") {
      return;
    }
    // A thought flag applies to its entire subtree, not just its direct text field.
    if (node.thought === true || node.isThought === true || node.role === "thought" || node.type === "thought") return;
    if (Array.isArray(node)) {
      for (const item of node) {
        extractStructuredPieces(item, outTexts, outCitations);
      }
      return;
    }

    // Vertex AI Search / Agentspace groundedContent.content: { text, thought }
    if (node.groundedContent && node.groundedContent.content) {
      const contentObj = node.groundedContent.content;
      if (!contentObj.thought && typeof contentObj.text === "string" && contentObj.text) {
        outTexts.push({ text: contentObj.text, snapshot: false });
      }
    } else if (
      typeof node.summaryText === "string" &&
      node.summaryText &&
      !node.thought
    ) {
      outTexts.push({ text: node.summaryText, snapshot: true });
    } else if (
      node.role === "model" &&
      Array.isArray(node.parts)
    ) {
      for (const p of node.parts) {
        if (p && !p.thought && typeof p.text === "string" && p.text) {
          outTexts.push({ text: p.text, snapshot: false });
        }
      }
    }

    // Grounding / Enterprise Search references
    if (typeof node.uri === "string" && /^https?:\/\//i.test(node.uri)) {
      outCitations.push({
        url: node.uri,
        title: typeof node.title === "string" ? node.title : node.uri
      });
    }
    if (node.web && typeof node.web.uri === "string") {
      outCitations.push({
        url: node.web.uri,
        title: node.web.title || node.web.uri
      });
    }

    for (const key of Object.keys(node)) {
      if (["suggestedQuestions", "suggested_questions", "relatedQuestions"].includes(key)) continue;
      if (key === "groundedContent") {
        // Still inspect grounding metadata siblings, skip re-adding content.text
        const gc = node.groundedContent;
        if (gc && gc.textGroundingMetadata) {
          extractStructuredPieces(gc.textGroundingMetadata, outTexts, outCitations);
        }
        continue;
      }
      const child = node[key];
      if (child && typeof child === "object") {
        extractStructuredPieces(child, outTexts, outCitations);
      }
    }
  }

  function stripExplicitWebDecorations(text) {
    return String(text || "")
      .replace(/^[ \t]*\[\[(?:suggested_questions|section:suggested_questions)\]\][\s\S]*$/im, "")
      .replace(/\[\[section:[^\]]*\]\]/gi, "\n\n");
  }

  function replyFingerprint(text) {
    const stable = (value) => {
      if (Array.isArray(value)) return value.map(stable);
      if (value && typeof value === "object") {
        return Object.fromEntries(Object.keys(value).sort().map((key) => [key, stable(value[key])]));
      }
      return value;
    };
    // Formatting outside JSON strings may change in a final snapshot; commands may not.
    return stripExplicitWebDecorations(text).trim().replace(/\[TOOL_CALL\]((?:"(?:\\[\s\S]|[^"\\])*"|[^"])*?)\[\/TOOL_CALL\]/gi, (block, json) => {
      try { return "[TOOL_CALL]" + JSON.stringify(stable(JSON.parse(json))) + "[/TOOL_CALL]"; }
      catch (_) { return block; }
    });
  }

  function extractSnapshotSuffix(snapshotText, existingText) {
    const fpExisting = replyFingerprint(existingText);
    if (!fpExisting) return null;
    const fpSnapshot = replyFingerprint(snapshotText);
    if (fpSnapshot === fpExisting) return "";
    if (!fpSnapshot.startsWith(fpExisting)) return null;
    const closeTag = "[/TOOL_CALL]";
    const candidates = [];
    let pos = 0;
    while ((pos = snapshotText.indexOf(closeTag, pos)) !== -1) {
      pos += closeTag.length;
      candidates.push(pos);
    }
    for (let i = 0; i < snapshotText.length; i += 1) {
      if (snapshotText[i] === "\n") candidates.push(i);
    }
    for (const cut of candidates) {
      if (replyFingerprint(snapshotText.slice(0, cut)) === fpExisting) {
        return snapshotText.slice(cut);
      }
    }
    return null;
  }

  function parseJsonPacketIntoJob(jsonObj, job) {
    if (!job || job.cancelled) {
      return;
    }
    const texts = [];
    const refs = [];
    extractStructuredPieces(jsonObj, texts, refs);

    for (const ref of refs) {
      if (!job.citations.some((c) => c.url === ref.url)) {
        job.citations.push(ref);
      }
    }

    for (const piece of texts) {
      const combined = piece.text;
      if (combined) {
        if (piece.snapshot && job.networkText) {
          const snapshotSuffix = extractSnapshotSuffix(combined, job.networkText);
          if (snapshotSuffix !== null) {
            if (snapshotSuffix && stripExplicitWebDecorations(snapshotSuffix).trim()) {
              job.networkText += snapshotSuffix;
              job.onNetworkDelta(snapshotSuffix);
            }
            continue;
          }
        }
        // Distinguish cumulative full-text snapshots from incremental token deltas
        const isCumulativeGrowth =
          job.networkText.length >= 4 &&
          combined.length > job.networkText.length &&
          combined.startsWith(job.networkText);
        const isFinalEchoDuplicate =
          combined === job.networkText && combined.length >= 16;

        if (isFinalEchoDuplicate) {
          continue;
        }
        if (isCumulativeGrowth) {
          const suffix = combined.slice(job.networkText.length);
          if (suffix) {
            job.networkText += suffix;
            job.onNetworkDelta(suffix);
          }
        } else {
          job.networkText += combined;
          job.onNetworkDelta(combined);
        }
      }
    }
  }

  /**
   * Incrementally scans a streaming JSON array `[{...}, {...}]` or SSE/NDJSON stream
   * and invokes `onObject(parsed)` as soon as each top-level element closes.
   */
  async function consumeRpcReadableStream(readableStream, job) {
    const reader = readableStream.getReader();
    const cancelStream = () => { void reader.cancel().catch(() => {}); };
    job.cancelStream = cancelStream;
    const decoder = new TextDecoder("utf-8");
    let buffer = "";
    let cursor = 0;
    let inString = false;
    let escaped = false;
    let objDepth = 0;
    let arrayDepth = 0;
    let invalidPacket = false;
    let objStart = -1;
    let streamEnded = false;

    try {
      while (true) {
        const { value, done } = await reader.read();
        if (done) {
          streamEnded = true;
          break;
        }
        buffer += decoder.decode(value, { stream: true });

        while (cursor < buffer.length) {
          const ch = buffer[cursor];
          if (inString) {
            if (escaped) {
              escaped = false;
            } else if (ch === "\\") {
              escaped = true;
            } else if (ch === '"') {
              inString = false;
            }
          } else {
            if (ch === '"') {
              inString = true;
            } else if (ch === "{") {
              if (objDepth === 0) {
                objStart = cursor;
              }
              objDepth += 1;
            } else if (ch === "}") {
              if (objDepth > 0) {
                objDepth -= 1;
                if (objDepth === 0 && objStart >= 0) {
                  const candidate = buffer.slice(objStart, cursor + 1);
                  objStart = -1;
                  try {
                    const parsed = JSON.parse(candidate);
                    parseJsonPacketIntoJob(parsed, job);
                  } catch (_) { invalidPacket = true; }
                }
              }
            } else if (ch === "[") {
              arrayDepth += 1;
            } else if (ch === "]") {
              arrayDepth = Math.max(0, arrayDepth - 1);
            }
          }
          cursor += 1;
        }

        // Compact processed prefix when outside any top-level object
        if (objDepth === 0 && cursor > 8192) {
          buffer = buffer.slice(cursor);
          cursor = 0;
        }
      }
    } catch (_) {
      streamEnded = false;
    } finally {
      if (job && !job.cancelled) {
        if (streamEnded && objDepth === 0 && arrayDepth === 0 && !inString && !invalidPacket && job.networkText) {
          job.networkDone = true;
          job.onNetworkDone();
        } else if (!streamEnded || objDepth > 0 || arrayDepth > 0 || inString || invalidPacket) {
          job.networkError = "网页响应流中断或数据不完整。";
          logDriver("warn", job.networkError);
        }
      }
      reader.releaseLock();
      if (job.cancelStream === cancelStream) delete job.cancelStream;
    }
  }

  const nativeFetch = sharedState.nativeFetch;
  sharedState.getActiveJob = () => activeJob;
  sharedState.consumeStream = consumeRpcReadableStream;
  sharedState.parsePacket = parseJsonPacketIntoJob;
  sharedState.checkChatRpc = isLikelyChatRpc;
  sharedState.onRpcCaptured = () => {
    emitToIsolated({ type: "driver_ready", captured_rpc: true });
  };

  if (!sharedState.networkPatched || sharedState.networkPatchVersion !== DRIVER_VERSION) {
    sharedState.networkPatched = true;
    sharedState.networkPatchVersion = DRIVER_VERSION;

    window.fetch = async function patchedFetch(input, init) {
      let url = "";
      let method = (init && init.method) || "GET";
      let bodyText = "";

      try {
        if (typeof input === "string") {
          url = input;
        } else if (input instanceof URL) {
          url = input.href;
        } else if (input && typeof input.url === "string") {
          url = input.url;
          method = input.method || method;
        }
        if (init && typeof init.body === "string") {
          bodyText = init.body;
        }
      } catch (_) {}

      const matchedRpc =
        sharedState.checkChatRpc && sharedState.checkChatRpc(url, method, bodyText);
      if (matchedRpc && bodyText) {
        const headersCopy = {};
        try {
          const rawHeaders = (init && init.headers) || (input && input.headers);
          if (rawHeaders instanceof Headers) {
            rawHeaders.forEach((v, k) => {
              headersCopy[k] = v;
            });
          } else if (Array.isArray(rawHeaders)) {
            for (const [k, v] of rawHeaders) {
              headersCopy[k] = v;
            }
          } else if (rawHeaders && typeof rawHeaders === "object") {
            Object.assign(headersCopy, rawHeaders);
          }
        } catch (_) {}

        sharedState.lastCapturedRpc = {
          url,
          method: String(method).toUpperCase(),
          headers: headersCopy,
          bodyText,
          capturedAt: Date.now()
        };
        if (typeof sharedState.onRpcCaptured === "function") {
          sharedState.onRpcCaptured();
        }
      }

      // Bind before awaiting headers so a late response cannot attach to the next job.
      const curJob = sharedState.getActiveJob ? sharedState.getActiveJob() : null;
      if (matchedRpc && curJob && !curJob.cancelled) curJob.submissionStarted = true;
      let response;
      try {
        response = await sharedState.nativeFetch(input, init);
      } catch (error) {
        if (matchedRpc && curJob && !curJob.cancelled) {
          curJob.networkError = "网页请求失败，连接已中断。";
        }
        throw error;
      }
      if (matchedRpc && curJob && !curJob.cancelled && response && response.ok === false) {
        curJob.networkError = `网页请求失败 (HTTP ${response.status})。`;
      }

      if (matchedRpc && curJob && !curJob.cancelled && response && response.body) {
        try {
          const cloned = response.clone();
          if (cloned.body && sharedState.consumeStream) {
            void sharedState.consumeStream(cloned.body, curJob);
          }
        } catch (_) {}
      }

      return response;
    };

    XMLHttpRequest.prototype.open = function patchedOpen(method, url, ...rest) {
      this.__gebMethod = method;
      this.__gebUrl = String(url || "");
      return sharedState.nativeXhrOpen.call(this, method, url, ...rest);
    };

    XMLHttpRequest.prototype.send = function patchedSend(body) {
      const bodyStr = typeof body === "string" ? body : "";
      const curJob = sharedState.getActiveJob ? sharedState.getActiveJob() : null;
      if (
        sharedState.checkChatRpc &&
        sharedState.checkChatRpc(this.__gebUrl, this.__gebMethod, bodyStr) &&
        curJob
      ) {
        const jobRef = curJob;
        jobRef.submissionStarted = true;
        let lastProcessedIdx = 0;
        this.addEventListener("readystatechange", () => {
          if (this.readyState >= 3 && typeof this.responseText === "string") {
            const fresh = this.responseText.slice(lastProcessedIdx);
            if (fresh && this.readyState === 4) {
              lastProcessedIdx = this.responseText.length;
              try {
                const cleaned = this.responseText.replace(/^\)\]\}'\s*/, "");
                const parsed = JSON.parse(cleaned);
                if (sharedState.parsePacket) {
                  sharedState.parsePacket(parsed, jobRef);
                }
                if (jobRef.networkText) {
                  jobRef.networkDone = true;
                  jobRef.onNetworkDone();
                }
              } catch (_) {}
            }
          }
        });
      }
      return sharedState.nativeXhrSend.call(this, body);
    };
  }

  // =========================================================================
  // 2. Deep Shadow-DOM Piercing Utilities
  // =========================================================================

  function collectDeepRoots(root = document) {
    const roots = [root];
    const queue = [root];
    while (queue.length > 0) {
      const current = queue.shift();
      if (!current || !current.querySelectorAll) {
        continue;
      }
      const all = current.querySelectorAll("*");
      for (let i = 0; i < all.length; i += 1) {
        const el = all[i];
        if (el.shadowRoot) {
          roots.push(el.shadowRoot);
          queue.push(el.shadowRoot);
        }
        if (el.tagName === "IFRAME") {
          try {
            if (el.contentDocument) {
              roots.push(el.contentDocument);
              queue.push(el.contentDocument);
            }
          } catch (_) {}
        }
      }
    }
    return roots;
  }

  function querySelectorAllDeep(selector, root = document) {
    if (!selector || !selector.trim()) {
      return [];
    }
    const results = [];
    const roots = collectDeepRoots(root);
    for (const r of roots) {
      try {
        const matched = r.querySelectorAll(selector);
        for (let i = 0; i < matched.length; i += 1) {
          results.push(matched[i]);
        }
      } catch (_) {}
    }
    return results;
  }

  function isElementVisible(el) {
    if (!el || !el.getBoundingClientRect) {
      return false;
    }
    const style = window.getComputedStyle(el);
    if (style.display === "none" || style.visibility === "hidden" || Number(style.opacity) === 0) {
      return false;
    }
    const rect = el.getBoundingClientRect();
    return rect.width > 8 && rect.height > 8;
  }

  function getShadowHostChainTagString(el) {
    const tags = [];
    let cur = el;
    let guard = 0;
    while (cur && guard < 20) {
      guard += 1;
      if (cur.tagName) {
        tags.push(cur.tagName.toLowerCase());
      }
      if (cur.parentElement) {
        cur = cur.parentElement;
      } else {
        const rootNode = cur.getRootNode ? cur.getRootNode() : null;
        cur = rootNode && rootNode.host ? rootNode.host : null;
      }
    }
    return tags.join(" < ");
  }

  function findComposerInput() {
    if (driverConfig.customInputSelector) {
      const customMatches = querySelectorAllDeep(driverConfig.customInputSelector).filter(
        isElementVisible
      );
      if (customMatches.length > 0) {
        return customMatches[0];
      }
    }

    const candidates = querySelectorAllDeep(
      [
        '.ProseMirror[contenteditable="true"]',
        'div[contenteditable="true"][role="textbox"]',
        'div[contenteditable="plaintext-only"]',
        '[contenteditable="true"]',
        "textarea",
        'input[type="text"]'
      ].join(", ")
    ).filter((el) => isElementVisible(el) && !el.disabled && !el.readOnly);

    if (candidates.length === 0) {
      return null;
    }

    const scored = candidates.map((el) => {
      let score = 0;
      const chain = getShadowHostChainTagString(el);
      const cls = String(el.className || "");
      const aria = String(el.getAttribute("aria-label") || el.getAttribute("placeholder") || "");
      const rect = el.getBoundingClientRect();

      if (cls.includes("ProseMirror")) score += 60;
      if (chain.includes("ucs-prosemirror-editor")) score += 55;
      if (chain.includes("ucs-chat") || chain.includes("ucs-conversation")) score += 45;
      if (chain.includes("ucs-search-box") || chain.includes("rich-textarea")) score += 35;
      if (el.getAttribute("role") === "textbox") score += 20;
      if (el.tagName === "TEXTAREA") score += 18;
      if (/ask|chat|message|prompt|follow|search|提问|输入|消息|询问/i.test(aria)) score += 25;
      // Chat input is typically wider and lower on screen when in a conversation
      if (rect.width >= 240) score += 15;
      if (rect.top > window.innerHeight * 0.35) score += 10;

      return { el, score };
    });

    scored.sort((a, b) => b.score - a.score);
    return scored[0].el;
  }

  function readButtonIconAndLabelText(btn) {
    if (!btn) return "";
    const parts = [
      getElementTooltipOrLabel(btn),
      btn.getAttribute ? btn.getAttribute("data-testid") || "" : "",
      btn.getAttribute ? btn.getAttribute("data-test-id") || "" : "",
      btn.getAttribute ? btn.getAttribute("name") || "" : "",
      btn.getAttribute ? btn.getAttribute("icon") || "" : "",
      getDeepVisibleText(btn),
      btn.textContent || ""
    ];
    try {
      const icons = querySelectorAllDeep(
        "md-icon, mat-icon, .google-symbols, .material-symbols-outlined, .material-icons, svg, [slot=\"icon\"]",
        btn.shadowRoot || btn
      );
      for (const ic of icons) {
        parts.push(
          ic.getAttribute ? ic.getAttribute("aria-label") || ic.getAttribute("name") || ic.getAttribute("data-icon") || "" : "",
          ic.textContent || ""
        );
      }
    } catch (_) {}
    return parts.filter(Boolean).join(" ");
  }

  function findSendButton(inputEl) {
    if (driverConfig.customSendSelector) {
      const customBtns = querySelectorAllDeep(driverConfig.customSendSelector).filter(
        isElementVisible
      );
      if (customBtns.length > 0) {
        return customBtns[0];
      }
    }

    const sendHintRe =
      /\b(send|submit|ask|run|arrow_upward|arrow_forward|north|submitting)\b|发送|提交|提问/i;
    const excludeHintRe =
      /attach|upload|mic|voice|audio|stop|clear|reset|copy|more|menu|settings|new chat|model|argon|carbon|pro|flash|auto|上传|语音|停止|清空|新建|模型/i;

    // Walk up shadow/DOM ancestors near inputEl first so we pick the composer's own button
    let container = inputEl;
    for (let depth = 0; depth < 9 && container; depth += 1) {
      const scopeRoot = container.shadowRoot || container;
      const localButtons = querySelectorAllDeep(
        'button, md-icon-button, md-filled-icon-button, ucs-icon-button, [role="button"]',
        scopeRoot
      ).filter(
        (btn) =>
          isElementVisible(btn) &&
          !btn.disabled &&
          btn.getAttribute("aria-disabled") !== "true" &&
          !btn.hasAttribute("disabled")
      );

      const matched = localButtons.filter((btn) => {
        const label = readButtonIconAndLabelText(btn);
        if (excludeHintRe.test(label)) {
          return false;
        }
        if (sendHintRe.test(label) || btn.getAttribute("type") === "submit") {
          return true;
        }
        return false;
      });

      if (matched.length > 0) {
        return matched[matched.length - 1];
      }

      if (container.parentElement) {
        container = container.parentElement;
      } else {
        const rn = container.getRootNode ? container.getRootNode() : null;
        container = rn && rn.host ? rn.host : null;
      }
    }

    return null;
  }

  function readComposerCurrentText(inputEl) {
    if (!inputEl) return "";
    const tag = inputEl.tagName ? inputEl.tagName.toUpperCase() : "";
    if (tag === "TEXTAREA" || tag === "INPUT") {
      return String(inputEl.value || "").trim();
    }
    return (
      getDeepVisibleText(inputEl).trim() ||
      String(inputEl.innerText || inputEl.textContent || "").trim()
    );
  }

  async function fillInputWithPrompt(inputEl, promptText) {
    inputEl.focus();
    await nextAnimationFrame();

    const tag = inputEl.tagName ? inputEl.tagName.toUpperCase() : "";
    if (tag === "TEXTAREA" || tag === "INPUT") {
      const proto =
        tag === "TEXTAREA" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
      const descriptor = Object.getOwnPropertyDescriptor(proto, "value");
      if (descriptor && descriptor.set) {
        descriptor.set.call(inputEl, promptText);
      } else {
        inputEl.value = promptText;
      }
      inputEl.dispatchEvent(
        new InputEvent("input", {
          bubbles: true,
          composed: true,
          inputType: "insertText",
          data: promptText.length > 4000 ? null : promptText
        })
      );
      inputEl.dispatchEvent(new Event("change", { bubbles: true, composed: true }));
      return;
    }

    // ContentEditable / ProseMirror path (100% TrustedTypes-compliant: ZERO innerHTML / HTML strings!)
    const isHiddenOrUnfocused = Boolean(
      document.hidden ||
        document.visibilityState === "hidden" ||
        (typeof document.hasFocus === "function" && !document.hasFocus())
    );
    const isLargePrompt = promptText.length > 3000;
    const readEditorText = () => readComposerCurrentText(inputEl);

    let insertedCleanly = false;

    // Select all existing text first so insertion replaces any stale draft
    const sel = window.getSelection ? window.getSelection() : null;
    if (sel && typeof document.createRange === "function") {
      try {
        const selectRange = document.createRange();
        selectRange.selectNodeContents(inputEl);
        sel.removeAllRanges();
        sel.addRange(selectRange);
      } catch (_) {}
    }

    // Strategy 1: Native `execCommand("insertText")` only for short/medium prompts when focused
    // (For >3000 chars, execCommand synchronously lays out every line in ProseMirror and freezes the tab!)
    if (!isHiddenOrUnfocused && !isLargePrompt) {
      try {
        inputEl.dispatchEvent(
          new InputEvent("beforeinput", {
            bubbles: true,
            composed: true,
            cancelable: true,
            inputType: "insertText",
            data: promptText
          })
        );
        insertedCleanly = document.execCommand("insertText", false, promptText);
      } catch (_) {
        insertedCleanly = false;
      }
    }

    // Strategy 2: Synthetic plain-text `ClipboardEvent("paste")` + `InputEvent`
    // ProseMirror handles `paste` with `text/plain` natively in memory in a single transaction!
    if (
      (!insertedCleanly || (promptText.trim() && !readEditorText())) &&
      typeof DataTransfer === "function" &&
      typeof ClipboardEvent === "function"
    ) {
      try {
        const dt = new DataTransfer();
        dt.setData("text/plain", promptText);
        const pasteEv = new ClipboardEvent("paste", {
          bubbles: true,
          composed: true,
          cancelable: true,
          clipboardData: dt
        });
        inputEl.dispatchEvent(pasteEv);
        await nextAnimationFrame();
        if (readEditorText()) {
          insertedCleanly = true;
        }
      } catch (_) {}
    }

    // Strategy 3: Safe `replaceChildren` + `cloneNode` / `textContent` batched paragraph nodes + `InputEvent`
    if (!insertedCleanly || (promptText.trim() && !readEditorText())) {
      const frag = document.createDocumentFragment
        ? document.createDocumentFragment()
        : null;
      const lines = promptText.split("\n");
      const batchSize = lines.length > 48 ? Math.max(64, Math.ceil(lines.length / 24)) : 1;
      const protoP = document.createElement("p");

      if (typeof inputEl.replaceChildren === "function") {
        inputEl.replaceChildren();
      } else {
        while (inputEl.firstChild) {
          inputEl.removeChild(inputEl.firstChild);
        }
      }

      const targetContainer = frag || inputEl;
      for (let idx = 0; idx < lines.length; idx += batchSize) {
        const p =
          typeof protoP.cloneNode === "function"
            ? protoP.cloneNode(false)
            : document.createElement("p");
        const sliceText =
          batchSize === 1 ? lines[idx] : lines.slice(idx, idx + batchSize).join("\n");
        if (sliceText) {
          p.textContent = sliceText;
        } else {
          p.appendChild(document.createElement("br"));
        }
        targetContainer.appendChild(p);
      }
      if (frag) {
        inputEl.appendChild(frag);
      }

      const eventData = promptText.length > 4000 ? null : promptText;
      inputEl.dispatchEvent(
        new InputEvent("beforeinput", {
          bubbles: true,
          composed: true,
          cancelable: true,
          inputType: "insertText",
          data: eventData
        })
      );
      inputEl.dispatchEvent(
        new InputEvent("input", {
          bubbles: true,
          composed: true,
          inputType: "insertText",
          data: eventData
        })
      );
      inputEl.dispatchEvent(new Event("change", { bubbles: true, composed: true }));
    }
  }

  function dispatchEnterKeySequence(targetEl) {
    const enterOpts = {
      key: "Enter",
      code: "Enter",
      keyCode: 13,
      which: 13,
      bubbles: true,
      composed: true,
      cancelable: true
    };
    targetEl.dispatchEvent(new KeyboardEvent("keydown", enterOpts));
    targetEl.dispatchEvent(new KeyboardEvent("keypress", enterOpts));
    targetEl.dispatchEvent(new KeyboardEvent("keyup", enterOpts));
  }

  async function submitComposer(inputEl) {
    // Allow Lit/Angular/ProseMirror reactive cycle to enable the Send button
    await nextAnimationFrame();
    let sendBtn = null;
    for (let waitIdx = 0; waitIdx < 12; waitIdx += 1) {
      sendBtn = findSendButton(inputEl);
      if (sendBtn) break;
      await sleep(80);
    }

    if (sendBtn) {
      sendBtn.click();
      return "button_click";
    }

    dispatchEnterKeySequence(inputEl);
    return "enter_keypress";
  }

  const BASE64_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

  function decodeBase64ToUint8Array(b64) {
    const clean = String(b64 || "").replace(/[^A-Za-z0-9+/=]/g, "");
    if (!clean) return new Uint8Array(0);
    if (typeof atob === "function") {
      const bin = atob(clean);
      const out = new Uint8Array(bin.length);
      for (let i = 0; i < bin.length; i += 1) {
        out[i] = bin.charCodeAt(i) & 0xff;
      }
      return out;
    }
    const bytes = [];
    for (let i = 0; i < clean.length; i += 4) {
      const c0 = BASE64_ALPHABET.indexOf(clean[i]);
      const c1 = BASE64_ALPHABET.indexOf(clean[i + 1]);
      const c2 = clean[i + 2] === "=" ? -1 : BASE64_ALPHABET.indexOf(clean[i + 2]);
      const c3 = clean[i + 3] === "=" ? -1 : BASE64_ALPHABET.indexOf(clean[i + 3]);
      if (c0 < 0 || c1 < 0) break;
      const triple = (c0 << 18) | (c1 << 12) | ((c2 < 0 ? 0 : c2) << 6) | (c3 < 0 ? 0 : c3);
      bytes.push((triple >> 16) & 0xff);
      if (c2 >= 0) bytes.push((triple >> 8) & 0xff);
      if (c3 >= 0) bytes.push(triple & 0xff);
    }
    return new Uint8Array(bytes);
  }

  function constructBrowserFile(bytes, filename, mimeType) {
    const safeName = String(filename || "attachment.bin");
    const safeMime = String(mimeType || "application/octet-stream");
    const now = Date.now();
    if (typeof File === "function") {
      return new File([bytes], safeName, { type: safeMime, lastModified: now });
    }
    if (typeof Blob === "function") {
      const blob = new Blob([bytes], { type: safeMime });
      try {
        Object.defineProperty(blob, "name", { value: safeName, configurable: true });
        Object.defineProperty(blob, "lastModified", { value: now, configurable: true });
      } catch (_) {}
      return blob;
    }
    return {
      name: safeName,
      size: bytes.byteLength,
      type: safeMime,
      lastModified: now,
      _bytes: bytes
    };
  }

  function buildDataTransferForFiles(files) {
    if (typeof DataTransfer === "function") {
      const dt = new DataTransfer();
      for (const file of files) {
        dt.items.add(file);
      }
      return dt;
    }
    const fileList = files.slice();
    fileList.item = (idx) => fileList[idx] || null;
    return {
      dropEffect: "copy",
      effectAllowed: "all",
      files: fileList,
      items: files.map((f) => ({
        kind: "file",
        type: f.type || "application/octet-stream",
        getAsFile: () => f
      })),
      types: ["Files"],
      getData: () => ""
    };
  }

  function getComposerScopeRoots(inputEl) {
    const roots = [];
    const seen = new Set();
    let cur = inputEl;
    for (let depth = 0; depth < 10 && cur; depth += 1) {
      const scope = cur.shadowRoot || cur;
      if (scope && !seen.has(scope)) {
        seen.add(scope);
        roots.push(scope);
      }
      if (cur.parentElement) {
        cur = cur.parentElement;
      } else {
        const rn = cur.getRootNode ? cur.getRootNode() : null;
        cur = rn && rn.host ? rn.host : null;
      }
    }
    return roots;
  }

  function fileMatchesAcceptToken(file, token) {
    const t = String(token || "").trim().toLowerCase();
    if (!t || t === "*/*" || t === "*") return true;
    const mime = String(file.type || "").toLowerCase();
    const name = String(file.name || "").toLowerCase();
    if (t.startsWith(".")) {
      return name.endsWith(t);
    }
    if (t.endsWith("/*")) {
      const prefix = t.slice(0, -1);
      return mime.startsWith(prefix);
    }
    return mime === t;
  }

  function scoreFileInputCandidate(el, files, composerHost) {
    if (!el || el.disabled || el.hasAttribute("disabled")) return -1;
    let score = 10;
    if (composerHost && containsDeep(composerHost, el)) {
      score += 50;
    }
    if (el.multiple && files.length > 1) {
      score += 15;
    }
    const acceptRaw = String(el.getAttribute("accept") || "").trim();
    if (!acceptRaw) {
      score += 20;
    } else {
      const tokens = acceptRaw.split(",").map((s) => s.trim()).filter(Boolean);
      const allMatch = files.every((f) => tokens.some((tok) => fileMatchesAcceptToken(f, tok)));
      if (allMatch) {
        score += 25;
      } else {
        score -= 30;
      }
    }
    return score;
  }

  function findBestFileInput(inputEl, files) {
    const composerHost = getComposerRootHost(inputEl);
    const candidates = [];
    const seen = new Set();
    for (const root of getComposerScopeRoots(inputEl)) {
      for (const el of querySelectorAllDeep('input[type="file"]', root)) {
        if (!seen.has(el)) {
          seen.add(el);
          candidates.push(el);
        }
      }
    }
    for (const el of querySelectorAllDeep('input[type="file"]')) {
      if (!seen.has(el)) {
        seen.add(el);
        candidates.push(el);
      }
    }
    const scored = candidates
      .map((el) => ({ el, score: scoreFileInputCandidate(el, files, composerHost) }))
      .filter((item) => item.score >= 0);
    if (scored.length === 0) return null;
    scored.sort((a, b) => b.score - a.score);
    return scored[0].el;
  }

  function findComposerAttachButton(inputEl) {
    const attachHintRe =
      /\b(attach|attach_file|attachment|upload|upload_file|add_photo_alternate|add_circle|add_box|paperclip|plus|add)\b|[+＋]|上传|附件|添加文件|添加/i;
    const excludeRe =
      /\b(send|submit|ask|run|stop|mic|voice|audio_magic_eraser|add_comment|chat_add_on|new chat|new session|reset|clear|model|argon|carbon|pro|flash|auto)\b|发送|提交|停止|语音|新建|新对话|模型/i;

    for (const root of getComposerScopeRoots(inputEl)) {
      const buttons = querySelectorAllDeep(
        'button, md-icon-button, md-filled-tonal-icon-button, md-outlined-button, ucs-icon-button, [role="button"]',
        root
      ).filter(
        (btn) =>
          isElementVisible(btn) &&
          !btn.disabled &&
          btn.getAttribute("aria-disabled") !== "true" &&
          !btn.hasAttribute("disabled")
      );
      for (const btn of buttons) {
        const label = readButtonIconAndLabelText(btn).trim();
        if (excludeRe.test(label)) continue;
        if (attachHintRe.test(label)) {
          return btn;
        }
      }
    }
    return null;
  }

  function findLocalUploadMenuOption(triggerBtn) {
    const localUploadRe =
      /\b(upload\s*files?|upload\s*from\s*computer|from\s*computer|from\s*your\s*computer|local\s*files?|choose\s*files?|browse\s*files?|upload\s*image|upload\s*media|computer|device|upload)\b|本地文件|从电脑上传|从设备上传|上传文件|选择文件|上传图片|上传/i;
    const cloudExcludeRe =
      /\b(google\s*drive|drive|onedrive|sharepoint|box|dropbox|github|jira|confluence|cloud|connector|data\s*source)\b|云端硬盘|数据源|连接器/i;
    const items = querySelectorAllDeep(
      'mat-menu-item, md-menu-item, md-list-item, mwc-list-item, ucs-menu-item, [role="menuitem"], .mat-mdc-menu-item, button'
    ).filter(
      (el) =>
        isElementVisible(el) &&
        el !== triggerBtn &&
        !containsDeep(el, triggerBtn) &&
        !el.disabled &&
        el.getAttribute("aria-disabled") !== "true"
    );
    for (const item of items) {
      const txt = `${getDeepVisibleText(item)} ${getElementTooltipOrLabel(item)}`.trim();
      if (!txt || cloudExcludeRe.test(txt)) continue;
      if (localUploadRe.test(txt)) {
        return item;
      }
    }
    return null;
  }

  function countComposerAttachmentIndicators(inputEl) {
    const composerHost = getComposerRootHost(inputEl);
    const roots = composerHost ? [composerHost.shadowRoot || composerHost] : getComposerScopeRoots(inputEl).slice(0, 5);
    const seen = new Set();
    const chipSelector = [
      "ucs-file-chip",
      "ucs-attachment-chip",
      "ucs-attachment",
      "ucs-uploaded-file",
      "file-chip",
      "attachment-chip",
      "md-input-chip",
      "mat-chip-row",
      '[data-test-id*="attachment" i]',
      '[data-testid*="attachment" i]',
      '[data-test-id*="file-chip" i]',
      '[data-testid*="file-chip" i]',
      '[class*="attachment-chip" i]',
      '[class*="file-chip" i]',
      '[class*="uploaded-file" i]'
    ].join(", ");
    for (const root of roots) {
      for (const chip of querySelectorAllDeep(chipSelector, root)) {
        if (isElementVisible(chip)) {
          seen.add(chip);
        }
      }
    }
    return seen.size;
  }

  function isComposerUploadingFiles(inputEl) {
    const composerHost = getComposerRootHost(inputEl);
    const roots = composerHost ? [composerHost.shadowRoot || composerHost] : getComposerScopeRoots(inputEl).slice(0, 5);
    const progressSelector = [
      "md-circular-progress",
      "md-linear-progress",
      "mat-progress-spinner",
      "mat-spinner",
      "mat-progress-bar",
      '[role="progressbar"]',
      '[aria-busy="true"]',
      '[data-upload-state="uploading"]',
      '[class*="uploading" i]'
    ].join(", ");
    for (const root of roots) {
      const active = querySelectorAllDeep(progressSelector, root).filter(isElementVisible);
      if (active.length > 0) {
        return true;
      }
    }
    return false;
  }

  async function assignFilesToInputElement(fileInput, files) {
    const batches = !fileInput.multiple && files.length > 1 ? files.map((f) => [f]) : [files];
    for (let idx = 0; idx < batches.length; idx += 1) {
      const dt = buildDataTransferForFiles(batches[idx]);
      let assigned = false;
      try {
        const desc =
          typeof HTMLInputElement !== "undefined" && HTMLInputElement.prototype
            ? Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "files")
            : null;
        if (desc && typeof desc.set === "function") {
          desc.set.call(fileInput, dt.files);
          assigned = true;
        }
      } catch (_) {}
      if (!assigned) {
        try {
          fileInput.files = dt.files;
          assigned = true;
        } catch (_) {
          try {
            Object.defineProperty(fileInput, "files", {
              value: dt.files,
              configurable: true,
              writable: true
            });
          } catch (__) {}
        }
      }
      fileInput.dispatchEvent(new Event("input", { bubbles: true, composed: true }));
      fileInput.dispatchEvent(new Event("change", { bubbles: true, composed: true }));
      if (idx + 1 < batches.length) {
        await sleep(90);
      }
    }
  }

  function dispatchPasteAndDropFallback(inputEl, files) {
    const dt = buildDataTransferForFiles(files);
    const composerHost = getComposerRootHost(inputEl) || inputEl;
    let dispatched = false;
    try {
      inputEl.focus();
    } catch (_) {}

    try {
      let pasteEv;
      if (typeof ClipboardEvent === "function") {
        pasteEv = new ClipboardEvent("paste", {
          bubbles: true,
          composed: true,
          cancelable: true,
          clipboardData: dt
        });
      } else {
        pasteEv = new Event("paste", { bubbles: true, composed: true, cancelable: true });
      }
      if (!pasteEv.clipboardData) {
        try {
          Object.defineProperty(pasteEv, "clipboardData", { value: dt, configurable: true });
        } catch (_) {}
      }
      inputEl.dispatchEvent(pasteEv);
      dispatched = true;
    } catch (_) {}

    try {
      for (const type of ["dragenter", "dragover", "drop"]) {
        let dragEv;
        if (typeof DragEvent === "function") {
          dragEv = new DragEvent(type, {
            bubbles: true,
            composed: true,
            cancelable: true,
            dataTransfer: dt
          });
        } else {
          dragEv = new Event(type, { bubbles: true, composed: true, cancelable: true });
        }
        if (!dragEv.dataTransfer) {
          try {
            Object.defineProperty(dragEv, "dataTransfer", { value: dt, configurable: true });
          } catch (_) {}
        }
        composerHost.dispatchEvent(dragEv);
      }
      dispatched = true;
    } catch (_) {}

    return dispatched;
  }

  async function waitForComposerAttachmentsReady(inputEl, totalBytes = 0, onProgress = null) {
    await sleep(120);
    const maxWaitMs = Math.min(60000, Math.max(8000, Math.ceil(totalBytes / 2048)));
    const deadline = Date.now() + maxWaitMs;
    let lastProgress = Date.now();
    let quietChecks = 0;
    while (Date.now() < deadline) {
      if (onProgress && Date.now() - lastProgress >= 7500) { lastProgress = Date.now(); onProgress(); }
      const liveInput = findComposerInput() || inputEl;
      const uploading = isComposerUploadingFiles(liveInput);
      if (!uploading) {
        quietChecks += 1;
        if (quietChecks >= 2) {
          break;
        }
      } else {
        quietChecks = 0;
      }
      await sleep(120);
    }
  }

  async function uploadAttachmentsToComposer(inputEl, attachmentSpecs, onProgress = null) {
    if (!Array.isArray(attachmentSpecs) || attachmentSpecs.length === 0) {
      return { uploaded: 0, method: "none" };
    }
    const files = [];
    let totalBytes = 0;
    for (let i = 0; i < attachmentSpecs.length; i += 1) {
      const spec = attachmentSpecs[i] || {};
      const bytes =
        spec.bytes instanceof Uint8Array
          ? spec.bytes
          : decodeBase64ToUint8Array(spec.data_b64 || "");
      totalBytes += bytes.byteLength;
      files.push(
        constructBrowserFile(
          bytes,
          spec.filename || `attachment-${i + 1}.bin`,
          spec.mime_type || "application/octet-stream"
        )
      );
    }

    let uploadMethod = "";
    let fileInput = findBestFileInput(inputEl, files);
    if (fileInput) {
      await assignFilesToInputElement(fileInput, files);
      uploadMethod = "file_input";
    } else {
      const attachBtn = findComposerAttachButton(inputEl);
      if (attachBtn) {
        const hasInputProto =
          typeof HTMLInputElement !== "undefined" &&
          HTMLInputElement.prototype &&
          typeof HTMLInputElement.prototype.click === "function";
        const origClick = hasInputProto ? HTMLInputElement.prototype.click : null;
        let capturedInput = null;
        if (hasInputProto) {
          HTMLInputElement.prototype.click = function (...args) {
            if (this && String(this.type || "").toLowerCase() === "file") {
              capturedInput = this;
              return undefined;
            }
            return origClick.apply(this, args);
          };
        }
        try {
          attachBtn.click();
          await sleep(110);
          if (!capturedInput) {
            capturedInput = findBestFileInput(inputEl, files);
          }
          if (!capturedInput) {
            const menuOpt = findLocalUploadMenuOption(attachBtn);
            if (menuOpt) {
              menuOpt.click();
              await sleep(110);
              if (!capturedInput && isElementVisible(menuOpt)) {
                dispatchRealisticClick(menuOpt);
                await sleep(110);
              }
            }
          }
          if (!capturedInput) {
            capturedInput = findBestFileInput(inputEl, files);
          }
        } finally {
          if (hasInputProto && origClick) {
            HTMLInputElement.prototype.click = origClick;
          }
        }
        if (capturedInput) {
          await assignFilesToInputElement(capturedInput, files);
          uploadMethod = "attach_button_file_input";
          await dismissTransientOverlays(null);
        } else {
          await dismissTransientOverlays(null);
        }
      }
    }

    if (!uploadMethod) {
      dispatchPasteAndDropFallback(inputEl, files);
      uploadMethod = "paste_drop_fallback";
    }

    await waitForComposerAttachmentsReady(inputEl, totalBytes, onProgress);
    logDriver(
      "info",
      `已注入 ${files.length} 个多模态附件 (${uploadMethod}, bytes=${totalBytes}): ${files
        .map((f) => f.name)
        .join(", ")}`
    );
    return {
      uploaded: files.length,
      method: uploadMethod,
      chipCount: countComposerAttachmentIndicators(findComposerInput() || inputEl)
    };
  }


  async function submitComposerWithVerification(inputEl, promptText, job = null, baselineCount = 0, options = {}) {
    const promptTrimLen = String(promptText || "").trim().length;
    const hadAttachments = Boolean(options && options.hadAttachments);
    const preSubmitChipCount = Number((options && options.preSubmitChipCount) || 0);
    const hasSubmissionStarted = () => {
      if (job && (job.submissionStarted || job.networkText.length > 0 || job.networkDone)) return true;
      if (isPageStillGenerating()) return true;
      if (collectAssistantReplyTexts().length > baselineCount) return true;
      const liveInput = findComposerInput() || inputEl;
      const curText = readComposerCurrentText(liveInput);
      if (promptTrimLen > 0 && curText.length === 0) return true;
      if (!hadAttachments && curText.length === 0) return true;
      if (promptTrimLen > 10 && curText.length < Math.min(20, promptTrimLen * 0.2)) return true;
      if (hadAttachments && preSubmitChipCount > 0 && countComposerAttachmentIndicators(liveInput) === 0) return true;
      return false;
    };

    // Wait up to ~1s for Lit/ProseMirror reactive state to enable the Send button
    await nextAnimationFrame();
    let sendBtn = null;
    for (let i = 0; i < 12; i += 1) {
      assertJobActive(job);
      sendBtn = findSendButton(inputEl);
      if (sendBtn) break;
      await sleep(80);
    }

    // Step A: Primary click or Enter keydown
    assertJobActive(job);
    let method = "enter_keypress";
    if (sendBtn) {
      sendBtn.click();
      method = "button_click";
    } else {
      dispatchEnterKeySequence(inputEl);
    }

    for (let i = 0; i < 6; i += 1) {
      await sleep(90);
      assertJobActive(job);
      if (hasSubmissionStarted()) {
        return method;
      }
    }

    // Try one submission method at a time, then wait for acknowledgement.
    sendBtn = findSendButton(inputEl) || sendBtn;
    if (sendBtn) {
      dispatchRealisticClick(sendBtn);
      method = "realistic_button_click";
    } else {
      const form = inputEl.closest ? inputEl.closest("form") : null;
      if (form && typeof form.requestSubmit === "function") {
        form.requestSubmit();
      } else {
        dispatchEnterKeySequence(inputEl);
      }
    }

    for (let i = 0; i < 7; i += 1) {
      await sleep(95);
      assertJobActive(job);
      if (hasSubmissionStarted()) {
        return method;
      }
    }

    // Step C: Background / parallel tab self-heal via brief mutex-guarded tab activation
    logDriver("warn", "检测到后台/并行标签页首次提交未触发，正在短暂激活标签页以完成发送...");
    await requestBriefTabActivation(240);
    await sleep(180);
    assertJobActive(job);
    if (hasSubmissionStarted()) return method;

    const activeInput = findComposerInput() || inputEl;
    if (promptTrimLen > 0 && !readComposerCurrentText(activeInput) && !hasSubmissionStarted()) {
      await fillInputWithPrompt(activeInput, promptText);
      await sleep(100);
    }
    const healedBtn = findSendButton(activeInput);
    assertJobActive(job);
    if (healedBtn) {
      dispatchRealisticClick(healedBtn);
      method = "tab_activated_button_click";
    } else {
      dispatchEnterKeySequence(activeInput);
      method = "tab_activated_enter";
    }

    for (let i = 0; i < 8; i += 1) {
      await sleep(100);
      assertJobActive(job);
      if (hasSubmissionStarted()) {
        return method;
      }
    }
    throw new Error("网页未确认发送成功，已停止重复提交，请重试。");
  }

  // =========================================================================
  // 2a. New Chat / Clean Session Reset (`findNewChatButton` + Verified Reset)
  // =========================================================================

  const ICON_LIGATURES_RE =
    /\b(arrow_drop_down|arrow_drop_up|expand_more|expand_less|keyboard_arrow_down|keyboard_arrow_up|unfold_more|stat_minus_1|chevron_right|chevron_left|check_circle|check|done|auto_awesome|spark|bolt|psychology|tune)\b/gi;

  const GENERIC_MODEL_PLACEHOLDER_RE =
    /^(choose\s+a?\s*model|select\s+a?\s*model|switch\s+model|model\s+selector|model\s+picker|model|选择模型|切换模型)$/i;

  function getDeepVisibleText(node, depth = 0) {
    if (!node || depth > 12) return "";
    // Text node (`nodeType === 3`)
    if (node.nodeType === 3) {
      return node.nodeValue || "";
    }
    const tag = String(node.tagName || "").toUpperCase();
    if (
      tag === "STYLE" ||
      tag === "SCRIPT" ||
      tag === "NOSCRIPT" ||
      tag.includes("TOOLTIP") ||
      (node.getAttribute && node.getAttribute("role") === "tooltip")
    ) {
      return "";
    }
    if (node.style && (node.style.display === "none" || node.style.visibility === "hidden")) {
      return "";
    }

    const pieces = [];
    // Support both browser DOM `childNodes` and lightweight test nodes with `children` / `_text`
    if (typeof node._text === "string" && node._text) {
      pieces.push(node._text);
    } else if (node.childNodes && node.childNodes.length > 0) {
      for (let i = 0; i < node.childNodes.length; i += 1) {
        const sub = getDeepVisibleText(node.childNodes[i], depth + 1);
        if (sub) pieces.push(sub);
      }
    }

    if (node.children && (!node.childNodes || node.childNodes.length === 0)) {
      for (let i = 0; i < node.children.length; i += 1) {
        const sub = getDeepVisibleText(node.children[i], depth + 1);
        if (sub) pieces.push(sub);
      }
    }

    if (node.shadowRoot) {
      const srChildren = node.shadowRoot.childNodes && node.shadowRoot.childNodes.length > 0
        ? node.shadowRoot.childNodes
        : node.shadowRoot.children || [];
      for (let i = 0; i < srChildren.length; i += 1) {
        const sub = getDeepVisibleText(srChildren[i], depth + 1);
        if (sub) pieces.push(sub);
      }
    }

    if (pieces.length === 0 && typeof node.innerText === "string" && node.innerText) {
      return node.innerText;
    }
    const sep = tag === "DIV" || tag === "P" || tag === "LI" || tag.includes("ITEM") || tag.includes("OPTION") ? "\n" : " ";
    return pieces.join(sep);
  }

  function stripIconLigatures(raw) {
    return String(raw || "")
      .replace(ICON_LIGATURES_RE, " ")
      .replace(/[▾▴▼▲⌄›]/g, " ")
      .replace(/\s+/g, " ")
      .trim();
  }

  function getElementTooltipOrLabel(el) {
    if (!el || !el.getAttribute) return "";
    const parts = [
      el.getAttribute("aria-label") || "",
      el.getAttribute("title") || "",
      el.getAttribute("mattooltip") || "",
      el.getAttribute("data-tooltip") || "",
      el.getAttribute("data-title") || "",
      el.getAttribute("data-test-id") || "",
      el.getAttribute("data-testid") || ""
    ];

    const rootNode = el.getRootNode ? el.getRootNode() : document;
    for (const attr of ["aria-labelledby", "aria-describedby"]) {
      const refIds = (el.getAttribute(attr) || "").split(/\s+/).filter(Boolean);
      for (const refId of refIds) {
        try {
          const refEl = rootNode.getElementById
            ? rootNode.getElementById(refId)
            : document.getElementById(refId);
          if (refEl) {
            parts.push(refEl.textContent || "");
          }
        } catch (_) {}
      }
    }

    // Material Web `<md-tooltip>` / `<ucs-tooltip>` is commonly placed as adjacent sibling or anchored by id
    const sib = el.nextElementSibling;
    if (sib && /tooltip/i.test(sib.tagName || "")) {
      parts.push(sib.textContent || "");
    }
    if (el.id && rootNode && rootNode.querySelectorAll) {
      try {
        const anchored = rootNode.querySelectorAll(
          `md-tooltip[anchor="${CSS.escape(el.id)}"], [for="${CSS.escape(el.id)}"]`
        );
        for (let i = 0; i < anchored.length; i += 1) {
          parts.push(anchored[i].textContent || "");
        }
      } catch (_) {}
    }
    if (el.parentElement && el.parentElement.querySelector) {
      try {
        const parentTip = el.parentElement.querySelector("md-tooltip, mat-tooltip, ucs-tooltip, [role='tooltip']");
        if (parentTip && parentTip !== el) {
          parts.push(parentTip.textContent || "");
        }
      } catch (_) {}
    }

    return Array.from(new Set(parts.map((p) => p.trim()).filter(Boolean)))
      .join(" ")
      .replace(/\s+/g, " ")
      .trim();
  }

  function getComposerRootHost(inputEl) {
    let cur = inputEl;
    let best = null;
    for (let i = 0; i < 10 && cur; i += 1) {
      const tag = (cur.tagName || "").toLowerCase();
      if (
        tag.includes("search-box") ||
        tag.includes("composer") ||
        tag.includes("chat-input") ||
        tag.includes("prompt-box")
      ) {
        best = cur;
      }
      if (cur.parentElement) {
        cur = cur.parentElement;
      } else {
        const rn = cur.getRootNode ? cur.getRootNode() : null;
        cur = rn && rn.host ? rn.host : null;
      }
    }
    return best;
  }

  function computeCleanNewChatUrl(href = window.location.href) {
    try {
      const u = new URL(href);
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

  function hasSessionPathInUrl(pathname = window.location.pathname) {
    return /\/(?:r\/)?session\/[^/?#]+|\/c\/[^/?#]+/i.test(String(pathname || ""));
  }

  function findNewChatButton() {
    const inputEl = findComposerInput();
    const composerHost = inputEl ? getComposerRootHost(inputEl) : null;
    const inputRect = inputEl && inputEl.getBoundingClientRect ? inputEl.getBoundingClientRect() : null;

    const explicitNewChatRe =
      /\b(new chat|new conversation|new session|start a new chat|start new chat|start new|create new chat|reset chat|clear chat)\b|新建对话|新对话|新会话|新建会话|开启新对话|开始新对话|重新开始/i;
    const excludeNewChatRe =
      /\b(attach|upload|file|image|drive|source|connector|tool|grounding|mic|voice|send|submit|stop|delete|remove|rename|pin|share|settings|feedback|help)\b|上传|附件|文件|数据源|连接器|工具|语音|发送|停止|删除|重命名|分享|设置/i;
    const composeIconNames = new Set([
      "edit_square",
      "add_comment",
      "chat_add_on",
      "maps_ugc",
      "post_add",
      "rate_review"
    ]);
    const plusIconNames = new Set(["add", "+", "＋"]);

    const cleanHomeUrl = computeCleanNewChatUrl(window.location.href);
    let cleanHomePath = "";
    if (cleanHomeUrl) {
      try {
        cleanHomePath = new URL(cleanHomeUrl).pathname;
      } catch (_) {}
    }

    const controls = querySelectorAllDeep(
      [
        "button",
        "a[href]",
        "md-icon-button",
        "md-fab",
        "md-filled-tonal-icon-button",
        "md-text-button",
        "md-filled-button",
        "md-outlined-button",
        "ucs-icon-button",
        "mat-list-item",
        "md-list-item",
        '[role="button"]',
        '[role="link"]'
      ].join(", ")
    ).filter((el) => isElementVisible(el) && !el.disabled && el.getAttribute("aria-disabled") !== "true");

    const scored = [];

    for (const el of controls) {
      // Never click the `+` (attach/tools) button inside the bottom prompt composer!
      if (composerHost && containsDeep(composerHost, el)) {
        continue;
      }
      const rect = el.getBoundingClientRect();
      if (
        inputRect &&
        Math.abs(rect.top - inputRect.top) < 90 &&
        rect.left >= inputRect.left - 80 &&
        rect.right <= inputRect.right + 120
      ) {
        continue;
      }

      const rawText = (el.innerText || el.textContent || "").replace(/\s+/g, " ").trim();
      const tooltipLabel = getElementTooltipOrLabel(el);
      const combined = `${tooltipLabel} ${rawText}`.trim();

      if (excludeNewChatRe.test(tooltipLabel)) {
        continue;
      }

      let score = 0;
      const chain = getShadowHostChainTagString(el);

      if (explicitNewChatRe.test(combined)) {
        score += 100;
      }

      // Check Material/Google icon ligatures inside header / left navigation rail
      const iconEls = querySelectorAllDeep(
        "md-icon, mat-icon, .google-symbols, .material-symbols-outlined, .material-icons",
        el.shadowRoot || el
      );
      const iconTokens = iconEls
        .map((ic) => (ic.textContent || "").trim().toLowerCase())
        .filter(Boolean);
      if (rawText.length <= 20) {
        iconTokens.push(rawText.toLowerCase());
      }

      const inHeaderOrRail = rect.top < 140 || rect.left < 300 || chain.includes("nav") || chain.includes("header") || chain.includes("sidebar");
      if (iconTokens.some((tok) => composeIconNames.has(tok)) && inHeaderOrRail) {
        score += 80;
      } else if (iconTokens.some((tok) => plusIconNames.has(tok)) && (rect.top < 120 || rect.left < 260)) {
        score += 68;
      }

      // Check `<a href>` pointing directly to clean `/u/N/home/cid/<cid>` without `/r/session/`
      const href = el.getAttribute("href") || "";
      if (href && cleanHomePath) {
        try {
          const resolved = new URL(href, window.location.origin);
          if (resolved.pathname === cleanHomePath) {
            score += 75;
          }
        } catch (_) {}
      }

      if (inHeaderOrRail) {
        score += 10;
      }

      // Prefer inner button over outer wrapper if nested
      if (score >= 65) {
        scored.push({
          el,
          score,
          summary: `${el.tagName.toLowerCase()}("${(tooltipLabel || rawText).slice(0, 40)}")@${Math.round(rect.left)},${Math.round(rect.top)}`
        });
      }
    }

    if (scored.length === 0) {
      console.log("[GEB:newchat] No New Chat button matched in current DOM view.");
      return null;
    }

    scored.sort((a, b) => b.score - a.score);
    console.log(
      "[GEB:newchat] Top New Chat button candidates:",
      scored.slice(0, 4).map((c) => ({ score: c.score, summary: c.summary }))
    );
    return scored[0].el;
  }

  function findSidebarToggleButton() {
    const controls = querySelectorAllDeep('button, md-icon-button, ucs-icon-button, [role="button"]').filter(
      isElementVisible
    );
    for (const btn of controls) {
      const rect = btn.getBoundingClientRect();
      if (rect.top > 130 || rect.left > 180) continue;
      const txt = `${getElementTooltipOrLabel(btn)} ${btn.textContent || ""}`.toLowerCase();
      if (/\b(menu|navigation|sidebar|side_navigation|left_panel_open|dock_to_right)\b|菜单|导航|侧边栏/.test(txt)) {
        return btn;
      }
    }
    return null;
  }

  function dispatchRealisticClick(el) {
    if (!el) return;
    const rect = el.getBoundingClientRect ? el.getBoundingClientRect() : { left: 10, top: 10, width: 20, height: 20 };
    const cx = rect.left + rect.width / 2;
    const cy = rect.top + rect.height / 2;
    const evInit = {
      bubbles: true,
      composed: true,
      cancelable: true,
      clientX: cx,
      clientY: cy,
      button: 0,
      buttons: 1
    };
    try {
      if (typeof PointerEvent === "function") {
        el.dispatchEvent(new PointerEvent("pointerdown", evInit));
      }
      el.dispatchEvent(new MouseEvent("mousedown", evInit));
      if (typeof PointerEvent === "function") {
        el.dispatchEvent(new PointerEvent("pointerup", { ...evInit, buttons: 0 }));
      }
      el.dispatchEvent(new MouseEvent("mouseup", { ...evInit, buttons: 0 }));
    } catch (_) {}
    el.click();
  }

  function hasSessionCleared(beforeHref, beforeReplyCount) {
    const nowHref = window.location.href;
    const nowReplies = collectAssistantReplyTexts().length;
    const beforeHadSession = hasSessionPathInUrl(new URL(beforeHref, window.location.origin).pathname);
    const nowHasSession = hasSessionPathInUrl(window.location.pathname);

    if (beforeHadSession && !nowHasSession) {
      return true;
    }
    if (beforeHadSession && nowHref !== beforeHref && nowReplies === 0) {
      return true;
    }
    if (beforeReplyCount > 0 && nowReplies === 0) {
      return true;
    }
    return false;
  }

  async function triggerNewChatSession() {
    const beforeHref = window.location.href;
    const beforeReplies = collectAssistantReplyTexts().length;
    const cleanUrl = computeCleanNewChatUrl(beforeHref);
    const isHidden = Boolean(document.hidden || document.visibilityState === "hidden");

    logDriver(
      "info",
      `请求重置网页对话 (当前回复块=${beforeReplies}, sessionUrl=${hasSessionPathInUrl()}, hidden=${isHidden})`
    );

    if (!hasSessionPathInUrl() && beforeReplies === 0) {
      const existingInput = findComposerInput();
      if (existingInput && (existingInput.innerText || existingInput.value || "").trim()) {
        await fillInputWithPrompt(existingInput, "");
      }
      return { ok: true, cleared: true, method: "already_clean", cleanUrl, hidden: isHidden };
    }

    // Use shorter polling cycles when hidden so background.js can fast-fallback to URL navigation if DOM is frozen
    const pollCycles = isHidden ? 5 : 14;
    const pollStepMs = isHidden ? 80 : 110;

    // Stage 1: Direct New Chat button click
    let newChatBtn = findNewChatButton();

    // Stage 2: If sidebar is collapsed, expand navigation drawer and search again
    if (!newChatBtn) {
      const navToggle = findSidebarToggleButton();
      if (navToggle) {
        console.log("[GEB:newchat] Expanding collapsed sidebar drawer to locate New Chat button...");
        navToggle.click();
        await sleep(isHidden ? 160 : 320);
        newChatBtn = findNewChatButton();
      }
    }

    if (newChatBtn) {
      const btnDesc = (getElementTooltipOrLabel(newChatBtn) || newChatBtn.textContent || "").trim().slice(0, 50);
      newChatBtn.click();
      for (let i = 0; i < pollCycles; i += 1) {
        await sleep(pollStepMs);
        if (hasSessionCleared(beforeHref, beforeReplies)) {
          sharedState.lastCapturedRpc = null;
          await sleep(isHidden ? 100 : 180);
          logDriver("info", `已通过网页按钮开启新会话 (${btnDesc || newChatBtn.tagName.toLowerCase()})`);
          return { ok: true, cleared: true, method: "button", label: btnDesc, cleanUrl, hidden: isHidden };
        }
      }

      // Retry with full pointer+mouse sequence if simple .click() was ignored
      dispatchRealisticClick(newChatBtn);
      for (let i = 0; i < (isHidden ? 4 : 10); i += 1) {
        await sleep(pollStepMs);
        if (hasSessionCleared(beforeHref, beforeReplies)) {
          sharedState.lastCapturedRpc = null;
          await sleep(isHidden ? 100 : 180);
          logDriver("info", `已通过指针事件开启新会话 (${btnDesc || newChatBtn.tagName.toLowerCase()})`);
          return { ok: true, cleared: true, method: "pointer_button", label: btnDesc, cleanUrl, hidden: isHidden };
        }
      }
    }

    // Stage 3: Trigger SPA router via same-origin `<a>` link click inside app root
    if (cleanUrl) {
      try {
        const appHost =
          document.querySelector("ucs-standalone-app, app-root, body") || document.body;
        const tempLink = document.createElement("a");
        tempLink.href = cleanUrl;
        tempLink.style.display = "none";
        (appHost.shadowRoot || appHost).appendChild(tempLink);
        tempLink.click();
        tempLink.remove();

        for (let i = 0; i < (isHidden ? 4 : 10); i += 1) {
          await sleep(pollStepMs);
          if (hasSessionCleared(beforeHref, beforeReplies)) {
            sharedState.lastCapturedRpc = null;
            await sleep(isHidden ? 100 : 200);
            logDriver("info", "已通过 SPA 路由跳转开启新会话");
            return { ok: true, cleared: true, method: "spa_link_router", cleanUrl, hidden: isHidden };
          }
        }
      } catch (_) {}
    }

    logDriver(
      "warn",
      "页面内未检测到会话清空，将交由后台扩展执行干净会话 URL 导航回退。"
    );
    return {
      ok: false,
      cleared: false,
      reason: "in_page_reset_unconfirmed",
      cleanUrl,
      hidden: isHidden
    };
  }

  // =========================================================================
  // 2b. Web UI Model Selector Discovery & Auto-Switching (Auto / 3.1 Pro / 4 Argon / 4 Carbon / 3.8 Flash)
  // =========================================================================

  const MODEL_MATCH_RULES = {
    auto: {
      exactRe: /^(?:gemini\s+)?auto(?:\s*\(.*\))?$|^自动(?:\s*\(.*\))?$/i,
      headlineRe: /\bauto\b|自动/i
    },
    "3.1-pro": {
      exactRe: /^(?:gemini\s+)?3\.1\s*[-]?\s*pro\b/i,
      headlineRe: /3\.1\s*[-]?\s*pro\b|3\.1pro\b/i
    },
    "4-argon": {
      exactRe: /^(?:gemini\s+)?4(?:\.0)?\s*[-]?\s*argon\b|^argon\b/i,
      headlineRe: /4(?:\.0)?\s*[-]?\s*argon\b|4argon\b|\bargon\b/i,
      conflictRe: /\bcarbon\b|4carbon\b/i
    },
    "4-carbon": {
      exactRe: /^(?:gemini\s+)?4(?:\.0)?\s*[-]?\s*carbon\b|^carbon\b/i,
      headlineRe: /4(?:\.0)?\s*[-]?\s*carbon\b|4carbon\b|\bcarbon\b/i,
      conflictRe: /\bargon\b|4argon\b/i
    },
    "3.8-flash": {
      exactRe: /^(?:gemini\s+)?3\.8\s*[-]?\s*flash\b/i,
      headlineRe: /3\.8\s*[-]?\s*flash\b|3\.8flash\b/i
    },
    "2.5-pro": {
      exactRe: /^(?:gemini\s+)?2\.5\s*[-]?\s*pro\b/i,
      headlineRe: /2\.5\s*[-]?\s*pro\b|2\.5pro\b/i
    },
    "2.5-flash": {
      exactRe: /^(?:gemini\s+)?2\.5\s*[-]?\s*flash\b/i,
      headlineRe: /2\.5\s*[-]?\s*flash\b|2\.5flash\b/i
    }
  };

  function scoreModelOptionMatch(rawText, targetModel) {
    const cleaned = stripIconLigatures(rawText);
    if (!cleaned) return 0;

    const lines = String(rawText || "")
      .split(/\n+/)
      .map((line) => stripIconLigatures(line))
      .filter(Boolean);
    const headline = lines[0] || cleaned;

    const key = String(targetModel || "").trim().toLowerCase();
    if (!key) return 0;

    const rule = MODEL_MATCH_RULES[key];
    if (rule) {
      if (rule.conflictRe && rule.conflictRe.test(headline)) return 0;
      if (rule.exactRe.test(headline)) return 120;
      if (rule.headlineRe.test(headline)) return 100;
      // For `auto`, avoid matching description text like "Automatically selects..."
      if (key !== "auto" && (!rule.conflictRe || !rule.conflictRe.test(cleaned)) && rule.headlineRe.test(cleaned)) return 75;
      return 0;
    }

    // Custom pass-through model label matching
    const normHeadline = headline.toLowerCase();
    const normFull = cleaned.toLowerCase();
    const spacedKey = key.replace(/[-_]+/g, " ").replace(/\s+/g, " ").trim();
    const compactKey = key.replace(/[\s\-_]+/g, "");
    const compactHeadline = normHeadline.replace(/[\s\-_]+/g, "");
    const compactFull = normFull.replace(/[\s\-_]+/g, "");

    if (normHeadline === key || normHeadline === spacedKey) return 120;
    if (normHeadline.includes(spacedKey) || (compactKey && compactHeadline.includes(compactKey))) {
      return 100;
    }
    if (normFull.includes(spacedKey) || (compactKey && compactFull.includes(compactKey))) {
      return 70;
    }
    return 0;
  }

  function hasDropdownIndicator(el) {
    if (!el) return false;
    if (
      el.hasAttribute("aria-haspopup") ||
      el.hasAttribute("aria-expanded") ||
      el.getAttribute("role") === "combobox" ||
      (el.tagName || "").toUpperCase() === "MAT-SELECT"
    ) {
      return true;
    }
    const raw = (el.innerText || el.textContent || "").toLowerCase();
    if (
      /\b(arrow_drop_down|expand_more|keyboard_arrow_down|unfold_more|stat_minus_1)\b|[▾▼⌄]/.test(raw)
    ) {
      return true;
    }
    const innerChevron = querySelectorAllDeep(
      'svg, md-icon, mat-icon, .google-symbols, .material-symbols-outlined, [class*="arrow" i], [class*="chevron" i], [class*="caret" i]',
      el.shadowRoot || el
    );
    return innerChevron.length > 0;
  }

  function findModelSelectorTrigger() {
    if (driverConfig.customModelSelector) {
      const customEls = querySelectorAllDeep(driverConfig.customModelSelector).filter(isElementVisible);
      if (customEls.length > 0) {
        return customEls[0];
      }
    }

    // 1. Check dedicated custom elements / test-id wrappers first
    const dedicatedHosts = querySelectorAllDeep(
      [
        "ucs-model-selector",
        "ucs-model-picker",
        "model-selector",
        '[data-test-id*="model" i]',
        '[data-testid*="model" i]',
        '[class*="model-selector" i]',
        '[class*="model-picker" i]',
        '[class*="model-dropdown" i]'
      ].join(", ")
    ).filter(isElementVisible);

    for (const host of dedicatedHosts) {
      const tag = (host.tagName || "").toUpperCase();
      if (
        tag === "BUTTON" ||
        tag === "MAT-SELECT" ||
        host.getAttribute("role") === "button" ||
        host.getAttribute("role") === "combobox"
      ) {
        return host;
      }
      const innerBtns = querySelectorAllDeep(
        'button, [role="button"], [role="combobox"], mat-select, md-outlined-select, md-filled-select, md-assist-chip, md-filter-chip, [tabindex="0"]',
        host.shadowRoot || host
      ).filter(isElementVisible);
      if (innerBtns.length > 0) {
        return innerBtns[0];
      }
      return host;
    }

    // 2. Heuristic scan across composer & header controls (including "Auto" default pill!)
    const inputEl = findComposerInput();
    const inputRect = inputEl && inputEl.getBoundingClientRect ? inputEl.getBoundingClientRect() : null;

    const excludeRe =
      /\b(send|submit|attach|upload|mic|voice|audio|stop|clear|copy|new chat|new conversation|history|settings|share|feedback|help|sign out|tools|sources|connectors|data stores)\b|发送|提交|上传|附件|语音|停止|清空|新建|新会话|历史|设置|分享|反馈|工具|数据源|连接器/i;

    const explicitModelNameRe =
      /\b(gemini|3\.1\s*[-]?\s*pro|3\.1pro|4(?:\.0)?\s*[-]?\s*argon|4argon|argon|4(?:\.0)?\s*[-]?\s*carbon|4carbon|carbon|3\.8\s*[-]?\s*flash|3\.8flash|2\.5\s*[-]?\s*pro|2\.5\s*[-]?\s*flash|2\.0\s*[-]?\s*flash)\b/i;
    const shortModePillRe =
      /^(auto|自动|fast|pro|flash|thinking|reasoning|ultra|standard|default)$/i;
    const modelAttrRe = /\b(model|llm|select model|choose model|switch model)\b|模型|切换模型|选择模型/i;

    const candidates = querySelectorAllDeep(
      [
        "button",
        '[role="button"]',
        '[role="combobox"]',
        "[aria-haspopup]",
        "mat-select",
        "md-outlined-select",
        "md-filled-select",
        "md-assist-chip",
        "md-filter-chip",
        "md-input-chip",
        "md-text-button",
        "md-outlined-button",
        "md-filled-tonal-button",
        "mat-chip",
        "ucs-chip",
        "ucs-pill",
        "ucs-dropdown"
      ].join(", ")
    ).filter((el) => isElementVisible(el) && !el.disabled && el.getAttribute("aria-disabled") !== "true");

    const scored = [];
    for (const el of candidates) {
      const rawText = getDeepVisibleText(el).replace(/\s+/g, " ").trim();
      const cleanLabel = stripIconLigatures(rawText);
      const aria = getElementTooltipOrLabel(el);

      if ((!cleanLabel && !aria) || excludeRe.test(aria) || excludeRe.test(cleanLabel)) {
        continue;
      }

      let score = 0;
      const chain = getShadowHostChainTagString(el);
      const chevron = hasDropdownIndicator(el);
      const rect = el.getBoundingClientRect();
      const nearComposer =
        inputRect &&
        Math.abs(rect.top - inputRect.bottom) < 130 &&
        rect.left >= inputRect.left - 80 &&
        rect.right <= inputRect.right + 160;
      const inTopHeader = rect.top >= 0 && rect.top < 110;

      if (chain.includes("model-selector") || chain.includes("model-picker") || chain.includes("ucs-model")) {
        score += 90;
      }
      if (modelAttrRe.test(aria)) {
        score += 75;
      }
      if (cleanLabel.length >= 2 && cleanLabel.length <= 44 && explicitModelNameRe.test(cleanLabel)) {
        score += 65;
      }
      // Critical fix: recognize default "Auto" / "自动" / "Fast" / "Thinking" model selector pill!
      if (shortModePillRe.test(cleanLabel)) {
        if (chevron || nearComposer) {
          score += 58;
        } else if (inTopHeader) {
          score += 42;
        }
      }
      if (chevron) {
        score += 18;
      }
      if (nearComposer) {
        score += 22;
      } else if (inTopHeader) {
        score += 8;
      }
      if (chain.includes("ucs-search-box") || chain.includes("ucs-chat") || chain.includes("ucs-composer")) {
        score += 12;
      }

      if (score >= 50) {
        scored.push({
          el,
          score,
          cleanLabel,
          aria: aria.slice(0, 40),
          chevron,
          chain: chain.slice(0, 70)
        });
      }
    }

    if (scored.length === 0) {
      console.log("[GEB:model] No model selector trigger matched (candidates scanned=" + candidates.length + ").");
      return null;
    }

    // If both an outer wrapper and an inner button matched with tied/similar score, prefer the inner interactive button
    scored.sort((a, b) => {
      if (b.score !== a.score) return b.score - a.score;
      if (containsDeep(a.el, b.el)) return 1;
      if (containsDeep(b.el, a.el)) return -1;
      return 0;
    });

    console.log(
      "[GEB:model] Model trigger candidates:",
      scored.slice(0, 4).map((c) => ({
        score: c.score,
        label: c.cleanLabel,
        aria: c.aria,
        chevron: c.chevron,
        chain: c.chain
      }))
    );
    return scored[0].el;
  }

  function readDetectedWebModel() {
    const trigger = findModelSelectorTrigger();
    if (!trigger) return sharedState.lastSelectedModel || "";
    const deepTxt = stripIconLigatures(getDeepVisibleText(trigger));
    if (deepTxt && deepTxt.length <= 60 && !GENERIC_MODEL_PLACEHOLDER_RE.test(deepTxt)) {
      return deepTxt;
    }
    const tipTxt = stripIconLigatures(getElementTooltipOrLabel(trigger)).slice(0, 60);
    if (tipTxt && !GENERIC_MODEL_PLACEHOLDER_RE.test(tipTxt)) {
      return tipTxt;
    }
    return sharedState.lastSelectedModel || "";
  }

  function collectVisibleModelOptions(trigger) {
    const explicitOptionSelector = [
      "mat-option",
      "md-menu-item",
      "md-select-option",
      "md-list-item",
      "mwc-list-item",
      "ucs-model-option",
      "ucs-menu-item",
      "ucs-option",
      '[role="menuitem"]',
      '[role="menuitemradio"]',
      '[role="option"]',
      ".mat-mdc-menu-item",
      ".mat-mdc-option",
      ".mdc-list-item"
    ].join(", ");

    const rawSet = new Set(querySelectorAllDeep(explicitOptionSelector));

    // Also inspect open menu/listbox/popover containers for custom Lit option rows
    const menuContainers = querySelectorAllDeep(
      '[role="menu"], [role="listbox"], md-menu, mat-select-panel, ucs-menu, .cdk-overlay-pane, [popover]'
    ).filter(isElementVisible);

    for (const menu of menuContainers) {
      const innerRows = querySelectorAllDeep(
        'button, li, [tabindex="0"], [tabindex="-1"], [class*="option" i], [class*="item" i]',
        menu.shadowRoot || menu
      );
      for (const row of innerRows) {
        rawSet.add(row);
      }
    }

    const visibleOpts = Array.from(rawSet).filter((opt) => {
      if (!isElementVisible(opt)) return false;
      if (trigger && (opt === trigger || containsDeep(opt, trigger) || containsDeep(trigger, opt))) {
        return false;
      }
      const txt = stripIconLigatures(getDeepVisibleText(opt));
      return txt.length >= 2 && txt.length <= 220;
    });

    // Exclude parent menu containers that wrap 2+ distinct child options
    return visibleOpts.filter((opt) => {
      const wrappedChildren = visibleOpts.filter((other) => other !== opt && containsDeep(opt, other));
      return wrappedChildren.length < 2;
    });
  }

  async function dismissTransientOverlays(selectedOptionEl = null) {
    await sleep(160);
    const backdrops = querySelectorAllDeep(
      ".cdk-overlay-backdrop-showing, .cdk-overlay-backdrop, .mdc-menu-surface--open"
    ).filter(isElementVisible);
    const menuStillOpen =
      (selectedOptionEl && isElementVisible(selectedOptionEl)) || backdrops.length > 0;

    if (!menuStillOpen) {
      return;
    }

    console.log("[GEB:model] Dismissing lingering dropdown overlay/backdrop...");
    for (const bd of backdrops) {
      if (bd.classList && bd.classList.contains("cdk-overlay-backdrop")) {
        try {
          bd.click();
        } catch (_) {}
      }
    }

    const escInit = { key: "Escape", code: "Escape", keyCode: 27, which: 27, bubbles: true, composed: true };
    try {
      (document.activeElement || document.body).dispatchEvent(new KeyboardEvent("keydown", escInit));
      document.body.dispatchEvent(new KeyboardEvent("keydown", escInit));
    } catch (_) {}
    await sleep(100);
  }

  async function ensureWebModelSelected(targetModel) {
    if (!targetModel || !String(targetModel).trim()) {
      return { ok: true, switched: false, reason: "keep_current" };
    }

    const desired = String(targetModel).trim();
    const trigger = findModelSelectorTrigger();
    if (!trigger) {
      logDriver(
        "warn",
        `未定位到网页模型选择器，无法切换至 "${desired}"（页面可能已被管理员锁定模型），将继续发送。`
      );
      return { ok: false, switched: false, reason: "trigger_not_found" };
    }

    const deepCurrent = getDeepVisibleText(trigger);
    const currentRaw = `${deepCurrent}\n${getElementTooltipOrLabel(trigger)}`;
    const currentClean = stripIconLigatures(deepCurrent);

    if (scoreModelOptionMatch(currentRaw, desired) >= 100) {
      sharedState.lastSelectedModel = currentClean || desired;
      logDriver("info", `网页模型已处于目标状态: ${currentClean || desired}`);
      emitToIsolated({
        type: "driver_ready",
        captured_rpc: Boolean(sharedState.lastCapturedRpc),
        active_model: currentClean || desired
      });
      return { ok: true, switched: false, alreadySelected: true, model: currentClean || desired };
    }

    logDriver("info", `正在将网页模型从 "${currentClean || "Auto"}" 切换至 "${desired}"...`);

    // Open Strategy 1: Standard single .click() first (avoids double-toggle on mousedown+click menus)
    trigger.click();
    let options = [];
    for (let i = 0; i < 5; i += 1) {
      await sleep(90);
      options = collectVisibleModelOptions(trigger);
      if (options.length > 0) break;
    }

    // Open Strategy 2: Full pointerdown/mousedown/mouseup/click with coordinates if menu didn't appear
    if (options.length === 0) {
      console.log("[GEB:model] Simple .click() opened 0 options; retrying with realistic pointer events...");
      dispatchRealisticClick(trigger);
      for (let i = 0; i < 7; i += 1) {
        await sleep(95);
        options = collectVisibleModelOptions(trigger);
        if (options.length > 0) break;
      }
    }

    // Open Strategy 3: Try clicking shadow host if trigger was an inner node
    if (options.length === 0) {
      const rn = trigger.getRootNode ? trigger.getRootNode() : null;
      if (rn && rn.host && rn.host !== trigger) {
        console.log("[GEB:model] Retrying click on trigger shadow host:", rn.host.tagName);
        dispatchRealisticClick(rn.host);
        for (let i = 0; i < 6; i += 1) {
          await sleep(95);
          options = collectVisibleModelOptions(trigger);
          if (options.length > 0) break;
        }
      }
    }

    const optionLabels = options.map((o) =>
      stripIconLigatures(getDeepVisibleText(o).split("\n")[0])
    );
    console.log("[GEB:model] Visible dropdown options:", optionLabels);

    const scoredHits = options
      .map((opt) => {
        const optDeep = getDeepVisibleText(opt);
        const text = `${optDeep}\n${getElementTooltipOrLabel(opt)} ${opt.getAttribute("data-value") || ""}`;
        return {
          opt,
          score: scoreModelOptionMatch(text, desired),
          label: stripIconLigatures(optDeep.split("\n")[0])
        };
      })
      .filter((item) => item.score > 0);

    if (scoredHits.length === 0) {
      await dismissTransientOverlays(null);
      logDriver(
        "warn",
        `已打开模型菜单但未匹配到 "${desired}"（当前可见选项: [${optionLabels.slice(0, 8).join(", ") || "无"}]），继续使用当前模型。`
      );
      return { ok: false, switched: false, reason: "option_not_found", availableOptions: optionLabels };
    }

    // Prefer highest match score, breaking ties by preferring inner interactive items (`role=menuitem*`/`option`)
    scoredHits.sort((a, b) => {
      if (b.score !== a.score) return b.score - a.score;
      if (containsDeep(a.opt, b.opt)) return 1;
      if (containsDeep(b.opt, a.opt)) return -1;
      return getDeepVisibleText(a.opt).length - getDeepVisibleText(b.opt).length;
    });

    const chosen = scoredHits[0];
    console.log("[GEB:model] Selecting matched option:", chosen.label, "score=", chosen.score);
    chosen.opt.click();
    await sleep(200);

    // Verify if trigger updated; if option is still open & unchanged, send full pointer click on option
    const midTrigger = findModelSelectorTrigger();
    const midLabel = stripIconLigatures(midTrigger ? getDeepVisibleText(midTrigger) : "");
    if (isElementVisible(chosen.opt) && scoreModelOptionMatch(midLabel, desired) === 0) {
      dispatchRealisticClick(chosen.opt);
      await sleep(200);
    }

    await dismissTransientOverlays(chosen.opt);

    sharedState.lastCapturedRpc = null;
    sharedState.lastSelectedModel = chosen.label || desired;
    const updatedLabel = readDetectedWebModel() || chosen.label || desired;

    logDriver("info", `已切换网页模型: ${currentClean || "Auto"} -> ${updatedLabel}`);
    emitToIsolated({
      type: "driver_ready",
      captured_rpc: Boolean(sharedState.lastCapturedRpc),
      active_model: updatedLabel
    });
    return {
      ok: true,
      switched: true,
      model: desired,
      detectedLabel: updatedLabel
    };
  }

  // =========================================================================
  // 3. Shadow-DOM Assistant Reply Extraction & Stability Detection
  // =========================================================================

  const ASSISTANT_CONTAINER_SELECTORS = [
    "ucs-summary",
    "ucs-markdown",
    "ucs-turn-answer",
    "ucs-response",
    "message-content",
    '[data-message-author-role="model"]',
    '[data-message-author-role="assistant"]',
    ".model-response-text",
    ".assistant-message",
    ".markdown-body"
  ].join(", ");

  function cleanAssistantDomText(el) {
    if (!el) {
      return "";
    }
    const text = (el.innerText || el.textContent || "").trim();
    return text
      .replace(/\n{3,}/g, "\n\n")
      .trim();
  }

  function containsDeep(ancestor, descendant) {
    if (!ancestor || !descendant || ancestor === descendant) {
      return false;
    }
    let cur = descendant;
    let guard = 0;
    while (cur && guard < 40) {
      guard += 1;
      if (cur === ancestor) {
        return true;
      }
      if (cur.parentNode) {
        cur = cur.parentNode;
      } else if (cur.host) {
        cur = cur.host;
      } else {
        const root = cur.getRootNode ? cur.getRootNode() : null;
        cur = root && root !== cur ? root.host || null : null;
      }
    }
    return false;
  }

  function collectAssistantReplyTexts() {
    const selector = driverConfig.customReplySelector || ASSISTANT_CONTAINER_SELECTORS;
    const rawNodes = querySelectorAllDeep(selector).filter(isElementVisible);

    // Filter out ancestor containers across Shadow DOM boundaries that wrap matched inner containers
    const leafContainers = rawNodes.filter(
      (node) => !rawNodes.some((other) => other !== node && containsDeep(node, other))
    );

    return leafContainers
      .map((node) => cleanAssistantDomText(node))
      .filter((txt) => txt.length > 0);
  }

  function findGenerationStopButton() {
    return querySelectorAllDeep(
      'button, md-icon-button, md-filled-icon-button, ucs-icon-button, [role="button"]'
    ).find((btn) => {
      if (!isElementVisible(btn)) return false;
      const label = readButtonIconAndLabelText(btn);
      return /\b(stop generating|stop response|cancel generation|stop_circle|stop_generating|^stop$)\b|停止生成|停止回答|停止/i.test(
        label.trim()
      );
    });
  }

  function stopJob(job) {
    job.cancelled = true;
    if (job.cancelStream) job.cancelStream();
    const stopButton = findGenerationStopButton();
    if (stopButton) stopButton.click();
  }

  function assertJobActive(job) {
    if (job && job.cancelled) throw new Error("任务已取消。");
    if (job && job.networkError) throw new Error(job.networkError);
  }

  function isPageStillGenerating() {
    if (findGenerationStopButton()) {
      return true;
    }
    const spinners = querySelectorAllDeep(
      'md-circular-progress, mat-progress-spinner, mat-spinner, ucs-thinking, ucs-thought, [aria-busy="true"]'
    ).filter(isElementVisible);
    return spinners.length > 0;
  }

  // =========================================================================
  // 4. Optional Direct In-Page Fetch Replay Engine
  // =========================================================================

  async function tryDirectFetchReplay(promptText, job, timeoutMs) {
    const rpc = sharedState.lastCapturedRpc;
    if (!rpc || !rpc.bodyText) {
      throw new Error("尚未捕获到网页对话 RPC 请求模板，请先使用 auto 模式发送一条消息。");
    }

    let parsedBody;
    try {
      parsedBody = JSON.parse(rpc.bodyText);
    } catch (_) {
      throw new Error("捕获的 RPC 请求体不是可直接替换的 JSON 格式，请切换回 auto 模式。");
    }

    let replaced = false;
    function replaceQueryField(obj) {
      if (!obj || typeof obj !== "object" || replaced) return;
      if (obj.query && typeof obj.query.text === "string") {
        obj.query.text = promptText;
        replaced = true;
        return;
      }
      if (Array.isArray(obj.queryParts)) {
        obj.queryParts = [{ text: promptText }];
        replaced = true;
        return;
      }
      for (const k of Object.keys(obj)) {
        if (typeof obj[k] === "object") {
          replaceQueryField(obj[k]);
        }
      }
    }
    replaceQueryField(parsedBody);

    if (!replaced) {
      throw new Error("未能在 RPC 模板中定位 query.text 字段，自动回退至 DOM 模式。");
    }

    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    try {
      const res = await nativeFetch(rpc.url, {
        method: rpc.method || "POST",
        headers: rpc.headers,
        body: JSON.stringify(parsedBody),
        credentials: "include",
        signal: controller.signal
      });
      if (!res.ok || !res.body) {
        throw new Error("Replay HTTP " + res.status);
      }
      await consumeRpcReadableStream(res.body, job);
      if (!job.networkDone || !job.networkText.trim()) {
        throw new Error("Replay 回复不完整或返回内容为空");
      }
      return job.networkText;
    } finally {
      clearTimeout(timer);
    }
  }

  // =========================================================================
  // 5. Unified Prompt Execution Orchestrator
  // =========================================================================

  async function selectComposerTool(toolPattern, errorMissingTrigger, errorMissingTool) {
    const alreadyActive = querySelectorAllDeep('button, md-filter-chip, mat-chip, [role="button"]').some((el) => {
      if (!isElementVisible(el)) return false;
      const label = readButtonIconAndLabelText(el);
      return toolPattern.test(label) && (/close|cancel|remove|selected|active|已选/i.test(label) || el.getAttribute("aria-pressed") === "true");
    });
    if (alreadyActive) return;
    const trigger = querySelectorAllDeep('button, md-icon-button, ucs-icon-button, [role="button"]').find((el) => {
      if (!isElementVisible(el)) return false;
      const aria = `${el.getAttribute("aria-label") || ""} ${el.getAttribute("title") || ""} ${getElementTooltipOrLabel(el)}`.trim();
      return /\b(select tools|tools)\b|选择工具|工具/i.test(aria);
    });
    if (!trigger) throw new Error(errorMissingTrigger);
    trigger.click();
    await sleep(220);
    let item = querySelectorAllDeep('[role="menuitem"],[role="menuitemcheckbox"],[role="option"],md-menu-item,button,span,div').find((el) =>
      toolPattern.test(stripIconLigatures(String(el.textContent || "").trim())) && isElementVisible(el));
    if (!item) {
      dispatchRealisticClick(trigger);
      await sleep(250);
      item = querySelectorAllDeep('[role="menuitem"],[role="menuitemcheckbox"],[role="option"],md-menu-item,button,span,div').find((el) =>
        toolPattern.test(stripIconLigatures(String(el.textContent || "").trim())) && isElementVisible(el));
    }
    if (!item) {
      await dismissTransientOverlays(null);
      throw new Error(errorMissingTool);
    }
    item.click();
    await sleep(250);
  }

  async function selectImageGenerationTool() {
    await selectComposerTool(
      /^(create images|generate images|image generation|生成图片|创建图片)$/i,
      "网页没有可用的生图工具入口。",
      "当前账号没有 Create images 工具。"
    );
  }

  async function selectVideoGenerationTool() {
    await selectComposerTool(
      /^(create videos?|generate videos?|video generation|生成视频|创建视频)$/i,
      "网页没有可用的视频生成工具入口。",
      "当前账号没有 Create videos 工具。"
    );
  }

  function isAllowedArtifactUrl(rawSrc) {
    try {
      const url = new URL(rawSrc, location.href);
      if (['blob:', 'data:'].includes(url.protocol)) return true;
      if (url.origin === location.origin) return true;
      if (/(^|\.)(googleusercontent|googleapis|gstatic|cloud\.google)\.com$/i.test(url.hostname)) return true;
    } catch (_) {}
    return false;
  }

  const B64_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

  function encodeBytesBase64(bytes) {
    if (typeof btoa === "function") {
      const parts = [];
      for (let i = 0; i < bytes.length; i += 0x8000) {
        parts.push(String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000)));
      }
      return btoa(parts.join(""));
    }
    let out = "";
    for (let i = 0; i < bytes.length; i += 3) {
      const b0 = bytes[i];
      const b1 = i + 1 < bytes.length ? bytes[i + 1] : 0;
      const b2 = i + 2 < bytes.length ? bytes[i + 2] : 0;
      const trip = (b0 << 16) | (b1 << 8) | b2;
      out += B64_ALPHABET[(trip >> 18) & 63];
      out += B64_ALPHABET[(trip >> 12) & 63];
      out += i + 1 < bytes.length ? B64_ALPHABET[(trip >> 6) & 63] : "=";
      out += i + 2 < bytes.length ? B64_ALPHABET[trip & 63] : "=";
    }
    return out;
  }

  function decodeBase64Bytes(b64) {
    const clean = String(b64 || "").replace(/[^A-Za-z0-9+/=]/g, "");
    if (typeof atob === "function") {
      const bin = atob(clean);
      const bytes = new Uint8Array(bin.length);
      for (let i = 0; i < bin.length; i += 1) bytes[i] = bin.charCodeAt(i);
      return bytes;
    }
    const lookup = new Int16Array(256).fill(-1);
    for (let i = 0; i < B64_ALPHABET.length; i += 1) lookup[B64_ALPHABET.charCodeAt(i)] = i;
    const pad = clean.endsWith("==") ? 2 : clean.endsWith("=") ? 1 : 0;
    const outLen = Math.max(0, Math.floor((clean.length * 3) / 4) - pad);
    const out = new Uint8Array(outLen);
    let pos = 0;
    for (let i = 0; i < clean.length; i += 4) {
      const c0 = lookup[clean.charCodeAt(i)];
      const c1 = lookup[clean.charCodeAt(i + 1)];
      const c2 = clean[i + 2] === "=" ? 0 : lookup[clean.charCodeAt(i + 2)];
      const c3 = clean[i + 3] === "=" ? 0 : lookup[clean.charCodeAt(i + 3)];
      if (c0 < 0 || c1 < 0 || c2 < 0 || c3 < 0) throw new Error("附件 base64 内容无效。");
      const trip = (c0 << 18) | (c1 << 12) | (c2 << 6) | c3;
      if (pos < outLen) out[pos++] = (trip >> 16) & 255;
      if (pos < outLen) out[pos++] = (trip >> 8) & 255;
      if (pos < outLen) out[pos++] = trip & 255;
    }
    return out;
  }

  function blobToDataUrl(blob) {
    if (typeof FileReader === "function") {
      return new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve(reader.result);
        reader.onerror = () => reject(new Error("无法读取媒体文件。"));
        reader.readAsDataURL(blob);
      });
    }
    return blob.arrayBuffer().then((buf) => {
      const b64 = encodeBytesBase64(new Uint8Array(buf));
      return `data:${blob.type || "application/octet-stream"};base64,${b64}`;
    });
  }

  function dataUrlToFile(dataUrl, fallbackName, fallbackMime) {
    if (!/^data:[^,]+;base64,/i.test(dataUrl || "")) {
      throw new Error("附件内容格式无效（需要 base64 data URL）。");
    }
    const commaIdx = dataUrl.indexOf(",");
    const header = dataUrl.slice(5, commaIdx);
    const mime = fallbackMime || header.split(";")[0] || "application/octet-stream";
    const bytes = decodeBase64Bytes(dataUrl.slice(commaIdx + 1));
    return constructBrowserFile(bytes, fallbackName || "attachment.bin", mime);
  }

  function generatedImageNodes(baseline) {
    return querySelectorAllDeep('img').filter((img) => img.complete && img.naturalWidth >= 128 &&
      img.naturalHeight >= 128 && !/^https:\/\/www\.gstatic\.com\/vertexaisearch\/images\//i.test(img.currentSrc || img.src) &&
      !baseline.has(img.currentSrc || img.src) && isElementVisible(img));
  }

  async function readGeneratedImages(nodes) {
    const images = [];
    const seen = new Set();
    for (const img of nodes) {
      const src = img.currentSrc || img.src;
      if (!src || seen.has(src) || !isAllowedArtifactUrl(src)) continue;
      seen.add(src);
      let dataUrl = "";
      let mime = "image/png";
      if (src.startsWith("data:image/")) {
        dataUrl = src;
        mime = src.slice(5).split(";")[0] || "image/png";
      } else {
        const response = await nativeFetch(src, { credentials: "include" });
        if (!response.ok) throw new Error("生成图片下载失败：HTTP " + response.status);
        const blob = await response.blob();
        if (!/^image\/(png|jpeg|webp|gif|bmp|svg\+xml|avif)$/i.test(blob.type) || blob.size === 0 || blob.size > 20 * 1024 * 1024) continue;
        mime = blob.type;
        dataUrl = await blobToDataUrl(blob);
      }
      images.push({ data_url: dataUrl, mime_type: mime, width: img.naturalWidth, height: img.naturalHeight });
      if (images.length >= 6) break;
    }
    return images;
  }

  function collectMediaAndDownloadCandidates(baselineMedia, baselineLinks) {
    const candidates = [];
    for (const el of querySelectorAllDeep('video, audio')) {
      if (!isElementVisible(el) && el.tagName.toLowerCase() !== 'audio') continue;
      const src = el.currentSrc || el.src || (el.querySelector && el.querySelector('source') ? el.querySelector('source').src : "");
      if (src && !baselineMedia.has(src)) {
        const kind = el.tagName.toLowerCase();
        candidates.push({ url: src, name: `generated-${kind}.${kind === 'video' ? 'mp4' : 'wav'}`, category: kind });
      }
    }
    for (const a of querySelectorAllDeep('a[download], a[href^="blob:"], a[href^="data:"]')) {
      if (!isElementVisible(a)) continue;
      const href = a.href || a.getAttribute("href") || "";
      if (!href || baselineLinks.has(href)) continue;
      const dlName = (a.getAttribute("download") || a.textContent || "").trim().slice(0, 100) || "generated-file";
      candidates.push({ url: href, name: dlName, category: "file" });
    }
    return candidates;
  }

  async function readGeneratedFiles(candidates) {
    const out = [];
    const seen = new Set();
    for (const item of candidates) {
      if (!item.url || seen.has(item.url) || !isAllowedArtifactUrl(item.url)) continue;
      seen.add(item.url);
      try {
        if (item.url.startsWith("data:")) {
          const mime = item.url.slice(5).split(";")[0] || "application/octet-stream";
          out.push({ data_url: item.url, mime_type: mime, name: item.name });
        } else {
          const response = await nativeFetch(item.url, { credentials: "include" });
          if (!response.ok) continue;
          const blob = await response.blob();
          if (!blob || blob.size === 0 || blob.size > 80 * 1024 * 1024) continue;
          if (/^text\/html/i.test(blob.type) && item.category !== "file") continue;
          const dataUrl = await blobToDataUrl(blob);
          out.push({ data_url: dataUrl, mime_type: blob.type || "application/octet-stream", name: item.name });
        }
      } catch (_) {}
      if (out.length >= 6) break;
    }
    return out;
  }

  const mainAttachmentReassembly = new Map();

  function findSuitableFileInput(files) {
    const inputs = querySelectorAllDeep('input[type="file"]').filter((el) => !el.disabled);
    if (inputs.length === 0) return null;
    const needsNonImage = files.some((f) => !String(f.type || "").startsWith("image/"));
    if (!needsNonImage) return inputs[0];
    const unrestricted = inputs.find((el) => {
      const accept = String(el.getAttribute("accept") || "").trim().toLowerCase();
      return !accept || accept.includes("*/*") || accept.includes("pdf") || !/^image\/\*$/.test(accept);
    });
    return unrestricted || inputs[0];
  }

  async function attachFilesToComposer(attachments, onProgress = null) {
    if (!attachments || attachments.length === 0) return;
    const files = [];
    for (const att of attachments) {
      let dataUrl = att.data_url || "";
      if (!dataUrl && att.chunked_att_key) {
        const entry = mainAttachmentReassembly.get(att.chunked_att_key);
        mainAttachmentReassembly.delete(att.chunked_att_key);
        const total = att.total_chunks || (entry ? entry.total : 0);
        dataUrl = assembleChunks(entry, total);
      }
      files.push(dataUrlToFile(dataUrl, att.name || "attachment.bin", att.mime_type || "application/octet-stream"));
    }
    let input = findSuitableFileInput(files);
    if (!input) {
      const composerInput = findComposerInput();
      const composerHost = composerInput ? getComposerRootHost(composerInput) : null;
      const addButton = querySelectorAllDeep('button, md-icon-button, ucs-icon-button, [role="button"]').find((el) => {
        if (!isElementVisible(el) || el.disabled) return false;
        const meta = `${el.getAttribute("aria-label") || ""} ${el.getAttribute("title") || ""} ${getElementTooltipOrLabel(el)} ${readButtonIconAndLabelText(el)}`;
        if (/\b(add files|add files or sources|attach|upload files|add_circle)\b|添加文件|添加来源|上传文件|上传附件/i.test(meta)) {
          return true;
        }
        return Boolean(composerHost && containsDeep(composerHost, el) && /^\s*(add|\+|attach_file)\s*$/i.test((el.textContent || "").trim()));
      });
      if (!addButton) throw new Error("当前网页没有文件上传入口。");
      addButton.click();
      await sleep(250);
      input = findSuitableFileInput(files);
      if (!input) {
        const upload = querySelectorAllDeep('[role="menuitem"],md-menu-item,button,span,div').find((el) =>
          /^(upload files?|upload from computer|upload|from computer|local files?|上传文件|从电脑上传|本地上传)$/i.test(stripIconLigatures(String(el.textContent || "").trim())) && isElementVisible(el));
        if (upload) upload.click();
      }
      for (let attempt = 0; attempt < 25 && !input; attempt += 1) {
        await sleep(200);
        input = findSuitableFileInput(files);
      }
    }
    if (!input) throw new Error("没有找到网页的文件输入框，附件未上传。");

    const countRemovable = () => querySelectorAllDeep('button, md-icon-button, ucs-icon-button, [role="button"]').filter((el) =>
      /remove file|remove attachment|delete file|delete attachment|cancel upload|删除附件|移除文件|移除附件/i.test(readButtonIconAndLabelText(el))).length;
    const countAttachmentChips = () => querySelectorAllDeep('ucs-file-chip, ucs-attachment, ucs-context-chip, mat-chip, md-input-chip, [data-file-name]').filter(isElementVisible).length;
    const composerInputForScope = findComposerInput();
    const composerScope = (composerInputForScope && getComposerRootHost(composerInputForScope)) || document;
    const readComposerScopedText = () =>
      querySelectorAllDeep('*', composerScope)
        .map((el) => (el.shadowRoot ? "" : (el.textContent || "")))
        .join("\n");
    const baselineScopedText = readComposerScopedText();
    const baselineRemovable = countRemovable();
    const baselineChips = countAttachmentChips();
    const baselineImgs = querySelectorAllDeep('img', composerScope).length;

    await assignFilesToInputElement(input, files);

    const startedAt = Date.now();
    let lastKeepalive = startedAt;
    let sawUploadActivity = false;
    while (Date.now() - startedAt < 120000) {
      await sleep(350);
      if (onProgress && Date.now() - lastKeepalive >= 7500) {
        lastKeepalive = Date.now();
        onProgress();
      }
      const scopedText = readComposerScopedText();
      const errors = querySelectorAllDeep('[role="alert"], .mat-mdc-snack-bar-label, md-dialog').map((el) => el.textContent || "").join(" ");
      if (/failed|unsupported|too large|cannot upload|not supported|exceeds|无法上传|上传失败|不支持|超过限制|过大/i.test(errors)) {
        throw new Error("网页拒绝附件：" + errors.trim().slice(0, 240));
      }
      const pending = querySelectorAllDeep('[role="progressbar"],md-circular-progress,mat-progress-spinner,mat-spinner').some(isElementVisible);
      if (pending) sawUploadActivity = true;
      const chipAppeared =
        countRemovable() >= baselineRemovable + files.length ||
        countAttachmentChips() >= baselineChips + files.length ||
        querySelectorAllDeep('img', composerScope).length >= baselineImgs + files.length ||
        files.every((file) => !baselineScopedText.includes(file.name) && scopedText.includes(file.name));
      if (chipAppeared) sawUploadActivity = true;
      if (chipAppeared && !pending && (Date.now() - startedAt > 200 || typeof File === "undefined")) {
        logDriver("info", `已上传 ${files.length} 个多模态附件 (${files.map((f) => f.name).join(", ")})。`);
        return;
      }
      if (!sawUploadActivity && !pending && Date.now() - startedAt > 25000) {
        throw new Error(`网页未响应附件上传 (${files.map((f) => f.name).join(", ")})，该文件格式可能不受当前网页输入框支持。`);
      }
    }
    throw new Error("附件上传在时限内未完成，已停止发送，避免模型在缺失附件的情况下回答。");
  }

  const attachImagesToComposer = attachFilesToComposer;

  const REPLY_POSTMSG_CHUNK_THRESHOLD = 32768;
  const REPLY_POSTMSG_CHUNK_SIZE = 24576;
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

  const mainPromptReassembly = new Map();

  function cleanupRequestAttachments(requestId) {
    if (!requestId) return;
    const prefix = `${requestId}:`;
    for (const key of Array.from(mainAttachmentReassembly.keys())) {
      if (key.startsWith(prefix)) {
        mainAttachmentReassembly.delete(key);
      }
    }
  }

  function reassembleRequestAttachments(requestId, rawAttachments) {
    if (!Array.isArray(rawAttachments) || rawAttachments.length === 0) {
      cleanupRequestAttachments(requestId);
      return [];
    }
    const resolved = [];
    for (let i = 0; i < rawAttachments.length; i += 1) {
      const item = rawAttachments[i] || {};
      const fname = String(item.filename || item.name || `attachment-${i + 1}.bin`);
      const mime = String(item.mime_type || "application/octet-stream");
      let dataUrl = String(item.data_url || "");
      if (!dataUrl && item.chunked_att_key) {
        const entry = mainAttachmentReassembly.get(item.chunked_att_key);
        mainAttachmentReassembly.delete(item.chunked_att_key);
        const total = item.total_chunks || (entry ? entry.total : 0);
        dataUrl = assembleChunks(entry, total);
      }
      if (dataUrl) {
        const b64Part = dataUrl.includes(",") ? dataUrl.split(",")[1] : dataUrl;
        resolved.push({
          id: String(item.id || `att-${i}`),
          filename: fname,
          name: fname,
          mime_type: mime,
          kind: String(item.kind || item.category || "file"),
          data_url: dataUrl.startsWith("data:") ? dataUrl : `data:${mime};base64,${b64Part}`,
          bytes: decodeBase64ToUint8Array(b64Part)
        });
        continue;
      }
      const attId = String(item.id || `att-${i}`);
      const key = `${requestId}:${attId}`;
      const entry = mainAttachmentReassembly.get(key);
      mainAttachmentReassembly.delete(key);
      let dataB64 = "";
      if (typeof item.data_b64 === "string" && item.data_b64.length > 0) {
        dataB64 = item.data_b64;
      } else if (entry) {
        const total = item.total_chunks || entry.total || entry.parts.length;
        dataB64 = assembleChunks(entry, total);
      }
      resolved.push({
        id: attId,
        filename: fname,
        name: fname,
        mime_type: mime,
        kind: String(item.kind || item.category || "file"),
        data_b64: dataB64,
        data_url: `data:${mime};base64,${dataB64}`,
        bytes: item.bytes instanceof Uint8Array ? item.bytes : decodeBase64ToUint8Array(dataB64)
      });
    }
    cleanupRequestAttachments(requestId);
    return resolved;
  }


  function emitRequestComplete(payload) {
    const fullReply = String(payload.reply || "");
    if (fullReply.length > REPLY_POSTMSG_CHUNK_THRESHOLD) {
      const total = Math.ceil(fullReply.length / REPLY_POSTMSG_CHUNK_SIZE);
      for (let i = 0; i < total; i += 1) {
        emitToIsolated({
          type: "reply_chunk",
          request_id: payload.request_id,
          chunk_index: i,
          total_chunks: total,
          data: fullReply.slice(i * REPLY_POSTMSG_CHUNK_SIZE, (i + 1) * REPLY_POSTMSG_CHUNK_SIZE)
        });
      }
      emitToIsolated({
        ...payload,
        reply: "",
        chunked: true,
        total_chunks: total
      });
      return;
    }
    emitToIsolated(payload);
  }

  async function executePromptJob({
    request_id: requestId,
    prompt,
    attachments,
    image_generation: imageGeneration,
    video_generation: videoGeneration,
    callback_url: callbackUrl,
    target_model: targetModel,
    reset_chat: resetChat,
    timeout_ms: timeoutMs
  }) {
    const mediaGenerationMode = Boolean(imageGeneration || videoGeneration);
    if (activeJob && !activeJob.cancelled) {
      stopJob(activeJob);
    }

    let domEmittedLen = 0;
    let usingNetworkStream = false;
    let resolveNetworkComplete;
    const networkCompletePromise = new Promise((resolve) => {
      resolveNetworkComplete = resolve;
    });

    let lastActivityAt = Date.now();
    let lastProgressSentAt = 0;
    const job = {
      requestId,
      cancelled: false,
      networkText: "",
      citations: [],
      networkDone: false,
      onNetworkDelta(delta) {
        if (job.cancelled || !delta) return;
        usingNetworkStream = true;
        lastActivityAt = Date.now();
        emitToIsolated({
          type: "stream_chunk",
          request_id: requestId,
          delta
        });
      },
      onNetworkDone() {
        lastActivityAt = Date.now();
        resolveNetworkComplete(true);
      }
    };
    activeJob = job;

    try {
      const resolvedAttachments = reassembleRequestAttachments(requestId, attachments);
      const hasAttachments = resolvedAttachments.length > 0;
      const promptText = String(prompt || "");
      if (!promptText.trim() && !hasAttachments) {
        throw new Error("收到的请求内容为空 (len=0)，已中止空消息提交。");
      }

      if (resetChat) {
        await triggerNewChatSession();
      }

      // Wait for composer to mount before model switching (important right after New Chat!)
      for (let attempt = 0; attempt < 16; attempt += 1) {
        if (findComposerInput()) break;
        await sleep(220);
      }

      if (targetModel) {
        await ensureWebModelSelected(targetModel);
      }
      assertJobActive(job);

      // Optional Replay path if explicitly selected
      if (driverConfig.driverMode === "replay" && sharedState.lastCapturedRpc && !(attachments || []).length && !mediaGenerationMode) {
        try {
          const replayReply = await tryDirectFetchReplay(prompt, job, timeoutMs || 180000);
          assertJobActive(job);
          emitRequestComplete({
            type: "request_complete",
            request_id: requestId,
            reply: replayReply,
            callback_url: callbackUrl || "",
            citations: job.citations,
            source: "fetch_replay",
            captured_rpc: Boolean(sharedState.lastCapturedRpc),
            active_model: readDetectedWebModel() || sharedState.lastSelectedModel
          });
          return;
        } catch (replayErr) {
          assertJobActive(job);
          logDriver("warn", "Replay 回退至 DOM 混合驱动: " + replayErr.message);
        }
      }

      // Re-acquire input box AFTER model switch in case composer re-rendered
      let inputEl = null;
      for (let attempt = 0; attempt < 12; attempt += 1) {
        inputEl = findComposerInput();
        if (inputEl) break;
        await sleep(250);
      }

      if (!inputEl) {
        throw new Error(
          "未在当前页面找到 Gemini Enterprise 对话输入框。请确认已登录并进入对话界面，或在扩展弹窗中点击“页面诊断”。"
        );
      }

      if (imageGeneration) await selectImageGenerationTool();
      if (videoGeneration) await selectVideoGenerationTool();
      const baselineImageSources = new Set(querySelectorAllDeep("img").map((img) => img.currentSrc || img.src).filter(Boolean));
      const baselineMediaSources = new Set(querySelectorAllDeep("video, audio, source").map((m) => m.currentSrc || m.src).filter(Boolean));
      const baselineDownloadLinks = new Set(querySelectorAllDeep('a[download], a[href^="blob:"], a[href^="data:"]').map((a) => a.href || a.getAttribute("href")).filter(Boolean));
      const baselineReplies = collectAssistantReplyTexts();
      const baselineCount = baselineReplies.length;
      const baselineLastText = baselineCount > 0 ? baselineReplies[baselineCount - 1] : "";

      let preSubmitChipCount = 0;
      if (hasAttachments) {
        const uploadRes = await uploadAttachmentsToComposer(inputEl, resolvedAttachments, () => {
          lastActivityAt = Date.now();
          emitToIsolated({ type: "request_progress", request_id: requestId, generating: true, dom_len: 0, net_len: 0 });
        });
        preSubmitChipCount = Number(uploadRes.chipCount || 0);
        inputEl = findComposerInput() || inputEl;
      }
      assertJobActive(job);
      for (const img of querySelectorAllDeep("img")) {
        const s = img.currentSrc || img.src;
        if (s) baselineImageSources.add(s);
      }
      for (const m of querySelectorAllDeep("video, audio, source")) {
        const s = m.currentSrc || m.src;
        if (s) baselineMediaSources.add(s);
      }

      const collectNewTurnArtifacts = async (allowBriefWait = false) => {
        if (allowBriefWait) {
          const hasPendingImg = querySelectorAllDeep("img").some(
            (img) => !baselineImageSources.has(img.currentSrc || img.src) && !img.complete && isElementVisible(img)
          );
          if (hasPendingImg) await sleep(600);
        }
        const imgNodes = generatedImageNodes(baselineImageSources);
        const fileCandidates = collectMediaAndDownloadCandidates(baselineMediaSources, baselineDownloadLinks);
        const outImages = imgNodes.length > 0 ? await readGeneratedImages(imgNodes) : [];
        const outFiles = fileCandidates.length > 0 ? await readGeneratedFiles(fileCandidates) : [];
        return { outImages, outFiles };
      };
      inputEl = findComposerInput() || inputEl;
      let effectivePrompt = promptText;
      if (promptText.trim().length > 0) {
        await fillInputWithPrompt(inputEl, promptText);
      } else if (hasAttachments && !findSendButton(inputEl)) {
        await fillInputWithPrompt(inputEl, " ");
        effectivePrompt = " ";
      }
      assertJobActive(job);
      const sendMethod = await submitComposerWithVerification(
        inputEl,
        effectivePrompt,
        job,
        baselineCount,
        { hadAttachments: hasAttachments, preSubmitChipCount }
      );
      logDriver("info", `已提交问题 (${sendMethod}, len=${prompt.length})，等待响应流...`);

      const startedAt = Date.now();
      let lastDiagnosticAt = startedAt;
      const toolLabels = querySelectorAllDeep('button,[role="button"]').map(readButtonIconAndLabelText)
        .filter((label) => /create images|create videos?|select tools|生成图片|创建图片|生成视频|创建视频/i.test(label)).map((label) => label.slice(0, 120));
      logDriver("info", "当前工具状态: " + JSON.stringify(toolLabels));
      lastActivityAt = Date.now();
      const idleTimeoutMs = Math.max(Number(timeoutMs) || 600000, 180000);
      const hardCapMs = 45 * 60 * 1000;
      let latestDomCandidate = "";
      let lastDomChangeAt = Date.now();
      let sawDomGrowth = false;

      while (
        !job.cancelled &&
        Date.now() - lastActivityAt < idleTimeoutMs &&
        Date.now() - startedAt < hardCapMs
      ) {
        assertJobActive(job);
        // Fast path: if passive network stream sniffer captured and completed the RPC!
        if (!mediaGenerationMode && driverConfig.driverMode !== "dom" && job.networkDone && job.networkText.trim()) {
          const { outImages, outFiles } = await collectNewTurnArtifacts(true);
          emitRequestComplete({
            type: "request_complete",
            request_id: requestId,
            reply: job.networkText,
            output_images: outImages,
            output_files: outFiles,
            callback_url: callbackUrl || "",
            citations: job.citations,
            source: "network_stream",
            captured_rpc: Boolean(sharedState.lastCapturedRpc),
            active_model: readDetectedWebModel() || sharedState.lastSelectedModel
          });
          return;
        }

        // Wait a short tick or immediate network completion
        await (job.networkDone ? sleep(220) : Promise.race([networkCompletePromise, sleep(220)]));
        assertJobActive(job);

        if (!mediaGenerationMode && driverConfig.driverMode !== "dom" && job.networkDone && job.networkText.trim()) {
          const { outImages, outFiles } = await collectNewTurnArtifacts(true);
          emitRequestComplete({
            type: "request_complete",
            request_id: requestId,
            reply: job.networkText,
            output_images: outImages,
            output_files: outFiles,
            callback_url: callbackUrl || "",
            citations: job.citations,
            source: "network_stream",
            captured_rpc: Boolean(sharedState.lastCapturedRpc),
            active_model: readDetectedWebModel() || sharedState.lastSelectedModel
          });
          return;
        }

        // Inspect Shadow DOM assistant messages
        const currentReplies = collectAssistantReplyTexts();
        let candidate = "";
        if (currentReplies.length > baselineCount) {
          candidate = currentReplies.slice(baselineCount).join("\n\n").trim();
        } else if (currentReplies.length > 0) {
          const tail = currentReplies[currentReplies.length - 1];
          if (tail && tail !== baselineLastText && tail !== prompt.trim()) {
            candidate = tail;
          }
        }

        if (candidate && candidate !== latestDomCandidate) {
          sawDomGrowth = true;
          lastDomChangeAt = Date.now();
          lastActivityAt = Date.now();
          if (!usingNetworkStream && candidate.length > domEmittedLen) {
            if (candidate.startsWith(latestDomCandidate)) {
              const delta = candidate.slice(domEmittedLen);
              if (delta) {
                domEmittedLen = candidate.length;
                emitToIsolated({
                  type: "stream_chunk",
                  request_id: requestId,
                  delta
                });
              }
            }
          }
          latestDomCandidate = candidate;
        }

        const generating = isPageStillGenerating();
        if (generating) {
          lastActivityAt = Date.now();
        }
        if (
          (generating || sawDomGrowth || job.networkText.length > 0) &&
          Date.now() - lastProgressSentAt >= 8000
        ) {
          lastProgressSentAt = Date.now();
          emitToIsolated({
            type: "request_progress",
            request_id: requestId,
            generating: Boolean(generating),
            dom_len: latestDomCandidate.length,
            net_len: job.networkText.length
          });
        }
        const idleMs = Date.now() - lastDomChangeAt;
        if (Date.now() - lastDiagnosticAt >= 45000) {
          lastDiagnosticAt = Date.now();
          logDriver("info", "回复进度: " + JSON.stringify({ generating, dom: latestDomCandidate.length,
            network: job.networkText.length, blocks: currentReplies.length, baseline: baselineCount }));
        }
        if (mediaGenerationMode && !generating && Date.now() - startedAt > 2000) {
          const { outImages, outFiles } = await collectNewTurnArtifacts(false);
          if (imageGeneration && outImages.length > 0) {
            emitRequestComplete({
              type: "request_complete",
              request_id: requestId,
              reply: job.networkText || latestDomCandidate || "",
              output_images: outImages,
              output_files: outFiles,
              callback_url: callbackUrl || "",
              source: "image_generation",
              active_model: readDetectedWebModel() || sharedState.lastSelectedModel
            });
            return;
          }
          if (videoGeneration && (outFiles.length > 0 || outImages.length > 0)) {
            emitRequestComplete({
              type: "request_complete",
              request_id: requestId,
              reply: job.networkText || latestDomCandidate || "",
              output_images: outImages,
              output_files: outFiles,
              callback_url: callbackUrl || "",
              source: "video_generation",
              active_model: readDetectedWebModel() || sharedState.lastSelectedModel
            });
            return;
          }
        }

        if (!mediaGenerationMode && !generating && idleMs >= 1500) {
          const { outImages, outFiles } = (sawDomGrowth || Date.now() - startedAt > 2500)
            ? await collectNewTurnArtifacts(false)
            : { outImages: [], outFiles: [] };
          if ((sawDomGrowth && latestDomCandidate) || outImages.length > 0 || outFiles.length > 0) {
            const bestReply =
              usingNetworkStream && job.networkText.trim().length >= latestDomCandidate.length
                ? job.networkText
                : latestDomCandidate;
            emitRequestComplete({
              type: "request_complete",
              request_id: requestId,
              reply: bestReply,
              output_images: outImages,
              output_files: outFiles,
              callback_url: callbackUrl || "",
              citations: job.citations,
              source: usingNetworkStream ? "hybrid_network_dom" : "shadow_dom",
              captured_rpc: Boolean(sharedState.lastCapturedRpc),
              active_model: readDetectedWebModel() || sharedState.lastSelectedModel
            });
            return;
          }
        }
      }

      if (job.cancelled) {
        return;
      }

      if (imageGeneration) throw new Error("网页没有返回可下载的真实图片。");
      if (videoGeneration) throw new Error("网页没有返回可下载的真实视频。");
      if ((job.networkText || latestDomCandidate || "").trim()) {
        throw new Error("网页回复在时限内未完成，已停止返回不完整结果，请重试。");
      }

      throw new Error("等待 Gemini Enterprise 页面回复超时，请确认页面未处于重登录或验证码状态。");
    } catch (err) {
      if (!job.cancelled) {
        emitToIsolated({
          type: "request_error",
          request_id: requestId,
          error: err && err.message ? err.message : String(err)
        });
        stopJob(job);
      }
    } finally {
      if (activeJob === job) {
        activeJob = null;
      }
    }
  }

  function diagnoseCurrentPage() {
    const inputEl = findComposerInput();
    const sendBtn = inputEl ? findSendButton(inputEl) : null;
    const modelTrigger = findModelSelectorTrigger();
    const newChatBtn = findNewChatButton();
    const detectedModel = readDetectedWebModel();
    const replies = collectAssistantReplyTexts();
    return {
      ok: true,
      driverVersion: DRIVER_VERSION,
      visibilityState: document.visibilityState || "visible",
      hidden: Boolean(document.hidden),
      url: window.location.href,
      cleanNewChatUrl: computeCleanNewChatUrl(window.location.href) || null,
      title: document.title,
      inputFound: Boolean(inputEl),
      inputTag: inputEl ? `${inputEl.tagName.toLowerCase()}.${String(inputEl.className || "").replace(/\s+/g, ".")}` : null,
      inputChain: inputEl ? getShadowHostChainTagString(inputEl) : null,
      sendButtonFound: Boolean(sendBtn),
      sendButtonLabel: sendBtn
        ? (getElementTooltipOrLabel(sendBtn) || sendBtn.textContent || "").trim().slice(0, 50)
        : null,
      newChatButtonFound: Boolean(newChatBtn),
      newChatButtonLabel: newChatBtn
        ? (getElementTooltipOrLabel(newChatBtn) || newChatBtn.textContent || "").trim().slice(0, 50)
        : null,
      modelSelectorFound: Boolean(modelTrigger),
      modelSelectorChain: modelTrigger ? getShadowHostChainTagString(modelTrigger) : null,
      detectedModelLabel: detectedModel || null,
      activeModel: detectedModel || sharedState.lastSelectedModel || null,
      assistantBlocksCount: replies.length,
      capturedRpc: sharedState.lastCapturedRpc
        ? { url: sharedState.lastCapturedRpc.url.slice(0, 120), capturedAt: sharedState.lastCapturedRpc.capturedAt }
        : null,
      driverMode: driverConfig.driverMode
    };
  }

  // =========================================================================
  // 6. Message Listener & Public Diagnostic/Test API
  // =========================================================================

  function onIsolatedMessage(event) {
    if (event.source !== window || event.origin !== window.location.origin) {
      return;
    }
    const data = event.data;
    if (!data || data.channel !== TO_MAIN_CHANNEL) {
      return;
    }

    const { action, payload = {}, callId } = data;

    if (action === "sw_sleep_done") {
      const resolver = pendingIsolatedSleep.get(payload.sleep_id);
      if (resolver) {
        pendingIsolatedSleep.delete(payload.sleep_id);
        resolver();
      }
      return;
    }

    if (action === "briefly_activate_tab_done") {
      const key = payload.req_id || payload.activate_id;
      const resolver = pendingBriefActivate.get(key);
      if (resolver) {
        pendingBriefActivate.delete(key);
        resolver(payload);
      }
      return;
    }

    if (action === "configure") {
      if (payload.driverMode) driverConfig.driverMode = payload.driverMode;
      if (payload.customModelSelector !== undefined) {
        driverConfig.customModelSelector = String(payload.customModelSelector || "").trim();
      }
      if (payload.customInputSelector !== undefined) {
        driverConfig.customInputSelector = String(payload.customInputSelector || "").trim();
      }
      if (payload.customSendSelector !== undefined) {
        driverConfig.customSendSelector = String(payload.customSendSelector || "").trim();
      }
      if (payload.customReplySelector !== undefined) {
        driverConfig.customReplySelector = String(payload.customReplySelector || "").trim();
      }
      if (callId) {
        emitToIsolated({ ok: true }, callId);
      }
      return;
    }

    if (action === "attachment_chunk") {
      const key = payload.att_key || `${payload.request_id}:${payload.attachment_id}`;
      let entry = mainAttachmentReassembly.get(key);
      if (!entry) {
        entry = { parts: [], total: payload.total_chunks || 0 };
        mainAttachmentReassembly.set(key, entry);
      }
      entry.parts[payload.chunk_index] = payload.data || "";
      if (payload.total_chunks) entry.total = payload.total_chunks;
      return;
    }

    if (action === "prompt_chunk") {
      let entry = mainPromptReassembly.get(payload.request_id);
      if (!entry) {
        entry = { parts: [], total: payload.total_chunks || 0 };
        mainPromptReassembly.set(payload.request_id, entry);
      }
      entry.parts[payload.chunk_index] = payload.data || "";
      if (payload.total_chunks) entry.total = payload.total_chunks;
      return;
    }

    if (action === "execute_prompt") {
      let finalPrompt = payload.prompt || "";
      if (payload.chunked) {
        const entry = mainPromptReassembly.get(payload.request_id);
        mainPromptReassembly.delete(payload.request_id);
        const total = payload.total_chunks || (entry ? entry.total : 0);
        try {
          finalPrompt = assembleChunks(entry, total);
        } catch (error) {
          emitToIsolated({type: "request_error", request_id: payload.request_id, error: error.message});
          return;
        }
      }
      void executePromptJob({ ...payload, prompt: finalPrompt, trustedPolicyReady: Boolean(trustedPolicy) });
      return;
    }

    if (action === "cancel_request") {
      if (activeJob && (!payload.request_id || activeJob.requestId === payload.request_id)) {
        stopJob(activeJob);
      }
      return;
    }

    if (action === "diagnose") {
      emitToIsolated(diagnoseCurrentPage(), callId);
      return;
    }

    if (action === "new_chat") {
      void (async () => {
        const res = await triggerNewChatSession();
        emitToIsolated(res, callId);
      })();
      return;
    }

    if (action === "select_model") {
      void (async () => {
        const res = await ensureWebModelSelected(payload.target_model);
        emitToIsolated(res, callId);
      })();
    }
  }

  window.addEventListener("message", onIsolatedMessage);

  globalThis.__GEB_MAIN_DRIVER_CLEANUP__ = () => {
    window.removeEventListener("message", onIsolatedMessage);
  };

  globalThis.__GEB_MAIN_API__ = {
    version: DRIVER_VERSION,
    stripIconLigatures,
    scoreModelOptionMatch,
    computeCleanNewChatUrl,
    findComposerInput,
    findSendButton,
    findBestFileInput,
    findComposerAttachButton,
    uploadAttachmentsToComposer,
    fillInputWithPrompt,
    attachImagesToComposer,
    attachFilesToComposer,
    submitComposer,
    submitComposerWithVerification,
    isPageStillGenerating,
    findNewChatButton,
    triggerNewChatSession,
    findModelSelectorTrigger,
    readDetectedWebModel,
    collectVisibleModelOptions,
    ensureWebModelSelected,
    executePromptJob,
    collectAssistantReplyTexts,
    diagnoseCurrentPage
  };

  logDriver("info", `MAIN 页面混合驱动 v${DRIVER_VERSION} 已就绪`);
  emitToIsolated({
    type: "driver_ready",
    captured_rpc: Boolean(sharedState.lastCapturedRpc),
    active_model: readDetectedWebModel() || sharedState.lastSelectedModel
  });
})();
