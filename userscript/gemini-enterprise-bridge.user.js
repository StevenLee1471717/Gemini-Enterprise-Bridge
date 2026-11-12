// ==UserScript==
// @name         Gemini Enterprise Web-to-API Bridge (CSP-Immune Edition)
// @namespace    https://github.com/local/gemini-enterprise-bridge
// @version      1.0.0
// @description  将已登录的 Google Cloud Vertex AI Search / Agentspace 企业网页桥接至本地 OpenAI API 网关（采用 GM_xmlhttpRequest 绕过页面严格 CSP）
// @match        https://vertexaisearch.cloud.google.com/*
// @run-at       document-start
// @grant        GM_xmlhttpRequest
// @grant        GM_getValue
// @grant        GM_setValue
// @grant        unsafeWindow
// @connect      127.0.0.1
// @connect      localhost
// ==/UserScript==

(function () {
  "use strict";

  const pageWin = typeof unsafeWindow !== "undefined" ? unsafeWindow : window;
  if (pageWin.__GEB_MAIN_DRIVER_ACTIVE__ || pageWin.__GEB_USERSCRIPT_ACTIVE__) {
    // Avoid colliding if the Chrome MV3 extension is already active on this page
    return;
  }
  pageWin.__GEB_USERSCRIPT_ACTIVE__ = true;

  const SERVER_BASE = GM_getValue("geb_server_base", "http://127.0.0.1:8000");
  // Use per-tab `sessionStorage` so each open Gemini Enterprise tab acts as an independent parallel worker!
  let clientId = "";
  try {
    clientId = pageWin.sessionStorage.getItem("geb_tab_client_id") || "";
    if (!clientId) {
      clientId = "tm-" + Math.random().toString(36).slice(2, 10);
      pageWin.sessionStorage.setItem("geb_tab_client_id", clientId);
    }
  } catch (_) {
    clientId = "tm-" + Math.random().toString(36).slice(2, 10);
  }

  let activeJob = null;
  let badgeEl = null;

  function setStatusBadge(text, ok) {
    if (!document.body) return;
    if (!badgeEl) {
      badgeEl = document.createElement("div");
      badgeEl.style.cssText = [
        "position:fixed",
        "right:14px",
        "bottom:14px",
        "z-index:2147483647",
        "padding:5px 10px",
        "border-radius:999px",
        "font:600 12px/1.3 system-ui,-apple-system,sans-serif",
        "background:#0f172a",
        "color:#e2e8f0",
        "border:1px solid #334155",
        "box-shadow:0 4px 14px rgba(0,0,0,0.3)",
        "pointer-events:none",
        "opacity:0.88"
      ].join(";");
      document.body.appendChild(badgeEl);
    }
    badgeEl.textContent = (ok ? "🟢 " : "🟠 ") + text;
  }

  function gmRequest(method, path, bodyObj = null, timeout = 25000) {
    return new Promise((resolve, reject) => {
      GM_xmlhttpRequest({
        method,
        url: SERVER_BASE + path,
        headers: bodyObj ? { "Content-Type": "application/json" } : {},
        data: bodyObj ? JSON.stringify(bodyObj) : undefined,
        timeout,
        onload(resp) {
          if (resp.status >= 200 && resp.status < 300) {
            try {
              resolve(JSON.parse(resp.responseText || "{}"));
            } catch (err) {
              reject(err);
            }
          } else {
            reject(new Error("HTTP " + resp.status));
          }
        },
        onerror() {
          reject(new Error("network_error"));
        },
        ontimeout() {
          reject(new Error("timeout"));
        }
      });
    });
  }

  async function sendBridgeEvent(event, extra = {}) {
    try {
      await gmRequest("POST", "/api/bridge/event", {
        client_id: clientId,
        event,
        tab_url: pageWin.location.href,
        tab_title: document.title,
        ready: true,
        ...extra
      });
    } catch (_) {}
  }

  // Passive StreamAssist fetch sniffer inside pageWin
  const origFetch = pageWin.fetch.bind(pageWin);
  pageWin.fetch = async function (...args) {
    const req = args[0];
    const init = args[1] || {};
    const url = typeof req === "string" ? req : (req && req.url) || "";
    const method = (init && init.method) || (req && req.method) || "GET";
    const resp = await origFetch(...args);

    if (
      activeJob &&
      !activeJob.cancelled &&
      String(method).toUpperCase() === "POST" &&
      /streamAssist|widgetStreamAssist|:converse|:assist/i.test(url) &&
      resp &&
      resp.body
    ) {
      try {
        const cloned = resp.clone();
        if (cloned.body) {
          void parseStreamBody(cloned.body, activeJob);
        }
      } catch (_) {}
    }
    return resp;
  };

  function collectStructuredTexts(node, out) {
    if (!node || typeof node !== "object") return;
    if (Array.isArray(node)) {
      node.forEach((item) => collectStructuredTexts(item, out));
      return;
    }
    if (node.groundedContent && node.groundedContent.content) {
      const c = node.groundedContent.content;
      if (!c.thought && typeof c.text === "string" && c.text) {
        out.push(c.text);
      }
      return;
    }
    for (const k of Object.keys(node)) {
      if (typeof node[k] === "object") collectStructuredTexts(node[k], out);
    }
  }

  async function parseStreamBody(stream, job) {
    const reader = stream.getReader();
    const decoder = new TextDecoder("utf-8");
    let buf = "";
    let idx = 0;
    let inStr = false;
    let esc = false;
    let depth = 0;
    let start = -1;
    try {
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        while (idx < buf.length) {
          const ch = buf[idx];
          if (inStr) {
            if (esc) esc = false;
            else if (ch === "\\") esc = true;
            else if (ch === '"') inStr = false;
          } else if (ch === '"') {
            inStr = true;
          } else if (ch === "{") {
            if (depth === 0) start = idx;
            depth += 1;
          } else if (ch === "}") {
            if (depth > 0) {
              depth -= 1;
              if (depth === 0 && start >= 0) {
                const raw = buf.slice(start, idx + 1);
                start = -1;
                try {
                  const obj = JSON.parse(raw);
                  const parts = [];
                  collectStructuredTexts(obj, parts);
                  const txt = parts.join("");
                  if (txt) {
                    job.networkText += txt;
                    void sendBridgeEvent("stream_chunk", {
                      request_id: job.requestId,
                      delta: txt
                    });
                  }
                } catch (_) {}
              }
            }
          }
          idx += 1;
        }
      }
    } catch (_) {
    } finally {
      if (job.networkText) {
        job.networkDone = true;
      }
    }
  }

  function queryDeep(selector, root = document) {
    const out = [];
    const queue = [root];
    while (queue.length) {
      const cur = queue.shift();
      if (!cur || !cur.querySelectorAll) continue;
      try {
        cur.querySelectorAll(selector).forEach((el) => out.push(el));
      } catch (_) {}
      cur.querySelectorAll("*").forEach((el) => {
        if (el.shadowRoot) queue.push(el.shadowRoot);
      });
    }
    return out;
  }

  function visible(el) {
    if (!el || !el.getBoundingClientRect) return false;
    const r = el.getBoundingClientRect();
    const s = window.getComputedStyle(el);
    return r.width > 8 && r.height > 8 && s.display !== "none" && s.visibility !== "hidden";
  }

  function containsDeep(ancestor, descendant) {
    if (!ancestor || !descendant || ancestor === descendant) return false;
    let cur = descendant;
    let guard = 0;
    while (cur && guard < 40) {
      guard += 1;
      if (cur === ancestor) return true;
      if (cur.parentNode) cur = cur.parentNode;
      else if (cur.host) cur = cur.host;
      else {
        const r = cur.getRootNode ? cur.getRootNode() : null;
        cur = r && r !== cur ? r.host || null : null;
      }
    }
    return false;
  }

  function queryLeafReplies(replySel) {
    const raw = queryDeep(replySel).filter(visible);
    return raw.filter((n) => !raw.some((o) => o !== n && containsDeep(n, o)));
  }

  function sleep(ms) {
    return new Promise((r) => setTimeout(r, ms));
  }

  let activeWebModel = "";
  const LIGATURES_RE =
    /\b(arrow_drop_down|arrow_drop_up|expand_more|expand_less|keyboard_arrow_down|unfold_more|stat_minus_1|check|done|auto_awesome|spark)\b|[▾▼⌄]/gi;

  function cleanModelStr(s) {
    return String(s || "").replace(LIGATURES_RE, " ").replace(/\s+/g, " ").trim();
  }

  function matchesModel(rawText, targetModel) {
    const cleaned = cleanModelStr(rawText);
    if (!cleaned || !targetModel) return false;
    const headline = cleanModelStr(String(rawText).split("\n")[0]) || cleaned;
    const k = String(targetModel).trim().toLowerCase();
    if (k === "auto") return /^(?:gemini\s+)?auto\b|^自动/i.test(headline);
    if (k === "3.1-pro") return /3\.1\s*[-]?\s*pro\b|3\.1pro\b/i.test(headline) || /3\.1\s*[-]?\s*pro\b/i.test(cleaned);
    if (k === "4-argon") return !/\bcarbon\b|4carbon\b/i.test(headline) && (/4(?:\.0)?\s*[-]?\s*argon\b|4argon\b|\bargon\b/i.test(headline) || /4(?:\.0)?\s*[-]?\s*argon\b/i.test(cleaned));
    if (k === "4-carbon") return !/\bargon\b|4argon\b/i.test(headline) && (/4(?:\.0)?\s*[-]?\s*carbon\b|4carbon\b|\bcarbon\b/i.test(headline) || /4(?:\.0)?\s*[-]?\s*carbon\b/i.test(cleaned));
    if (k === "3.8-flash") return /3\.8\s*[-]?\s*flash\b|3\.8flash\b/i.test(headline) || /3\.8\s*[-]?\s*flash\b/i.test(cleaned);
    if (k === "2.5-pro") return /2\.5\s*[-]?\s*pro\b|2\.5pro\b/i.test(headline);
    if (k === "2.5-flash") return /2\.5\s*[-]?\s*flash\b|2\.5flash\b/i.test(headline);
    const spaced = k.replace(/[-_]+/g, " ");
    const compact = k.replace(/[\s\-_]+/g, "");
    return headline.toLowerCase().includes(spaced) || headline.toLowerCase().replace(/[\s\-_]+/g, "").includes(compact);
  }

  async function triggerNewChat() {
    const newChatRe = /\b(new chat|new conversation|new session|start a new|start new|reset chat)\b|新建对话|新对话|新会话|新建会话|开启新对话/i;
    const btns = queryDeep('button, a[href], md-icon-button, md-fab, ucs-icon-button, [role="button"]').filter(visible);
    for (const b of btns) {
      const sibTip = b.nextElementSibling && /tooltip/i.test(b.nextElementSibling.tagName || "") ? b.nextElementSibling.textContent : "";
      const lbl = [b.getAttribute("aria-label") || "", b.getAttribute("title") || "", sibTip || "", b.textContent || ""].join(" ");
      const rect = b.getBoundingClientRect();
      if (newChatRe.test(lbl) || (/\b(edit_square|add_comment|chat_add_on)\b/.test(lbl) && (rect.top < 140 || rect.left < 280))) {
        b.click();
        await sleep(600);
        return;
      }
    }
    if (/\/(?:r\/)?session\/[^/?#]+/i.test(pageWin.location.pathname)) {
      const cleanPath = pageWin.location.pathname.replace(/\/r\/session\/[^/?#]+.*$/i, "").replace(/\/session\/[^/?#]+.*$/i, "");
      const link = document.createElement("a");
      link.href = cleanPath + pageWin.location.search;
      document.body.appendChild(link);
      link.click();
      link.remove();
      await sleep(600);
    }
  }

  async function ensureWebModelSelected(targetModel) {
    if (!targetModel || !String(targetModel).trim()) return;
    const excludeRe = /\b(send|submit|attach|upload|mic|voice|stop|clear|copy|new chat|new session|history|settings|tools|sources)\b|发送|上传|语音|停止|清空|新建|新会话|历史|设置|工具|数据源/i;
    const modelTextRe = /\b(gemini|3\.1\s*[-]?\s*pro|3\.1pro|4(?:\.0)?\s*[-]?\s*argon|4argon|argon|4(?:\.0)?\s*[-]?\s*carbon|4carbon|carbon|3\.8\s*[-]?\s*flash|3\.8flash|2\.5\s*[-]?\s*pro|2\.5\s*[-]?\s*flash)\b|^(auto|自动|fast|pro|flash|thinking)$/i;

    let trigger = null;
    const hosts = queryDeep('ucs-model-selector, ucs-model-picker, model-selector, [data-test-id*="model" i], [data-testid*="model" i], [class*="model-selector" i]').filter(visible);
    for (const h of hosts) {
      if (h.tagName === "BUTTON" || h.getAttribute("role") === "button" || h.getAttribute("role") === "combobox") {
        trigger = h;
        break;
      }
      const inner = queryDeep('button, [role="button"], [role="combobox"], mat-select, md-assist-chip', h.shadowRoot || h).filter(visible);
      if (inner.length) {
        trigger = inner[0];
        break;
      }
    }

    if (!trigger) {
      const cands = queryDeep('button, [role="button"], [role="combobox"], [aria-haspopup], mat-select, md-assist-chip, md-filter-chip, ucs-chip').filter((el) => visible(el) && !el.disabled);
      for (const el of cands) {
        const txt = cleanModelStr(el.innerText || el.textContent || "");
        const aria = [el.getAttribute("aria-label") || "", el.getAttribute("title") || ""].join(" ");
        if (excludeRe.test(`${txt} ${aria}`)) continue;
        if (/\bmodel\b|模型/i.test(aria) || (txt.length >= 2 && txt.length <= 44 && modelTextRe.test(txt))) {
          trigger = el;
          break;
        }
      }
    }

    if (!trigger) return;
    const curLabel = `${trigger.innerText || trigger.textContent || ""}\n${trigger.getAttribute("aria-label") || ""}`;
    if (matchesModel(curLabel, targetModel)) {
      activeWebModel = cleanModelStr(trigger.innerText || trigger.textContent || "") || String(targetModel);
      return;
    }

    trigger.click();
    const optSel = 'mat-option, md-menu-item, md-select-option, md-list-item, mwc-list-item, ucs-model-option, ucs-menu-item, [role="menuitem"], [role="menuitemradio"], [role="option"], .mat-mdc-menu-item, .mat-mdc-option';
    for (let i = 0; i < 12; i += 1) {
      await sleep(95);
      const allOpts = queryDeep(optSel).filter((o) => visible(o) && o !== trigger && !containsDeep(o, trigger));
      const leafOpts = allOpts.filter((o) => allOpts.filter((c) => c !== o && containsDeep(o, c)).length < 2);
      const hits = leafOpts.filter((o) => matchesModel(`${o.innerText || o.textContent || ""}\n${o.getAttribute("aria-label") || ""}`, targetModel));
      if (hits.length) {
        hits.sort((a, b) => (a.innerText || "").length - (b.innerText || "").length);
        hits[0].click();
        await sleep(220);
        activeWebModel = cleanModelStr(trigger.innerText || trigger.textContent || "") || String(targetModel);
        if (visible(hits[0])) {
          try {
            document.body.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", keyCode: 27, bubbles: true, composed: true }));
          } catch (_) {}
        }
        return;
      }
    }
    try {
      document.body.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", keyCode: 27, bubbles: true, composed: true }));
    } catch (_) {}
  }

  async function executePrompt(cmd) {
    const requestId = cmd.request_id;
    const prompt = cmd.prompt || "";
    const job = { requestId, cancelled: false, networkText: "", networkDone: false };
    activeJob = job;

    try {
      if (cmd.reset_chat) {
        await triggerNewChat();
      }
      if (cmd.target_model) {
        await ensureWebModelSelected(cmd.target_model);
      }
      const inputs = queryDeep(
        '.ProseMirror[contenteditable="true"], [contenteditable="true"][role="textbox"], [contenteditable="true"], textarea'
      ).filter(visible);
      if (!inputs.length) {
        throw new Error("未找到 Gemini Enterprise 输入框，请确认页面已登录并进入聊天页。");
      }
      const inputEl = inputs[0];
      const replySel =
        'ucs-summary, ucs-markdown, message-content, [data-message-author-role="model"], .markdown-body';
      const beforeNodes = queryLeafReplies(replySel);
      const beforeCount = beforeNodes.length;

      inputEl.focus();
      if (inputEl.tagName === "TEXTAREA") {
        Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value").set.call(
          inputEl,
          prompt
        );
        inputEl.dispatchEvent(new InputEvent("input", { bubbles: true, composed: true }));
      } else {
        const isHidden = Boolean(document.hidden || document.visibilityState === "hidden");
        let inserted = false;
        if (!isHidden) {
          const sel = window.getSelection();
          const range = document.createRange();
          range.selectNodeContents(inputEl);
          sel.removeAllRanges();
          sel.addRange(range);
          inserted = document.execCommand("insertText", false, prompt);
        }
        if (!inserted || !(inputEl.innerText || inputEl.textContent || "").trim()) {
          // Trusted-Types-safe DOM injection (NEVER assign .innerHTML!)
          if (typeof inputEl.replaceChildren === "function") {
            inputEl.replaceChildren();
          } else {
            while (inputEl.firstChild) inputEl.removeChild(inputEl.firstChild);
          }
          const frag = document.createDocumentFragment();
          const lines = prompt.split("\n");
          const step = lines.length > 160 ? 64 : 1;
          for (let idx = 0; idx < lines.length; idx += step) {
            const p = document.createElement("p");
            const txt = step === 1 ? lines[idx] : lines.slice(idx, idx + step).join("\n");
            if (txt) p.textContent = txt;
            else p.appendChild(document.createElement("br"));
            frag.appendChild(p);
          }
          inputEl.appendChild(frag);
          inputEl.dispatchEvent(new InputEvent("beforeinput", { bubbles: true, composed: true, inputType: "insertText", data: prompt }));
          inputEl.dispatchEvent(new InputEvent("input", { bubbles: true, composed: true, inputType: "insertText", data: prompt }));
        }
      }

      await sleep(100);
      const sendBtns = queryDeep('button, md-icon-button, [role="button"]').filter((b) => {
        if (!visible(b) || b.disabled) return false;
        const lbl = (b.getAttribute("aria-label") || b.textContent || "").toLowerCase();
        return /\b(send|submit|arrow_upward)\b|发送|提交/.test(lbl);
      });
      if (sendBtns.length) {
        sendBtns[sendBtns.length - 1].click();
      } else {
        inputEl.dispatchEvent(
          new KeyboardEvent("keydown", {
            key: "Enter",
            code: "Enter",
            keyCode: 13,
            bubbles: true,
            composed: true
          })
        );
      }

      let lastDom = "";
      let emitted = 0;
      let stableSince = Date.now();
      const deadline = Date.now() + (cmd.timeout_ms || 180000);

      while (!job.cancelled && Date.now() < deadline) {
        await sleep(250);
        if (job.networkDone && job.networkText.trim()) {
          await sendBridgeEvent("request_complete", {
            request_id: requestId,
            reply: job.networkText,
            source: "userscript_network",
            active_model: activeWebModel
          });
          return;
        }
        const curNodes = queryLeafReplies(replySel);
        if (curNodes.length > beforeCount) {
          const txt = (curNodes[curNodes.length - 1].innerText || "").trim();
          if (txt && txt !== lastDom) {
            if (!job.networkText && txt.length > emitted && txt.startsWith(lastDom)) {
              await sendBridgeEvent("stream_chunk", {
                request_id: requestId,
                delta: txt.slice(emitted)
              });
              emitted = txt.length;
            }
            lastDom = txt;
            stableSince = Date.now();
          } else if (txt && Date.now() - stableSince > 1600) {
            await sendBridgeEvent("request_complete", {
              request_id: requestId,
              reply: job.networkText || txt,
              source: "userscript_dom",
              active_model: activeWebModel
            });
            return;
          }
        }
      }
      throw new Error("等待网页回复超时");
    } catch (err) {
      await sendBridgeEvent("request_error", {
        request_id: requestId,
        error: err.message || String(err)
      });
    } finally {
      if (activeJob === job) activeJob = null;
    }
  }

  async function pollLoop() {
    while (true) {
      try {
        await gmRequest("POST", "/api/bridge/register", {
          client_id: clientId,
          user_agent: navigator.userAgent,
          tab_url: pageWin.location.href,
          tab_title: document.title,
          driver_mode: "userscript_auto"
        });
        setStatusBadge("Bridge 已挂载", true);

        while (true) {
          const res = await gmRequest(
            "GET",
            `/api/bridge/poll?client_id=${encodeURIComponent(clientId)}&timeout=18`,
            null,
            26000
          );
          if (res && res.command) {
            if (res.command.type === "execute_prompt") {
              void executePrompt(res.command);
            } else if (res.command.type === "cancel_request" && activeJob) {
              activeJob.cancelled = true;
            }
          }
        }
      } catch (_) {
        setStatusBadge("等待本地 8000 网关...", false);
        await sleep(3000);
      }
    }
  }

  if (document.readyState === "loading") {
    window.addEventListener("DOMContentLoaded", () => {
      void pollLoop();
    });
  } else {
    void pollLoop();
  }
})();
