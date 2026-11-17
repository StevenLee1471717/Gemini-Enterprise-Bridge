// MAIN-world driver: DOM controls the composer; the original RPC is the only reply source.

(() => {
  const DRIVER_VERSION = "2.8.6";

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
    driverMode: "auto", // original RPC only
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
    /streamAnswer/i,
    /streamGenerateContent/i,
    /(?::|\/|\.)(?:widget)?converse(?:Conversation)?(?=$|[/?#])/i,
    /(?::|\/|\.)(?:widget)?assist(?=$|[/?#])/i,
    /(?::|\/|\.)(?:widget)?answer(?:Query)?(?=$|[/?#])/i,
    /ConversationalSearch/i,
    /discoveryengine.*assist/i
  ];

  function isLikelyChatRpc(url, method) {
    if (!url || String(method || "GET").toUpperCase() !== "POST") {
      return false;
    }
    if (
      /batchexecute|(?:create|update|delete|get|list|purge|lookup)(?:Widget)?(?:Session|Conversation|Config)s?|addUserEvents|logClient/i.test(
        url
      )
    ) {
      return false;
    }
    return CHAT_RPC_PATTERNS.some((re) => re.test(url));
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

  function stripTrailingWebSuggestions(text) {
    const raw = String(text || "").replace(
      /(^[ \t]{0,3}```[ \t]*)(?=(?:`{3,}|~{3,})[ \t]*suggested_questions\b|\[\[(?:suggested_questions|section:suggested_questions)\]\])/gim,
      "$1\n"
    );
    const lines = raw.split("\n");
    const kept = [];
    let outerFence = "";
    let skipSuggestionFence = "";
    const fenceLineRe = /^[ \t]{0,3}(`{3,}|~{3,})([^\r\n]*)\r?$/;

    for (let i = 0; i < lines.length; i += 1) {
      const line = lines[i];
      if (skipSuggestionFence) {
        const mClose = line.match(fenceLineRe);
        if (
          mClose &&
          mClose[1][0] === skipSuggestionFence[0] &&
          mClose[1].length >= skipSuggestionFence.length &&
          !mClose[2].trim()
        ) {
          skipSuggestionFence = "";
        }
        continue;
      }
      if (!outerFence && /^[ \t]*\[\[(?:suggested_questions|section:suggested_questions)\]\]/i.test(line)) {
        break;
      }
      const cleanedLine = line.replace(/\[\[section:[^\]]*\]\]/gi, "");
      const mFence = cleanedLine.match(fenceLineRe);
      if (!outerFence) {
        if (mFence && /^suggested_questions(?:\b|$)/i.test(mFence[2].trim())) {
          skipSuggestionFence = mFence[1];
          continue;
        }
        if (mFence) {
          outerFence = mFence[1];
        }
      } else if (
        mFence &&
        mFence[1][0] === outerFence[0] &&
        mFence[1].length >= outerFence.length &&
        !mFence[2].trim()
      ) {
        outerFence = "";
      }
      kept.push(line);
    }
    return kept.join("\n");
  }

  function stripExplicitWebDecorations(text) {
    return stripTrailingWebSuggestions(text).replace(/\[\[section:[^\]]*\]\]/gi, "\n\n");
  }

  function replaceClientToolFences(text, replace) {
    const opening = /(?:^[ \t]{0,3}|(?:\[\[section:[^\r\n]*?\]\][ \t]*)+)(?:\[\[section:[^\r\n]*?\]\][ \t]*)*(`{3,}|~{3,})([^\r\n]*)\r?$/gmi;
    const wsOrSectionRe = /(?:\s+|\[\[section:[^\r\n]*?\]\])+/yi;
    const exactCloseRe = /(?:```[ \t]*(?:(?:`{3,}|~{3,})[ \t]*)?suggested_questions\b[^\r\n]*(?:\r?\n[\s\S]*?^[ \t]{0,3}(?:`{3,}|~{3,})[ \t]*\r?$|\r?\n[\s\S]*$|\r?$)|```[ \t]*(?=(?:\[\[(?:section:[^\r\n]*?|suggested_questions|section:suggested_questions)\]\][ \t]*(?:\r?$|\r?\n)|\r?$|\r?\n)))/ymi;
    let match, cursor = 0, output = "";
    while ((match = opening.exec(text))) {
      const fence = match[1];
      const language = match[2].replace(/\[\[section:[^\]]*\]\]/gi, "").trim().toLowerCase();
      const closing = new RegExp("^[ \\t]{0,3}" + fence[0] + "{" + fence.length + ",}[ \\t]*\\r?$", "gm");
      closing.lastIndex = opening.lastIndex;
      if (language !== "codex_tool_call" || fence !== "```") {
        const end = closing.exec(text);
        opening.lastIndex = end ? closing.lastIndex : text.length;
        continue;
      }
      let pos = opening.lastIndex;
      wsOrSectionRe.lastIndex = pos;
      const leadWs = wsOrSectionRe.exec(text);
      if (leadWs) pos = wsOrSectionRe.lastIndex;
      if (text[pos] !== "{") continue;
      const parsedValues = [];
      let parseOk = true;
      while (pos < text.length && text[pos] === "{") {
        let depth = 0, quoted = false, escaped = false, end = pos;
        for (; end < text.length; end += 1) {
          const char = text[end];
          if (quoted) {
            if (escaped) escaped = false;
            else if (char === "\\") escaped = true;
            else if (char === '"') quoted = false;
          } else if (char === '"') quoted = true;
          else if (char === "{" || char === "[") depth += 1;
          else if ((char === "}" || char === "]") && --depth === 0) { end += 1; break; }
        }
        if (depth !== 0) { parseOk = false; break; }
        try {
          parsedValues.push(JSON.parse(text.slice(pos, end)));
        } catch (_) {
          parseOk = false;
          break;
        }
        pos = end;
        wsOrSectionRe.lastIndex = pos;
        const midWs = wsOrSectionRe.exec(text);
        if (midWs) pos = wsOrSectionRe.lastIndex;
      }
      if (!parseOk || parsedValues.length === 0) continue;
      exactCloseRe.lastIndex = pos;
      const close = exactCloseRe.exec(text);
      if (!close) continue;
      output += text.slice(cursor, match.index) + parsedValues.map((v) => replace(v)).join("\n");
      cursor = exactCloseRe.lastIndex;
      opening.lastIndex = cursor;
    }
    return output + text.slice(cursor);
  }

  function replyFingerprint(text) {
    const stable = (value) => {
      if (Array.isArray(value)) return value.map(stable);
      if (value && typeof value === "object") {
        return Object.fromEntries(Object.keys(value).sort().map((key) => [key, stable(value[key])]));
      }
      return value;
    };
    text = String(text || "");
    // Protect command strings before stripping webpage decorations from prose.
    let marker = "\0GEB_CALL";
    while (text.includes(marker)) marker += "_";
    const calls = [];
    const remember = (value) => {
      calls.push(JSON.stringify(stable(value)));
      return marker + (calls.length - 1) + "\0";
    };
    text = replaceClientToolFences(text, remember);
    text = stripExplicitWebDecorations(text);
    text = text
      .replace(/\r\n?/g, "\n")
      .replace(/[ \t]+\n/g, "\n")
      .replace(/\n{2,}/g, "\n")
      .replace(new RegExp("[ \\t\\r\\n]+(?=" + marker + ")", "g"), "\n")
      .replace(new RegExp("(" + marker + "\\d+\\0)[ \\t\\r\\n]+", "g"), "$1\n")
      .trim();
    return text.replace(new RegExp(marker + "(\\d+)\\0", "g"), (_, index) => "CLIENT_CALL:" + calls[Number(index)]);
  }

  function findLastUnclosedToolFence(text) {
    const opening = /(?:^[ \t]{0,3}|(?:\[\[section:[^\r\n]*?\]\][ \t]*)+)```[ \t]*(?:\[\[section:[^\r\n]*?\]\][ \t]*)*codex_tool_call(?:[ \t]*\[\[section:[^\r\n]*?\]\])*[ \t]*\r?$/gmi;
    let m, lastOpen = -1;
    while ((m = opening.exec(text))) {
      const tailProbe = text.slice(m.index);
      if (replaceClientToolFences(tailProbe, () => "") === tailProbe) {
        lastOpen = m.index;
      }
    }
    return lastOpen;
  }

  function extractSnapshotSuffixCore(snapshotText, existingText) {
    const fpExisting = replyFingerprint(existingText);
    if (!fpExisting) return null;
    const fpSnapshot = replyFingerprint(snapshotText);
    if (fpSnapshot === fpExisting) return "";
    if (fpExisting.startsWith(fpSnapshot)) return "";
    if (!fpSnapshot.startsWith(fpExisting)) return null;
    const candidates = [];
    for (let i = 0; i < snapshotText.length; i += 1) {
      if (snapshotText[i] === "\n") candidates.push(i);
    }
    for (const cut of candidates) {
      if (replyFingerprint(snapshotText.slice(0, cut)) === fpExisting) {
        return snapshotText.slice(cut);
      }
    }
    let lo = 1;
    let hi = snapshotText.length;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if (replyFingerprint(snapshotText.slice(0, mid)).length < fpExisting.length) {
        lo = mid + 1;
      } else {
        hi = mid;
      }
    }
    const startScan = Math.max(1, lo - 8);
    const endScan = Math.min(snapshotText.length, lo + 8);
    for (let cut = startScan; cut <= endScan; cut += 1) {
      if (replyFingerprint(snapshotText.slice(0, cut)) === fpExisting) {
        return snapshotText.slice(cut);
      }
    }
    return null;
  }

  function extractSnapshotSuffix(snapshotText, existingText) {
    const cleanSnapshot = stripTrailingWebSuggestions(snapshotText);
    const direct = extractSnapshotSuffixCore(cleanSnapshot, existingText);
    if (direct !== null) {
      return direct;
    }
    const unclosedIdx = findLastUnclosedToolFence(existingText);
    if (unclosedIdx >= 0) {
      const closeToken = existingText.endsWith("\n") ? "```" : "\n```";
      const closedProbe = existingText + closeToken;
      if (replaceClientToolFences(closedProbe.slice(unclosedIdx), () => "") !== closedProbe.slice(unclosedIdx)) {
        const afterClose = extractSnapshotSuffixCore(cleanSnapshot, closedProbe);
        if (afterClose !== null) {
          return stripExplicitWebDecorations(afterClose).trim() ? closeToken + afterClose : closeToken;
        }
      }
      const prefixBefore = existingText.slice(0, unclosedIdx);
      const fpBefore = prefixBefore ? replyFingerprint(prefixBefore) : "";
      const unclosedTail = existingText.slice(unclosedIdx);
      let searchFrom = 0;
      while (searchFrom <= cleanSnapshot.length - unclosedTail.length) {
        const pos = cleanSnapshot.indexOf(unclosedTail, searchFrom);
        if (pos === -1) break;
        const snapBefore = cleanSnapshot.slice(0, pos);
        const prefixMatches = fpBefore
          ? replyFingerprint(snapBefore) === fpBefore
          : !stripExplicitWebDecorations(snapBefore).trim();
        if (prefixMatches) {
          return cleanSnapshot.slice(pos + unclosedTail.length);
        }
        searchFrom = pos + 1;
      }
    }
    return null;
  }

  function parseJsonPacketIntoJob(jsonObj, job, streamToken = null) {
    if (!job || job.cancelled || job.completed) {
      return;
    }
    if (streamToken && job.primaryStreamToken && job.primaryStreamToken !== streamToken) {
      return;
    }
    const texts = [];
    const refs = [];
    extractStructuredPieces(jsonObj, texts, refs);
    if (streamToken && !job.primaryStreamToken && texts.some((p) => p && p.text)) {
      job.primaryStreamToken = streamToken;
    }

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
          const sep =
            piece.snapshot &&
            job.networkText &&
            !job.networkText.endsWith("\n") &&
            !combined.startsWith("\n")
              ? "\n"
              : "";
          job.networkText += sep + combined;
          job.onNetworkDelta(sep + combined);
        }
      }
    }
  }

  /**
   * Incrementally scans a streaming JSON array `[{...}, {...}]` or SSE/NDJSON stream
   * and invokes `onObject(parsed)` as soon as each top-level element closes.
   */
  async function consumeRpcReadableStream(readableStream, job) {
    if (job && job.primaryStreamToken) {
      return;
    }
    const streamToken = Symbol("rpc_stream");
    if (job) {
      if (!job.activeStreams) job.activeStreams = new Set();
      job.activeStreams.add(streamToken);
    }
    const reader = readableStream.getReader();
    const cancelStream = () => { void reader.cancel().catch(() => {}); };
    if (job && !job.cancelStream) {
      job.cancelStream = cancelStream;
    }
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
    let xssiChecked = false;
    let parsedPackets = 0;
    let rpcErrorMessage = "";

    const scanBuffer = () => {
      if (job && job.primaryStreamToken && job.primaryStreamToken !== streamToken) {
        return;
      }
      if (!xssiChecked) {
        const trimmedLead = buffer.replace(/^\s+/, "");
        if (!trimmedLead) {
          cursor = buffer.length;
          return;
        }
        if ((")]}'".startsWith(trimmedLead) || /^\)\]\}'[ \t]*$/.test(trimmedLead)) && !streamEnded) {
          return;
        }
        const xssiMatch = buffer.match(/^\s*\)\]\}'[ \t]*(?:\r?\n)?/);
        if (xssiMatch) {
          buffer = buffer.slice(xssiMatch[0].length);
          cursor = 0;
        }
        xssiChecked = true;
      }

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
                  parsedPackets += 1;
                  if (
                    parsed &&
                    typeof parsed === "object" &&
                    !Array.isArray(parsed) &&
                    parsed.error &&
                    typeof parsed.error === "object" &&
                    typeof parsed.error.message === "string" &&
                    parsed.error.message
                  ) {
                    rpcErrorMessage = parsed.error.message;
                  }
                  parseJsonPacketIntoJob(parsed, job, streamToken);
                } catch (_) {
                  invalidPacket = true;
                }
              }
            } else {
              invalidPacket = true;
            }
          } else if (ch === "[") {
            arrayDepth += 1;
          } else if (ch === "]") {
            if (arrayDepth > 0) {
              arrayDepth -= 1;
            } else {
              invalidPacket = true;
            }
          }
        }
        cursor += 1;
      }

      // Compact processed prefix when outside any top-level object
      if (objDepth === 0 && cursor > 8192) {
        buffer = buffer.slice(cursor);
        cursor = 0;
      }
    };

    try {
      while (true) {
        const { value, done } = await reader.read();
        if (done) {
          streamEnded = true;
          buffer += decoder.decode();
          scanBuffer();
          break;
        }
        buffer += decoder.decode(value, { stream: true });
        scanBuffer();
      }
    } catch (_) {
      streamEnded = false;
    } finally {
      if (job && job.activeStreams) {
        job.activeStreams.delete(streamToken);
      }
      const isPrimary = Boolean(job && job.primaryStreamToken === streamToken);
      const noOtherStream = Boolean(
        job &&
        !job.primaryStreamToken &&
        (!job.activeStreams || job.activeStreams.size === 0)
      );
      if (job && !job.cancelled && !job.completed) {
        if (
          isPrimary &&
          streamEnded &&
          parsedPackets > 0 &&
          objDepth === 0 &&
          arrayDepth === 0 &&
          !inString &&
          !invalidPacket &&
          job.networkText
        ) {
          job.networkDone = true;
          job.onNetworkDone();
        } else if (
          (isPrimary || noOtherStream) &&
          (!streamEnded || objDepth > 0 || arrayDepth > 0 || inString || invalidPacket)
        ) {
          job.networkError = "网页响应流中断或数据不完整。";
          logDriver("warn", job.networkError);
        } else if ((isPrimary || noOtherStream) && rpcErrorMessage && !job.networkText) {
          job.networkError = `网页接口返回错误: ${rpcErrorMessage}`;
          logDriver("warn", job.networkError);
        }
      }
      reader.releaseLock();
      if (job && job.cancelStream === cancelStream) delete job.cancelStream;
    }
  }

  const nativeFetch = sharedState.nativeFetch;
  sharedState.getActiveJob = () => activeJob;
  sharedState.consumeStream = consumeRpcReadableStream;
  sharedState.parsePacket = parseJsonPacketIntoJob;
  sharedState.checkChatRpc = isLikelyChatRpc;
  sharedState.onRpcCaptured = () => {
    emitToIsolated({ type: "driver_ready", captured_rpc: true, driver_version: DRIVER_VERSION });
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
      const candidateJob =
        matchedRpc && sharedState.getActiveJob ? sharedState.getActiveJob() : null;
      const curJob =
        candidateJob &&
        candidateJob.awaitingRpc !== false &&
        !candidateJob.cancelled &&
        !candidateJob.completed &&
        !candidateJob.primaryStreamToken
          ? candidateJob
          : null;
      if (curJob) {
        curJob.submissionStarted = true;
      }
      let response;
      try {
        response = await sharedState.nativeFetch(input, init);
      } catch (error) {
        if (
          curJob &&
          !curJob.cancelled &&
          !curJob.primaryStreamToken &&
          (!curJob.activeStreams || curJob.activeStreams.size === 0)
        ) {
          curJob.networkError = "网页请求失败，连接已中断。";
        }
        throw error;
      }
      if (
        curJob &&
        !curJob.cancelled &&
        !curJob.primaryStreamToken &&
        (!curJob.activeStreams || curJob.activeStreams.size === 0) &&
        response &&
        response.ok === false
      ) {
        curJob.networkError = `网页请求失败 (HTTP ${response.status})。`;
      }

      if (curJob && !curJob.cancelled && !curJob.primaryStreamToken && response && response.body) {
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
      const candidateJob = sharedState.getActiveJob ? sharedState.getActiveJob() : null;
      if (
        sharedState.checkChatRpc &&
        sharedState.checkChatRpc(this.__gebUrl, this.__gebMethod, bodyStr) &&
        candidateJob &&
        candidateJob.awaitingRpc !== false &&
        !candidateJob.cancelled &&
        !candidateJob.completed &&
        !candidateJob.primaryStreamToken
      ) {
        const jobRef = candidateJob;
        jobRef.submissionStarted = true;
        let lastProcessedIdx = 0;
        const xhrToken = Symbol("xhr_stream");
        this.addEventListener("readystatechange", () => {
          if (jobRef.primaryStreamToken && jobRef.primaryStreamToken !== xhrToken) return;
          if (this.readyState >= 3 && typeof this.responseText === "string") {
            const fresh = this.responseText.slice(lastProcessedIdx);
            if (fresh && this.readyState === 4) {
              lastProcessedIdx = this.responseText.length;
              try {
                const cleaned = this.responseText.replace(/^\)\]\}'\s*/, "");
                const parsed = JSON.parse(cleaned);
                if (sharedState.parsePacket) {
                  sharedState.parsePacket(parsed, jobRef, xhrToken);
                }
                if (jobRef.primaryStreamToken === xhrToken && jobRef.networkText) {
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
    if (root && root.shadowRoot) {
      roots.push(root.shadowRoot);
      queue.push(root.shadowRoot);
    }
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

  async function submitComposerWithVerification(inputEl, promptText, job = null, baselineCount = 0) {
    const promptTrimLen = String(promptText || "").trim().length;
    const hasSubmissionStarted = () => {
      if (job && (job.submissionStarted || job.networkText.length > 0 || job.networkDone)) return true;
      if (isPageStillGenerating()) return true;
      if (collectAssistantReplyTexts().length > baselineCount) return true;
      const curText = readComposerCurrentText(inputEl);
      if (curText.length === 0) return true;
      if (promptTrimLen > 10 && curText.length < Math.min(20, promptTrimLen * 0.2)) return true;
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
    if (!readComposerCurrentText(activeInput) && !hasSubmissionStarted()) {
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
    for (let i = 0; i < 20 && cur; i += 1) {
      const tag = (cur.tagName || "").toLowerCase();
      if (
        tag.includes("search-box") ||
        tag.includes("search-bar") ||
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
    "ucs-response-markdown",
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
    const documents = querySelectorAllDeep(".markdown-document", el);
    let text = (documents.length
      ? documents.map(node => node.innerText || node.textContent || "").join("\n\n")
      : (el.innerText || el.textContent || "")).trim();
    return text.replace(/\n{3,}/g, "\n\n").trim();
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
  // 4. Composer tools and generated artifacts
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
    let clean = String(b64 || "").trim().replace(/-/g, "+").replace(/_/g, "/").replace(/[^A-Za-z0-9+/=]/g, "");
    const rem = clean.replace(/=+$/, "").length % 4;
    if (rem === 1) throw new Error("附件 base64 内容无效。");
    clean = clean.replace(/=+$/, "") + (rem === 2 ? "==" : rem === 3 ? "=" : "");
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

  function normalizeMimeType(rawMime, defaultMime = "application/octet-stream") {
    const cleaned = String(rawMime || "").split(";")[0].split(",")[0].trim();
    return cleaned && cleaned.includes("/") ? cleaned : defaultMime;
  }

  function dataUrlToFile(dataUrl, fallbackName, fallbackMime) {
    const raw = String(dataUrl || "").trim();
    if (!raw || /^(?:https?|blob|file):/i.test(raw)) {
      throw new Error("附件内容格式无效（需要 base64 data URL）。");
    }
    let headerMime = "";
    let bytes;
    if (/^data:/i.test(raw)) {
      const commaIdx = raw.indexOf(",");
      if (commaIdx < 0) {
        throw new Error("附件内容格式无效（需要 base64 data URL）。");
      }
      const header = raw.slice(5, commaIdx);
      const parts = header.split(";").map((s) => s.trim()).filter(Boolean);
      const isBase64 = parts.some((p) => p.toLowerCase() === "base64");
      if (parts.length > 0 && parts[0].toLowerCase() !== "base64") {
        headerMime = parts[0];
      }
      const body = raw.slice(commaIdx + 1);
      if (isBase64) {
        bytes = decodeBase64Bytes(body);
      } else {
        let decodedText = body;
        try {
          decodedText = decodeURIComponent(body);
        } catch (_) {}
        if (typeof TextEncoder === "function") {
          bytes = new TextEncoder().encode(decodedText);
        } else {
          const utf8 = unescape(encodeURIComponent(decodedText));
          bytes = new Uint8Array(utf8.length);
          for (let i = 0; i < utf8.length; i += 1) bytes[i] = utf8.charCodeAt(i);
        }
      }
    } else {
      const compact = raw.replace(/\s+/g, "");
      if (!compact || /[^A-Za-z0-9+/=_-]/.test(compact) || compact.replace(/=+$/, "").length % 4 === 1) {
        throw new Error("附件内容格式无效（需要 base64 data URL）。");
      }
      bytes = decodeBase64Bytes(compact);
    }
    const mime = normalizeMimeType(fallbackMime || headerMime, "application/octet-stream");
    return new File([bytes], fallbackName || "attachment.bin", { type: mime });
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

  function composerAttachmentControls() {
    const composer = findComposerInput();
    const scope = composer && getComposerRootHost(composer);
    if (!scope) return [];
    // Remove controls are transparent until hovered; their existence still identifies a file.
    const controls = querySelectorAllDeep('button, md-icon-button, ucs-icon-button, [role="button"]', scope).filter((el) =>
      /remove file|remove attachment|delete file|delete attachment|cancel upload|删除附件|移除文件|移除附件/i.test(readButtonIconAndLabelText(el)));
    // Material wrappers and their shadow buttons represent the same attachment.
    return controls.filter((el) => !controls.some((other) => other !== el && containsDeep(other, el)));
  }

  function buildFileDataTransfer(files) {
    if (typeof DataTransfer === "function") {
      const transfer = new DataTransfer();
      for (const file of files) transfer.items.add(file);
      return transfer;
    }
    const fileList = files.slice();
    fileList.item = (idx) => fileList[idx] || null;
    return {
      dropEffect: "copy",
      effectAllowed: "all",
      files: fileList,
      items: files.map((f) => ({ kind: "file", type: f.type || "application/octet-stream", getAsFile: () => f })),
      types: ["Files"],
      getData: () => ""
    };
  }

  function dispatchClipboardPasteAndDropFiles(targetInput, composerHost, files) {
    const dt = buildFileDataTransfer(files);
    const pasteTarget = targetInput || composerHost || document.body;
    let sent = false;
    if (pasteTarget) {
      try {
        if (typeof pasteTarget.focus === "function") pasteTarget.focus();
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
        pasteTarget.dispatchEvent(pasteEv);
        sent = true;
      } catch (_) {}
    }
    const dropTarget = composerHost || pasteTarget;
    if (dropTarget) {
      try {
        for (const evType of ["dragenter", "dragover", "drop"]) {
          let dragEv;
          if (typeof DragEvent === "function") {
            dragEv = new DragEvent(evType, {
              bubbles: true,
              composed: true,
              cancelable: true,
              dataTransfer: dt
            });
          } else {
            dragEv = new Event(evType, { bubbles: true, composed: true, cancelable: true });
          }
          if (!dragEv.dataTransfer) {
            try {
              Object.defineProperty(dragEv, "dataTransfer", { value: dt, configurable: true });
            } catch (_) {}
          }
          dropTarget.dispatchEvent(dragEv);
        }
        sent = true;
      } catch (_) {}
    }
    return sent;
  }

  async function attachFilesToComposer(attachments, onProgress = null) {
    if (composerAttachmentControls().length) {
      throw new Error("输入框中残留了前次未提交的附件，已停止发送，避免把错误文件带入当前请求。");
    }
    if (!attachments || attachments.length === 0) return;
    const files = [];
    for (const att of attachments) {
      const cleanMime = normalizeMimeType(att && att.mime_type, "application/octet-stream");
      let dataUrl = String((att && att.data_url) || "").trim();
      const chunkKey = (att && att.chunked_att_key) || (att && att.total_chunks && (att.id || att.attachment_id)) || "";
      if (!dataUrl && chunkKey && mainAttachmentReassembly.has(chunkKey)) {
        const entry = mainAttachmentReassembly.get(chunkKey);
        mainAttachmentReassembly.delete(chunkKey);
        const total = att.total_chunks || (entry ? entry.total : 0);
        dataUrl = assembleChunks(entry, total);
      }
      if (!dataUrl) {
        const rawB64 = (att && (att.data_b64 || att.b64_json || att.file_data || att.data)) || "";
        if (typeof rawB64 === "string" && rawB64.trim().length > 0) {
          dataUrl = /^data:/i.test(rawB64.trim())
            ? rawB64.trim()
            : `data:${cleanMime};base64,${rawB64.replace(/\s+/g, "")}`;
        }
      }
      files.push(dataUrlToFile(dataUrl, (att && (att.name || att.filename)) || "attachment.bin", cleanMime));
    }

    const composerInputForScope = findComposerInput();
    const composerScope = (composerInputForScope && getComposerRootHost(composerInputForScope)) || document;
    const readComposerScopedText = () =>
      querySelectorAllDeep('*', composerScope)
        .map((el) => (el.shadowRoot ? "" : (el.textContent || "")))
        .join("\n");
    const baselineScopedText = readComposerScopedText();
    const baselineControlCount = composerAttachmentControls().length;

    let input = findSuitableFileInput(files);
    if (!input) {
      const composerInput = findComposerInput();
      const composerHost = composerInput ? getComposerRootHost(composerInput) : null;
      const addButton = querySelectorAllDeep('button, md-icon-button, ucs-icon-button, [role="button"]').find((el) => {
        if (!isElementVisible(el) || el.disabled) return false;
        const meta = `${el.getAttribute("aria-label") || ""} ${el.getAttribute("title") || ""} ${getElementTooltipOrLabel(el)} ${readButtonIconAndLabelText(el)}`;
        if (/\b(add files|add files or sources|attach|upload files|add_circle|attach_file|paperclip)\b|添加文件|添加来源|上传文件|上传附件|附件/i.test(meta)) {
          return true;
        }
        return Boolean(composerHost && containsDeep(composerHost, el) && /^\s*(add|\+|＋|attach_file)\s*$/i.test((el.textContent || "").trim()));
      });
      if (addButton) {
        // Intercept HTMLInputElement.prototype.click so clicking 'Upload files' captures detached file inputs without opening OS dialog!
        const hasInputProto =
          typeof HTMLInputElement !== "undefined" &&
          HTMLInputElement.prototype &&
          typeof HTMLInputElement.prototype.click === "function";
        const origInputClick = hasInputProto ? HTMLInputElement.prototype.click : null;
        let interceptedFileInput = null;
        if (hasInputProto) {
          HTMLInputElement.prototype.click = function (...clickArgs) {
            if (this && String(this.type || "").toLowerCase() === "file") {
              interceptedFileInput = this;
              return undefined;
            }
            return origInputClick.apply(this, clickArgs);
          };
        }
        try {
          addButton.click();
          await sleep(220);
          input = interceptedFileInput || findSuitableFileInput(files);
          if (!input) {
            const upload = querySelectorAllDeep('[role="menuitem"],md-menu-item,mat-menu-item,button,span,div').find((el) =>
              /^(upload files?|upload from computer|from your computer|upload|from computer|local files?|choose files?|上传文件|从电脑上传|从设备上传|本地上传|本地文件)$/i.test(stripIconLigatures(String(el.textContent || "").trim())) && isElementVisible(el));
            if (upload) {
              upload.click();
              await sleep(150);
              if (!interceptedFileInput && isElementVisible(upload)) {
                dispatchRealisticClick(upload);
                await sleep(150);
              }
            }
          }
          input = interceptedFileInput || findSuitableFileInput(files);
          for (let attempt = 0; attempt < 15 && !input; attempt += 1) {
            await sleep(150);
            input = interceptedFileInput || findSuitableFileInput(files);
          }
        } finally {
          if (hasInputProto && origInputClick) {
            HTMLInputElement.prototype.click = origInputClick;
          }
        }
        await dismissTransientOverlays(null);
      }
    }

    let usedClipboardFallback = false;
    if (input) {
      const transfer = buildFileDataTransfer(files);
      try {
        const desc =
          typeof HTMLInputElement !== "undefined" && HTMLInputElement.prototype
            ? Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "files")
            : null;
        if (desc && typeof desc.set === "function") {
          desc.set.call(input, transfer.files);
        } else {
          input.files = transfer.files;
        }
      } catch (_) {
        input.files = transfer.files;
      }
      input.dispatchEvent(new Event("change", { bubbles: true, composed: true }));
      input.dispatchEvent(new Event("input", { bubbles: true, composed: true }));
    } else {
      const liveComposer = findComposerInput();
      const hostEl = liveComposer ? getComposerRootHost(liveComposer) : null;
      if (!dispatchClipboardPasteAndDropFiles(liveComposer, hostEl, files)) {
        throw new Error("没有找到网页的文件输入框或可粘贴输入区，附件未上传。");
      }
      usedClipboardFallback = true;
      logDriver("info", `未发现显式 file input，已通过剪贴板粘贴/拖放直接向网页注入 ${files.length} 个文件。`);
    }

    const startedAt = Date.now();
    let lastKeepalive = startedAt;
    let sawUploadActivity = false;
    let readySince = 0;
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
      const pending = querySelectorAllDeep('[role="progressbar"],[aria-busy="true"],md-circular-progress,mat-progress-spinner,mat-spinner', composerScope).some(isElementVisible);
      if (pending) sawUploadActivity = true;
      const curControlCount = composerAttachmentControls().length;
      if (curControlCount > baselineControlCount) sawUploadActivity = true;
      const chipAppeared = curControlCount === files.length ||
        (new Set(files.map((file) => file.name)).size === files.length &&
         files.every((file) => !baselineScopedText.includes(file.name) && scopedText.includes(file.name)));
      if (chipAppeared) sawUploadActivity = true;
      if (!sawUploadActivity && !pending && !usedClipboardFallback && Date.now() - startedAt > 2800) {
        usedClipboardFallback = true;
        const liveComposer = findComposerInput();
        const hostEl = liveComposer ? getComposerRootHost(liveComposer) : null;
        dispatchClipboardPasteAndDropFiles(liveComposer, hostEl, files);
        logDriver("info", `file input 未触发上传反馈，已自动切换剪贴板粘贴/拖拽直传 (${files.map((f) => f.name).join(", ")})...`);
      }
      readySince = chipAppeared && !pending ? (readySince || Date.now()) : 0;
      if (readySince && Date.now() - readySince >= 1200) {
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

  function emitRequestComplete(payload) {
    const enrichedPayload = {
      ...payload,
      tab_url:
        payload.tab_url ||
        (typeof window !== "undefined" && window.location ? String(window.location.href || "") : ""),
      tab_title:
        payload.tab_title ||
        (typeof document !== "undefined" ? String(document.title || "") : "")
    };
    const fullReply = String(enrichedPayload.reply || "");
    if (fullReply.length > REPLY_POSTMSG_CHUNK_THRESHOLD) {
      const total = Math.ceil(fullReply.length / REPLY_POSTMSG_CHUNK_SIZE);
      for (let i = 0; i < total; i += 1) {
        emitToIsolated({
          type: "reply_chunk",
          request_id: enrichedPayload.request_id,
          chunk_index: i,
          total_chunks: total,
          data: fullReply.slice(i * REPLY_POSTMSG_CHUNK_SIZE, (i + 1) * REPLY_POSTMSG_CHUNK_SIZE)
        });
      }
      emitToIsolated({
        ...enrichedPayload,
        reply: "",
        chunked: true,
        total_chunks: total
      });
      return;
    }
    emitToIsolated(enrichedPayload);
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

    let resolveNetworkComplete;
    const networkCompletePromise = new Promise((resolve) => {
      resolveNetworkComplete = resolve;
    });

    let lastActivityAt = Date.now();
    let lastProgressSentAt = 0;
    const job = {
      requestId,
      cancelled: false,
      awaitingRpc: false,
      primaryStreamToken: null,
      activeStreams: new Set(),
      networkText: "",
      citations: [],
      networkDone: false,
      onNetworkDelta(delta) {
        if (job.cancelled || job.completed || !delta) return;
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
    let chatWasReset = false;
    let promptSubmitted = false;
    let filledInputInThisJob = false;
    let composerEl = null;
    let hadPreexistingAttachments = false;

    try {
      if (!String(prompt || "").trim()) {
        throw new Error("收到的请求内容为空 (len=0)，已中止空消息提交。");
      }

      if (resetChat) {
        await triggerNewChatSession();
        chatWasReset = true;
      } else if (isPageStillGenerating()) {
        const lingeringStop = findGenerationStopButton();
        if (lingeringStop) lingeringStop.click();
        for (let waitIdx = 0; waitIdx < 16 && isPageStillGenerating(); waitIdx += 1) {
          await sleep(200);
        }
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
      hadPreexistingAttachments = composerAttachmentControls().length > 0;

      await attachFilesToComposer(attachments, () => {
        lastActivityAt = Date.now();
        emitToIsolated({ type: "request_progress", request_id: requestId, generating: true, submitted: false, tab_reset: chatWasReset, dom_len: 0, net_len: 0 });
      });
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
      await fillInputWithPrompt(inputEl, prompt);
      filledInputInThisJob = true;
      composerEl = inputEl;
      assertJobActive(job);
      job.awaitingRpc = true;
      const sendMethod = await submitComposerWithVerification(
        inputEl,
        prompt,
        job,
        baselineCount
      );
      promptSubmitted = true;
      emitToIsolated({
        type: "request_progress",
        request_id: requestId,
        generating: true,
        submitted: true,
        tab_reset: chatWasReset,
        dom_len: 0,
        net_len: 0
      });
      if ((attachments || []).length) {
        for (let attempt = 0; attempt < 12 && composerAttachmentControls().length; attempt += 1) await sleep(150);
        if (composerAttachmentControls().length) throw new Error("提交后仍有附件留在输入框，文件未完整发送，已停止等待错误结果。");
      }
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
      let sawDomGrowth = false;

      while (
        !job.cancelled &&
        Date.now() - lastActivityAt < idleTimeoutMs &&
        Date.now() - startedAt < hardCapMs
      ) {
        assertJobActive(job);
        // Fast path: if passive network stream sniffer captured and completed the RPC!
        if (!mediaGenerationMode && job.networkDone && job.networkText.trim()) {
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

        if (!mediaGenerationMode && job.networkDone && job.networkText.trim()) {
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
          lastActivityAt = Date.now();
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
              reply: job.networkText,
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
              reply: job.networkText,
              output_images: outImages,
              output_files: outFiles,
              callback_url: callbackUrl || "",
              source: "video_generation",
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
      if (!promptSubmitted && (job.submissionStarted || job.networkText.length > 0 || job.networkDone)) {
        promptSubmitted = true;
      }
      if (!job.cancelled) {
        emitToIsolated({
          type: "request_error",
          request_id: requestId,
          submitted: promptSubmitted,
          tab_reset: chatWasReset,
          error: err && err.message ? err.message : String(err)
        });
        stopJob(job);
      }
    } finally {
      if (!promptSubmitted && (job.submissionStarted || job.networkText.length > 0 || job.networkDone)) {
        promptSubmitted = true;
      }
      if (!promptSubmitted) {
        if (!hadPreexistingAttachments && (attachments || []).length > 0) {
          for (const ctrl of composerAttachmentControls()) {
            try {
              ctrl.click();
            } catch (_) {}
          }
        }
        if (filledInputInThisJob && composerEl) {
          try {
            await fillInputWithPrompt(composerEl, "");
          } catch (_) {}
        }
      }
      job.completed = true;
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
      const attKey = payload.att_key || payload.attachment_id || "";
      if (!attKey) return;
      let entry = mainAttachmentReassembly.get(attKey);
      if (!entry) {
        entry = { parts: [], total: payload.total_chunks || 0 };
        mainAttachmentReassembly.set(attKey, entry);
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

  logDriver("info", `MAIN 原始响应驱动 v${DRIVER_VERSION} 已就绪`);
  emitToIsolated({
    type: "driver_ready",
    captured_rpc: Boolean(sharedState.lastCapturedRpc),
    active_model: readDetectedWebModel() || sharedState.lastSelectedModel,
    driver_version: DRIVER_VERSION
  });
})();
