// Zero-dependency Node.js Shadow-DOM simulation harness for page-driver.js
// Verifies:
//  1) Switching model from default "Auto\narrow_drop_down" pill -> "4 Argon" -> "3.1 Pro" -> "3.8 Flash" -> "Auto"
//     without false-matching "pro" inside "Automatically selects the best model for your prompt"
//     and cleanly dismissing lingering .cdk-overlay-backdrop-showing overlays!
//  2) Locating & clicking New Chat (`md-icon-button` + sibling `<md-tooltip>New chat</md-tooltip>`) while
//     ignoring the composer's `+` (attach file) button, and verifying `/r/session/...` clears to `/u/3/home/cid/...`!
//  3) Hot-swapping `page-driver.js` twice without double-wrapping `window.fetch`!

import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const driverSource = fs.readFileSync(path.join(__dirname, "page-driver.js"), "utf8");

// Minimal multi-root Shadow DOM simulation matching Lit / Material Web / Angular CDK behavior
class MockEvent {
  constructor(type, init = {}) {
    this.type = type;
    this.bubbles = Boolean(init.bubbles);
    this.composed = Boolean(init.composed);
    this.key = init.key || "";
  }
}

function matchesSimpleSelector(el, sel) {
  const s = sel.trim();
  if (!s) return false;
  if (s === "*") return true;

  // Class selector `.foo`
  if (s.startsWith(".")) {
    const cls = s.slice(1);
    return (el.className || "").split(/\s+/).includes(cls);
  }
  // Attribute selector `[attr...]`
  if (s.startsWith("[") && s.endsWith("]")) {
    const inner = s.slice(1, -1).trim();
    const caseInsensitive = /\s+i$/i.test(inner);
    const body = caseInsensitive ? inner.replace(/\s+i$/i, "").trim() : inner;
    if (!body.includes("=")) {
      return el.hasAttribute(body);
    }
    const m = body.match(/^([a-zA-Z0-9_-]+)(\*=|\^=|=)"?([^"]*)"?$/);
    if (!m) return false;
    const [, attrName, op, rawExpected] = m;
    const actualRaw = el.getAttribute(attrName);
    if (actualRaw === null) return false;
    const actual = caseInsensitive ? actualRaw.toLowerCase() : actualRaw;
    const expected = caseInsensitive ? rawExpected.toLowerCase() : rawExpected;
    if (op === "=") return actual === expected;
    if (op === "*=") return actual.includes(expected);
    if (op === "^=") return actual.startsWith(expected);
    return false;
  }
  // Tag + optional attribute e.g. `a[href]` or `.ProseMirror[contenteditable="true"]`
  if (s.includes("[")) {
    const bracketIdx = s.indexOf("[");
    const prefix = s.slice(0, bracketIdx);
    const attrPart = s.slice(bracketIdx);
    return matchesSimpleSelector(el, prefix) && matchesSimpleSelector(el, attrPart);
  }
  return el.tagName.toLowerCase() === s.toLowerCase();
}

function matchesSelectorList(el, selectorList) {
  const parts = selectorList.split(",");
  return parts.some((p) => matchesSimpleSelector(el, p));
}

class MockNode {
  constructor() {
    this.children = [];
    this.parentNode = null;
    this.parentElement = null;
    this._listeners = new Map();
  }

  appendChild(child) {
    if (child && child.__isFragment) {
      for (const grandChild of child.children.slice()) {
        this.appendChild(grandChild);
      }
      child.children = [];
      return child;
    }
    child.parentNode = this;
    child.parentElement = this instanceof MockElement ? this : null;
    this.children.push(child);
    return child;
  }

  removeChild(child) {
    const idx = this.children.indexOf(child);
    if (idx !== -1) {
      this.children.splice(idx, 1);
      child.parentNode = null;
      child.parentElement = null;
    }
    return child;
  }

  replaceChildren(...nodes) {
    for (const ch of this.children) {
      ch.parentNode = null;
      ch.parentElement = null;
    }
    this.children = [];
    for (const n of nodes) {
      this.appendChild(n);
    }
  }

  get firstChild() {
    return this.children[0] || null;
  }

  remove() {
    if (!this.parentNode) return;
    const idx = this.parentNode.children.indexOf(this);
    if (idx !== -1) this.parentNode.children.splice(idx, 1);
    this.parentNode = null;
    this.parentElement = null;
  }

  getRootNode() {
    let cur = this;
    while (cur.parentNode) cur = cur.parentNode;
    return cur;
  }

  contains(other) {
    let cur = other;
    while (cur) {
      if (cur === this) return true;
      cur = cur.parentNode;
    }
    return false;
  }

  querySelectorAll(selector) {
    const out = [];
    const walk = (node) => {
      for (const ch of node.children) {
        if (ch instanceof MockElement && matchesSelectorList(ch, selector)) {
          out.push(ch);
        }
        walk(ch);
      }
    };
    walk(this);
    return out;
  }

  querySelector(selector) {
    return this.querySelectorAll(selector)[0] || null;
  }

  addEventListener(type, fn) {
    if (!this._listeners.has(type)) this._listeners.set(type, []);
    this._listeners.get(type).push(fn);
  }

  removeEventListener(type, fn) {
    const arr = this._listeners.get(type) || [];
    const idx = arr.indexOf(fn);
    if (idx !== -1) arr.splice(idx, 1);
  }

  dispatchEvent(ev) {
    const arr = (this._listeners.get(ev.type) || []).slice();
    for (const fn of arr) fn.call(this, ev);
    if (ev.bubbles && this.parentNode) {
      this.parentNode.dispatchEvent(ev);
    } else if (ev.bubbles && ev.composed && this.host) {
      this.host.dispatchEvent(ev);
    }
    return true;
  }
}

class MockShadowRoot extends MockNode {
  constructor(host) {
    super();
    this.host = host;
  }
  getElementById(id) {
    return this.querySelectorAll("*").find((e) => e.id === id) || null;
  }
}

class MockElement extends MockNode {
  constructor(tagName, opts = {}) {
    super();
    this.tagName = tagName.toUpperCase();
    this.id = opts.id || "";
    this.className = opts.className || "";
    this.disabled = false;
    this.readOnly = false;
    this._attrs = new Map(Object.entries(opts.attrs || {}));
    this._text = opts.text || "";
    this._rect = opts.rect || { left: 100, top: 100, right: 220, bottom: 136, width: 120, height: 36 };
    this.style = { display: opts.hidden ? "none" : "block", visibility: "visible", opacity: "1" };
    this.shadowRoot = null;
    this.classList = {
      contains: (c) => (this.className || "").split(/\s+/).includes(c)
    };
  }

  attachShadow() {
    this.shadowRoot = new MockShadowRoot(this);
    return this.shadowRoot;
  }

  cloneNode() {
    return new MockElement(this.tagName.toLowerCase());
  }

  setAttribute(k, v) {
    this._attrs.set(k, String(v));
  }
  getAttribute(k) {
    if (k === "href" && this.href) return this.href;
    return this._attrs.has(k) ? this._attrs.get(k) : null;
  }
  hasAttribute(k) {
    return this._attrs.has(k);
  }

  get nextElementSibling() {
    if (!this.parentNode) return null;
    const sibs = this.parentNode.children;
    const i = sibs.indexOf(this);
    return i >= 0 && i + 1 < sibs.length ? sibs[i + 1] : null;
  }

  get innerHTML() {
    return "";
  }
  set innerHTML(_val) {
    // Enforce Google Cloud's strict Trusted Types CSP (`require-trusted-types-for 'script'`)!
    throw new TypeError(
      "Failed to set the 'innerHTML' property on 'Element': This document requires 'TrustedHTML' assignment."
    );
  }

  get innerText() {
    const childText = this.children.map((c) => c.innerText || "").filter(Boolean).join("\n");
    return [this._text, childText].filter(Boolean).join("\n");
  }
  set innerText(v) {
    this._text = v;
  }

  get textContent() {
    const childText = this.children.map((c) => c.textContent || "").filter(Boolean).join(" ");
    return [this._text, childText].filter(Boolean).join(" ");
  }
  set textContent(v) {
    this._text = v;
  }

  getBoundingClientRect() {
    if (this.style.display === "none") {
      return { left: 0, top: 0, right: 0, bottom: 0, width: 0, height: 0 };
    }
    return this._rect;
  }

  focus() {}
  click() {
    this.dispatchEvent(new MockEvent("click", { bubbles: true, composed: true }));
  }
}

// Construct realistic Gemini Enterprise Shadow DOM tree:
// URL starts inside an active session `/u/3/home/cid/00000000-0000-0000-0000-000000000000/r/session/1000000000000000001?hl=en_US`
const doc = new MockNode();
doc.title = "Gemini Enterprise";
doc.body = new MockElement("body", { rect: { left: 0, top: 0, right: 1440, bottom: 900, width: 1440, height: 900 } });
doc.appendChild(doc.body);
doc.createElement = (tag) => new MockElement(tag);
doc.createDocumentFragment = () => {
  const frag = new MockNode();
  frag.__isFragment = true;
  return frag;
};
doc.getElementById = (id) => doc.querySelectorAll("*").find((e) => e.id === id) || null;

const app = new MockElement("ucs-standalone-app");
doc.body.appendChild(app);
const appShadow = app.attachShadow();

// 1) Left navigation panel with `<md-icon-button><md-icon>edit_square</md-icon></md-icon-button><md-tooltip>New chat</md-tooltip>`
const navPanel = new MockElement("ucs-nav-panel", {
  rect: { left: 0, top: 0, right: 72, bottom: 900, width: 72, height: 900 }
});
appShadow.appendChild(navPanel);
const navShadow = navPanel.attachShadow();

const newChatBtn = new MockElement("md-icon-button", {
  id: "nav-new-chat",
  rect: { left: 16, top: 64, right: 56, bottom: 104, width: 40, height: 40 }
});
newChatBtn.appendChild(new MockElement("md-icon", { text: "edit_square" }));
const newChatTooltip = new MockElement("md-tooltip", {
  text: "New chat",
  hidden: true,
  attrs: { anchor: "nav-new-chat" }
});
navShadow.appendChild(newChatBtn);
navShadow.appendChild(newChatTooltip);

// 2) Chat area with 2 existing assistant reply bubbles in current session
const ucsChat = new MockElement("ucs-chat");
appShadow.appendChild(ucsChat);
const chatShadow = ucsChat.attachShadow();

let replyBubble1 = new MockElement("ucs-markdown", { text: "Previous turn answer 1" });
let replyBubble2 = new MockElement("ucs-markdown", { text: "Previous turn answer 2" });
chatShadow.appendChild(replyBubble1);
chatShadow.appendChild(replyBubble2);

// 3) Composer (`ucs-search-box`) with:
//    - `+` attach button (MUST NOT be mistaken for New Chat!)
//    - `.ProseMirror` contenteditable input
//    - Model selector pill defaulting to `"Auto\narrow_drop_down"` (NO `aria-label="model"`!)
//    - Send button
const searchBox = new MockElement("ucs-search-box", {
  rect: { left: 300, top: 720, right: 1100, bottom: 840, width: 800, height: 120 }
});
chatShadow.appendChild(searchBox);
const searchShadow = searchBox.attachShadow();

let composerAttachClicked = 0;
const composerPlusBtn = new MockElement("md-icon-button", {
  text: "add",
  attrs: { "aria-label": "Add files or sources" },
  rect: { left: 316, top: 790, right: 352, bottom: 826, width: 36, height: 36 }
});
composerPlusBtn.addEventListener("click", () => {
  composerAttachClicked += 1;
});
searchShadow.appendChild(composerPlusBtn);

const pmHost = new MockElement("ucs-prosemirror-editor");
searchShadow.appendChild(pmHost);
const pmShadow = pmHost.attachShadow();
const proseMirror = new MockElement("div", {
  className: "ProseMirror",
  attrs: { contenteditable: "true", role: "textbox" },
  rect: { left: 320, top: 730, right: 1060, bottom: 780, width: 740, height: 50 }
});
pmShadow.appendChild(proseMirror);

// Default model pill showing "Auto" + Material icon ligature "arrow_drop_down"
const modelPillBtn = new MockElement("button", {
  text: "Auto",
  rect: { left: 920, top: 790, right: 1010, bottom: 824, width: 90, height: 34 }
});
const chevronIcon = new MockElement("md-icon", { text: "arrow_drop_down" });
modelPillBtn.appendChild(chevronIcon);
searchShadow.appendChild(modelPillBtn);

const sendBtn = new MockElement("md-icon-button", {
  text: "arrow_upward",
  attrs: { "aria-label": "Send" },
  rect: { left: 1020, top: 790, right: 1056, bottom: 826, width: 36, height: 36 }
});
searchShadow.appendChild(sendBtn);

// 4) CDK / Material Menu overlay (initially hidden; opens when modelPillBtn is clicked)
const overlayBackdrop = new MockElement("div", {
  className: "cdk-overlay-backdrop cdk-overlay-backdrop-showing",
  hidden: true,
  rect: { left: 0, top: 0, right: 1440, bottom: 900, width: 1440, height: 900 }
});
const menuContainer = new MockElement("div", {
  attrs: { role: "menu" },
  hidden: true,
  rect: { left: 860, top: 560, right: 1120, bottom: 780, width: 260, height: 220 }
});
doc.body.appendChild(overlayBackdrop);
doc.body.appendChild(menuContainer);

const menuDefs = [
  { label: "Auto", desc: "Automatically selects the best model for your prompt" }, // Note "prompt" contains "pro"!
  { label: "3.1 Pro", desc: "Complex reasoning & analysis" },
  { label: "4 Argon", desc: "Frontier multimodal & agentic tasks" },
  { label: "4 Carbon", desc: "Next-gen enterprise reasoning & coding" },
  { label: "3.8 Flash", desc: "Fast everyday assistance" }
];

for (const def of menuDefs) {
  const item = new MockElement("md-menu-item", {
    text: `${def.label}\n${def.desc}`,
    attrs: { role: "menuitemradio" },
    rect: { left: 868, top: 570, right: 1112, bottom: 618, width: 244, height: 48 }
  });
  item.addEventListener("click", () => {
    modelPillBtn._text = def.label;
    // Simulate transient sticky overlay on "3.1 Pro" to test backdrop dismissal!
    if (def.label !== "3.1 Pro") {
      menuContainer.style.display = "none";
      overlayBackdrop.style.display = "none";
    }
  });
  menuContainer.appendChild(item);
}

overlayBackdrop.addEventListener("click", () => {
  menuContainer.style.display = "none";
  overlayBackdrop.style.display = "none";
});

modelPillBtn.addEventListener("click", () => {
  const isOpen = menuContainer.style.display !== "none";
  menuContainer.style.display = isOpen ? "none" : "block";
  overlayBackdrop.style.display = isOpen ? "none" : "block";
});

// Wire New Chat button behavior: clears `/r/session/...` from URL, clears old reply bubbles, and resets model pill to "Auto"!
const mockLocation = {
  href: "https://vertexaisearch.cloud.google.com/u/3/home/cid/00000000-0000-0000-0000-000000000000/r/session/1000000000000000001?hl=en_US",
  origin: "https://vertexaisearch.cloud.google.com",
  pathname: "/u/3/home/cid/00000000-0000-0000-0000-000000000000/r/session/1000000000000000001",
  search: "?hl=en_US"
};

let newChatClicks = 0;
newChatBtn.addEventListener("click", () => {
  newChatClicks += 1;
  mockLocation.pathname = "/u/3/home/cid/00000000-0000-0000-0000-000000000000";
  mockLocation.href = `${mockLocation.origin}${mockLocation.pathname}${mockLocation.search}`;
  replyBubble1.remove();
  replyBubble2.remove();
  modelPillBtn._text = "Auto";
});

// Execute page-driver.js inside VM sandbox
let rawFetchCalls = 0;
const baseFetch = async (url) => {
  rawFetchCalls += 1;
  if (String(url).startsWith("data:")) return fetch(url);
  return { ok: true, body: null };
};

const sandbox = {
  window: null,
  globalThis: null,
  document: doc,
  location: mockLocation,
  CSS: { escape: (s) => s },
  URL,
  Headers,
  TextDecoder,
  AbortController,
  File,
  DataTransfer: class {
    constructor() { this.files = []; this.items = { add: (file) => this.files.push(file) }; }
  },
  MessageChannel,
  Event: MockEvent,
  MouseEvent: MockEvent,
  PointerEvent: MockEvent,
  KeyboardEvent: MockEvent,
  InputEvent: MockEvent,
  PopStateEvent: MockEvent,
  HTMLTextAreaElement: function () {},
  HTMLInputElement: function () {},
  XMLHttpRequest: function () {},
  setTimeout,
  clearTimeout,
  requestAnimationFrame: (cb) => setTimeout(cb, 0),
  console
};
sandbox.XMLHttpRequest.prototype = { open() {}, send() {} };
sandbox.window = sandbox;
sandbox.globalThis = sandbox;
sandbox.window.location = mockLocation;
sandbox.window.innerHeight = 900;
sandbox.window.fetch = baseFetch;
sandbox.window.getComputedStyle = (el) => el.style;
sandbox.window.postMessage = () => {};
sandbox.window.addEventListener = (t, fn) => doc.addEventListener(t, fn);
sandbox.window.removeEventListener = (t, fn) => doc.removeEventListener(t, fn);

vm.createContext(sandbox);

// Run page-driver.js TWICE to verify hot-swap safety (must NOT double-wrap fetch!)
vm.runInContext(driverSource, sandbox);
vm.runInContext(driverSource, sandbox);

await sandbox.window.fetch("https://example.com/ping");
assert.equal(rawFetchCalls, 1, "Hot-reloading page-driver.js must not double-wrap window.fetch");

const api = sandbox.__GEB_MAIN_API__;
assert.ok(api, "__GEB_MAIN_API__ must be exposed");

// DOM is diagnostic input only; it must not manufacture executable tool frames.
const typedReply = new MockElement('ucs-markdown');
const typedPre = new MockElement('pre');
const typedCode = new MockElement('code', {className:'language-codex_tool_call',text:JSON.stringify({name:'exec_command',arguments:{cmd:'printf "```` quoted"'}})});
typedPre.appendChild(typedCode);typedReply.appendChild(typedPre);chatShadow.appendChild(typedReply);
const typedText = api.collectAssistantReplyTexts().find(t=>t.includes('quoted'));
assert.ok(typedText.includes('printf'), 'DOM diagnostics retain visible content');
assert.ok(!typedText.includes('codex_tool_call'), 'DOM diagnostics never manufacture a tool frame');
assert.equal(typedText,typedCode.textContent,'DOM diagnostics must not add synthetic fence text');
typedReply.remove();
console.log('PASS: DOM diagnostics do not manufacture client tool boundaries');

// Actual Enterprise response markup lives below two Shadow DOM hosts.
const realReply = new MockElement('ucs-response-markdown');
const realReplyShadow = realReply.attachShadow();
const fastMarkdown = new MockElement('ucs-fast-markdown');
const fastShadow = fastMarkdown.attachShadow();
const markdownDocument = new MockElement('div',{className:'markdown-document'});
const codeBlock = new MockElement('ucs-code-block',{attrs:{language:'codex_tool_call'}});
const codeBlockPre = new MockElement('pre');
codeBlockPre.appendChild(new MockElement('code',{className:'language-codex_tool_call',text:JSON.stringify({name:'exec_command',arguments:{cmd:'cat seed.txt'}})}));
codeBlock.appendChild(codeBlockPre);markdownDocument.appendChild(codeBlock);fastShadow.appendChild(markdownDocument);realReplyShadow.appendChild(fastMarkdown);chatShadow.appendChild(realReply);
assert.ok(api.collectAssistantReplyTexts().some(t=>t.includes('cat seed.txt')));
realReply.remove();
console.log('PASS: real Enterprise nested response markup preserves client calls');

// Test 1: Initial diagnosis detects "Auto" model pill & New Chat button & 2 existing reply bubbles
const diag0 = api.diagnoseCurrentPage();
assert.equal(diag0.modelSelectorFound, true, "Must detect default 'Auto' model selector pill");
assert.equal(diag0.detectedModelLabel, "Auto");
assert.equal(diag0.newChatButtonFound, true, "Must detect New Chat button via <md-tooltip>");
assert.equal(diag0.assistantBlocksCount, 2);
assert.equal(
  diag0.cleanNewChatUrl,
  "https://vertexaisearch.cloud.google.com/u/3/home/cid/00000000-0000-0000-0000-000000000000?hl=en_US"
);

// Test 2: Switch from default "Auto" -> "4-argon" (`4argon`)
const swArgon = await api.ensureWebModelSelected("4-argon");
assert.equal(swArgon.ok, true);
assert.equal(swArgon.switched, true);
assert.equal(api.readDetectedWebModel(), "4 Argon");

// Calling ensureWebModelSelected("4-argon") again is an instant no-op (`alreadySelected: true`)
const swArgonAgain = await api.ensureWebModelSelected("4-argon");
assert.equal(swArgonAgain.ok, true);
assert.equal(swArgonAgain.switched, false);
assert.equal(swArgonAgain.alreadySelected, true);

// Test 2b: Switch from "4 Argon" -> "4-carbon" (`4carbon`) — must NOT falsely treat "4 Argon" as already matching "4-carbon"!
const swCarbon = await api.ensureWebModelSelected("4-carbon");
assert.equal(swCarbon.ok, true);
assert.equal(swCarbon.switched, true);
assert.equal(api.readDetectedWebModel(), "4 Carbon");

// Calling ensureWebModelSelected("4-carbon") again is an instant no-op (`alreadySelected: true`)
const swCarbonAgain = await api.ensureWebModelSelected("4-carbon");
assert.equal(swCarbonAgain.ok, true);
assert.equal(swCarbonAgain.switched, false);
assert.equal(swCarbonAgain.alreadySelected, true);

// Switching back from "4 Carbon" -> "4-argon" must also switch cleanly without false match
const swBackToArgon = await api.ensureWebModelSelected("4-argon");
assert.equal(swBackToArgon.ok, true);
assert.equal(swBackToArgon.switched, true);
assert.equal(api.readDetectedWebModel(), "4 Argon");

// Test 3: Switch to "3.1-pro" — must NOT falsely match "prompt" in Auto's description, AND must dismiss sticky backdrop!
const sw31Pro = await api.ensureWebModelSelected("3.1-pro");
assert.equal(sw31Pro.ok, true);
assert.equal(sw31Pro.switched, true);
assert.equal(api.readDetectedWebModel(), "3.1 Pro");
assert.equal(overlayBackdrop.style.display, "none", "Transient CDK backdrop must be dismissed");

// Test 4: Switch to "3.8-flash"
const sw38Flash = await api.ensureWebModelSelected("3.8-flash");
assert.equal(sw38Flash.ok, true);
assert.equal(sw38Flash.switched, true);
assert.equal(api.readDetectedWebModel(), "3.8 Flash");

// Test 5: Trigger New Chat (`*-newchat`) -> clicks real New Chat button (NOT composer `+`!), clears replies & `/r/session/`, then re-selects `4 Argon`!
const resetRes = await api.triggerNewChatSession();
assert.equal(resetRes.ok, true);
assert.equal(resetRes.cleared, true);
assert.equal(newChatClicks, 1, "Must click the nav New Chat button");
assert.equal(composerAttachClicked, 0, "Must NEVER click composer '+' attach button");
assert.equal(api.collectAssistantReplyTexts().length, 0, "Old assistant replies must be cleared");
assert.ok(!mockLocation.pathname.includes("/r/session/"), "Session path must be cleared from URL");

// New Chat reset the UI model back to "Auto"; verify subsequent ensureWebModelSelected("4-argon") switches it cleanly!
assert.equal(api.readDetectedWebModel(), "Auto");
const swAfterReset = await api.ensureWebModelSelected("4-argon");
assert.equal(swAfterReset.ok, true);
assert.equal(swAfterReset.switched, true);
assert.equal(api.readDetectedWebModel(), "4 Argon");

// Test 6: SILENT BACKGROUND TAB EXECUTION (`document.hidden === true`)!
// Simulate Chrome freezing `requestAnimationFrame` (NEVER fires!) AND throttling `window.setTimeout` to 60,000ms,
// while `sw_sleep` IPC messages from Service Worker remain unthrottled!
doc.hidden = true;
doc.visibilityState = "hidden";
doc.hasFocus = () => false;

const realSetTimeout = setTimeout;
sandbox.requestAnimationFrame = () => {
  // Never resolves in hidden tabs!
  return 0;
};
sandbox.setTimeout = (fn, ms) => {
  // Simulate Chrome Intensive Wake Up Throttling (60s) for tab timers > 30ms when hidden!
  if (doc.hidden && Number(ms) > 30) {
    return realSetTimeout(fn, 60000);
  }
  return realSetTimeout(fn, ms);
};

const vmWindow = vm.runInContext("window", sandbox);
const emittedEvents = [];
const replyChunkStore = new Map();

sandbox.window.postMessage = (msg) => {
  if (!msg) return;
  if (msg.channel === "GEB_MAIN_TO_ISOLATED" && msg.payload) {
    if (msg.payload.type === "sw_sleep") {
      // Unthrottled MV3 Service Worker timer replies immediately after real short delay!
      realSetTimeout(() => {
        doc.dispatchEvent(
          Object.assign(new MockEvent("message"), {
            source: vmWindow,
            origin: mockLocation.origin,
            data: {
              channel: "GEB_ISOLATED_TO_MAIN",
              action: "sw_sleep_done",
              payload: { sleep_id: msg.payload.sleep_id }
            }
          })
        );
      }, 12);
      return;
    }
    if (msg.payload.type === "reply_chunk") {
      const rid = msg.payload.request_id;
      if (!replyChunkStore.has(rid)) replyChunkStore.set(rid, []);
      replyChunkStore.get(rid)[msg.payload.chunk_index] = msg.payload.data;
      return;
    }
    if (msg.payload.type === "request_complete" && msg.payload.chunked) {
      const parts = replyChunkStore.get(msg.payload.request_id) || [];
      emittedEvents.push({ ...msg.payload, reply: parts.join(""), chunked: false });
      return;
    }
    emittedEvents.push(msg.payload);
  }
};

async function finishNetworkReply(text) {
  const job = sandbox.__GEB_MAIN_STATE__.getActiveJob();
  job.submissionStarted = true;
  const stream = new ReadableStream({start(controller) {
    controller.enqueue(new TextEncoder().encode(JSON.stringify([{groundedContent:{content:{text}}}])));
    controller.close();
  }});
  await sandbox.__GEB_MAIN_STATE__.consumeStream(stream, job);
}

// Simulate both short background replies AND 140KB+ large tool-call replies without TrustedHTML errors!
let nextSimulatedReplyOverride = "";
sendBtn.addEventListener("click", () => {
  const submittedText = (proseMirror.innerText || proseMirror.textContent || "").trim();
  const replyBody = nextSimulatedReplyOverride || `Background reply to: ${submittedText}`;
  nextSimulatedReplyOverride = "";
  const replyNode = new MockElement("ucs-markdown", { text: replyBody });
  chatShadow.appendChild(replyNode);
  void finishNetworkReply(replyNode.innerText);
});

const bgStart = Date.now();
await api.executePromptJob({
  request_id: "req-silent-bg-1",
  prompt: "Silent background query from Cursor",
  target_model: "3.8-flash",
  reset_chat: false,
  timeout_ms: 5000
});
const bgElapsed = Date.now() - bgStart;

assert.ok(bgElapsed < 3500, `Silent background prompt must not hang on rAF or throttled setTimeout (took ${bgElapsed}ms)`);
assert.equal(api.readDetectedWebModel(), "3.8 Flash", "Must switch model to 3.8 Flash while tab is hidden");

const completeEv = emittedEvents.find(
  (e) => e.type === "request_complete" && e.request_id === "req-silent-bg-1"
);
assert.ok(completeEv, "Must emit request_complete while tab is in background");
assert.equal(completeEv.reply, "Background reply to: Silent background query from Cursor");

// Test 7: 135KB+ Large Prompt Chunk Reassembly + Trusted-Types DocumentFragment + 140KB+ Chunked Reply!
const largeLine = "const payloadRow = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ_0123456789_TRUSTED_TYPES_SAFE';";
const largePrompt = Array.from({ length: 1900 }, (_, i) => `// line ${i}\n${largeLine}`).join("\n");
assert.ok(largePrompt.length > 130000, "Large prompt must exceed 130KB");

const largeCodeArg = "X".repeat(138000);
const expectedLargeReply = `\`\`\`codex_tool_call\n{"name": "write_file", "arguments": {"path": "big.ts", "content": "${largeCodeArg}"}}\n\`\`\``;
nextSimulatedReplyOverride = expectedLargeReply;

const chunkLen = 32768;
const totalPromptChunks = Math.ceil(largePrompt.length / chunkLen);
for (let i = 0; i < totalPromptChunks; i += 1) {
  doc.dispatchEvent(
    Object.assign(new MockEvent("message"), {
      source: vmWindow,
      origin: mockLocation.origin,
      data: {
        channel: "GEB_ISOLATED_TO_MAIN",
        action: "prompt_chunk",
        payload: {
          request_id: "req-140kb",
          chunk_index: i,
          total_chunks: totalPromptChunks,
          data: largePrompt.slice(i * chunkLen, (i + 1) * chunkLen)
        }
      }
    })
  );
}

doc.dispatchEvent(
  Object.assign(new MockEvent("message"), {
    source: vmWindow,
    origin: mockLocation.origin,
    data: {
      channel: "GEB_ISOLATED_TO_MAIN",
      action: "execute_prompt",
      payload: {
        request_id: "req-140kb",
        prompt: "",
        chunked: true,
        total_chunks: totalPromptChunks,
        target_model: null,
        reset_chat: false,
        timeout_ms: 6000
      }
    }
  })
);

// Wait for async execute_prompt completion
for (let w = 0; w < 120; w += 1) {
  if (emittedEvents.some((e) => e.type === "request_complete" && e.request_id === "req-140kb")) {
    break;
  }
  await new Promise((r) => realSetTimeout(r, 25));
}

const largeCompleteEv = emittedEvents.find(
  (e) => e.type === "request_complete" && e.request_id === "req-140kb"
);
assert.ok(largeCompleteEv, "140KB+ chunked reply must complete cleanly");
assert.equal(largeCompleteEv.reply, expectedLargeReply, "138KB tool argument must arrive with 0 bytes truncated");

// Test 8: Background tab send-button self-heal (`GEB_BRIEFLY_ACTIVATE_TAB`) when initial synthetic clicks are ignored while hidden!
let activateTabRequests = 0;
let ignoreClicksUntilActivated = true;
const origPostMessage = sandbox.window.postMessage;
sandbox.window.postMessage = (msg) => {
  if (msg && msg.channel === "GEB_MAIN_TO_ISOLATED" && msg.payload && msg.payload.type === "briefly_activate_tab") {
    activateTabRequests += 1;
    ignoreClicksUntilActivated = false;
    realSetTimeout(() => {
      doc.dispatchEvent(
        Object.assign(new MockEvent("message"), {
          source: vmWindow,
          origin: mockLocation.origin,
          data: {
            channel: "GEB_ISOLATED_TO_MAIN",
            action: "briefly_activate_tab_done",
            payload: { req_id: msg.payload.req_id, ok: true }
          }
        })
      );
    }, 10);
    return;
  }
  origPostMessage(msg);
};

// Replace sendBtn click listener temporarily so first clicks while `ignoreClicksUntilActivated === true` do NOT clear composer or emit reply
const savedSendListeners = sendBtn._listeners.get("click") || [];
sendBtn._listeners.set("click", [
  () => {
    if (ignoreClicksUntilActivated) {
      // Simulate frozen background ProseMirror ignoring synthetic click!
      return;
    }
    const submittedText = (proseMirror.innerText || proseMirror.textContent || "").trim();
    proseMirror.replaceChildren();
    proseMirror._text = "";
    const replyNode = new MockElement("ucs-markdown", { text: `Self-healed reply: ${submittedText}` });
    chatShadow.appendChild(replyNode);
  void finishNetworkReply(replyNode.innerText);
  }
]);

await api.executePromptJob({
  request_id: "req-self-heal-send",
  prompt: "Prompt needing background tab self-heal",
  target_model: null,
  reset_chat: false,
  timeout_ms: 6000
});

assert.ok(activateTabRequests >= 1, "Must request GEB_BRIEFLY_ACTIVATE_TAB when background click did not start submission");
const healedEv = emittedEvents.find(
  (e) => e.type === "request_complete" && e.request_id === "req-self-heal-send"
);
assert.ok(healedEv, "Must complete prompt after background tab self-heal");
assert.equal(healedEv.reply, "Self-healed reply: Prompt needing background tab self-heal");
sendBtn._listeners.set("click", savedSendListeners);

// Match the current landing-page composer name and its deeper editor nesting.
searchBox.tagName = "UCS-SEARCH-BAR";
pmHost.remove();
let editorParent = searchShadow;
for (let i = 0; i < 8; i += 1) {
  const wrapper = new MockElement("div");
  editorParent.appendChild(wrapper);
  editorParent = wrapper;
}
editorParent.appendChild(pmHost);

// Real upload completion can show only an image preview and a Remove file button, without a filename.
const fileInput = new MockElement("input", { attrs: { type: "file" } });
searchShadow.appendChild(fileInput);
let changedFiles = null;
fileInput.addEventListener("change", () => {
  changedFiles = fileInput.files;
  const remove = new MockElement("button", { attrs: { "aria-label": "Remove file" } });
  remove.style.opacity = "0"; // Actual page reveals this control only on hover.
  searchShadow.appendChild(remove);
});
await api.attachImagesToComposer([{ data_url: "data:image/png;base64,iVBORw0KGgo=", name: "never-visible.png", mime_type: "image/png" }]);
assert.equal(changedFiles.length, 1);
assert.equal(changedFiles[0].name, "never-visible.png");
assert.equal(changedFiles[0].type, "image/png");
// Remove the mock composer attachment after its completed upload.
for (const button of searchShadow.querySelectorAll('button')) {
  if (button.getAttribute("aria-label") === "Remove file") button.remove();
}
// Seven real attachments can contain eight nested chip wrappers. They must not
// satisfy an eight-file upload before the final remove control appears.
let finalAttachmentArrived = false;
fileInput._listeners.set("change", [() => {
  for (let i = 0; i < 7; i += 1) {
    const wrapper = new MockElement("ucs-file-chip");
    wrapper.appendChild(new MockElement("mat-chip"));
    wrapper.appendChild(new MockElement("button", {attrs: {"aria-label": "Remove file"}}));
    searchShadow.appendChild(wrapper);
  }
  // An unrelated old conversation button must not count as a composer attachment.
  const oldRemove = new MockElement("button", {attrs: {"aria-label": "Remove file"}});
  chatShadow.appendChild(oldRemove);
  setTimeout(() => {
    finalAttachmentArrived = true;
    searchShadow.appendChild(new MockElement("button", {attrs: {"aria-label": "Remove file"}}));
    oldRemove.remove();
  }, 2200);
}]);
await api.attachFilesToComposer(Array.from({length:8}, (_, i) => ({data_url: "data:image/png;base64,iVBORw0KGgo=", name: `image-${i}.png`, mime_type: "image/png"})));
assert.ok(finalAttachmentArrived, "nested chips or old conversation controls must not masquerade as complete uploads");
await assert.rejects(api.attachFilesToComposer([]), /残留/);
for (const el of searchShadow.querySelectorAll('ucs-file-chip, button')) {
  if (el.tagName.toLowerCase() === "ucs-file-chip" || el.getAttribute("aria-label") === "Remove file") el.remove();
}

searchBox.tagName = "UCS-SEARCH-BOX";
pmHost.remove();
searchShadow.appendChild(pmHost);

// A cancelled job must never submit, even when the Send button is available.
let unexpectedSends = 0;
sendBtn._listeners.set("click", [() => { unexpectedSends += 1; }]);
await assert.rejects(api.submitComposerWithVerification(proseMirror, "cancelled", {cancelled: true, networkText: ""}), /取消/);
assert.equal(unexpectedSends, 0);

// The request can be sent before any reply or DOM acknowledgement arrives.
let submittedOnce = 0;
const slowJob = {cancelled:false, networkText:"", networkDone:false};
sendBtn._listeners.set("click", [() => { submittedOnce += 1; slowJob.submissionStarted = true; }]);
await api.submitComposerWithVerification(proseMirror, "slow network headers", slowJob, api.collectAssistantReplyTexts().length);
assert.equal(submittedOnce, 1, "an in-flight network request must prevent fallback resubmissions");

// An interrupted network stream must not become a successful DOM fallback.
let stopClicks = 0;
const stopBtn = new MockElement("button", {attrs: {"aria-label": "Stop generating"}});
stopBtn.addEventListener("click", () => { stopClicks += 1; stopBtn.parentNode.removeChild(stopBtn); });
sendBtn._listeners.set("click", [() => {
  const job = sandbox.__GEB_MAIN_STATE__.getActiveJob();
  job.networkText = "partial answer";
  job.networkError = "simulated dropped stream";
  chatShadow.appendChild(new MockElement("ucs-markdown", {text: "partial answer"}));
  chatShadow.appendChild(stopBtn);
}]);
await api.executePromptJob({request_id: "req-network-failure", prompt: "must fail", reset_chat: false});
assert.ok(emittedEvents.some(e => e.type === "request_error" && e.request_id === "req-network-failure" && /dropped/.test(e.error)));
assert.ok(!emittedEvents.some(e => e.type === "request_complete" && e.request_id === "req-network-failure"));
assert.equal(stopClicks, 1, "an error must stop the actual page generation");

// Cancel while the page is generating: click Stop and emit no completion.
sendBtn._listeners.set("click", [() => {
  chatShadow.appendChild(stopBtn);
  doc.dispatchEvent(Object.assign(new MockEvent("message"), {
    source: vmWindow, origin: mockLocation.origin,
    data: {channel:"GEB_ISOLATED_TO_MAIN", action:"cancel_request", payload:{request_id:"req-cancel-generation"}}
  }));
}]);
await api.executePromptJob({request_id: "req-cancel-generation", prompt: "cancel after submit", reset_chat: false});
assert.equal(stopClicks, 2);
assert.ok(!emittedEvents.some(e => e.type === "request_complete" && e.request_id === "req-cancel-generation"));
sendBtn._listeners.set("click", savedSendListeners);

// A rendered code block has an artificial closing fence before its JSON finishes growing.
const growthListeners = (sendBtn._listeners.get('click') || []).slice();
const growthPayload = JSON.stringify({name:'exec_command',arguments:{cmd:'printf DOM_GROWTH_OK'}});
sendBtn._listeners.set('click',[()=>{
  const bubble = new MockElement('ucs-markdown');
  bubble.appendChild(new MockElement('p',{text:'正在读取。'}));
  const pre = new MockElement('pre');
  const code = new MockElement('code',{className:'language-codex_tool_call',text:growthPayload.slice(0,40)});
  pre.appendChild(code);bubble.appendChild(pre);chatShadow.appendChild(bubble);chatShadow.appendChild(stopBtn);
  realSetTimeout(()=>{code._text = growthPayload.slice(0,52);},420);
  realSetTimeout(()=>{code._text = growthPayload;void finishNetworkReply('```codex_tool_call\n'+growthPayload+'\n```');stopBtn.remove();},900);
}]);
await api.executePromptJob({request_id:'req-dom-tool-growth',prompt:'DOM growth test',reset_chat:false});
const growthEvents = emittedEvents.filter(e=>e.request_id==='req-dom-tool-growth');
const growthStream = growthEvents.filter(e=>e.type==='stream_chunk').map(e=>e.delta).join('');
const growthComplete = growthEvents.find(e=>e.type==='request_complete');
assert.ok(growthComplete,'the rendered tool call must complete');
assert.ok(growthComplete.reply.startsWith(growthStream),'DOM tool growth must keep a stable streamed prefix');
assert.equal(growthStream,'```codex_tool_call\n'+growthPayload+'\n```','only complete original network payload reaches transport');
assert.ok(growthComplete.reply.includes('DOM_GROWTH_OK'));
sendBtn._listeners.set('click',growthListeners);
console.log('PASS: growing DOM tool blocks retain a stable prefix');

// Render order cannot select the source; original response is always authoritative.
for (const source of ['network','dom']) {
  const listenersBefore = (sendBtn._listeners.get('click') || []).slice();
  const id = 'req-source-lock-'+source;
  sendBtn._listeners.set('click',[()=>{
    const job = sandbox.__GEB_MAIN_STATE__.getActiveJob();
    const reply = new MockElement('ucs-markdown',{text:source==='network'?'DOM rendering is longer than the network text':'DOM first stable text'});
    chatShadow.appendChild(reply);
    if (source==='network') {
      job.networkText='NETWORK_RAW';job.onNetworkDelta('NETWORK_RAW');
      realSetTimeout(()=>{job.networkDone=true;job.onNetworkDone();},2300);
    } else {
      chatShadow.appendChild(stopBtn);
      realSetTimeout(()=>{job.networkText='DIFFERENT_NETWORK_TEXT';job.onNetworkDelta(job.networkText);job.networkDone=true;job.onNetworkDone();stopBtn.remove();},900);
    }
  }]);
  await api.executePromptJob({request_id:id,prompt:'transport lock test',reset_chat:false});
  const events = emittedEvents.filter(e=>e.request_id===id);
  const streamed=events.filter(e=>e.type==='stream_chunk').map(e=>e.delta).join('');
  const completed=events.find(e=>e.type==='request_complete');
  assert.ok(completed,'locked source must complete');
  assert.ok(completed.reply.startsWith(streamed),'reply source cannot switch after streaming');
  assert.equal(completed.reply,source==='network'?'NETWORK_RAW':'DIFFERENT_NETWORK_TEXT');
  sendBtn._listeners.set('click',listenersBefore);
}
console.log('PASS: original response remains authoritative when DOM appears earlier');

// Missing input chunks must fail before entering anything in the composer.
for (const [action, payload] of [
  ["prompt_chunk", {request_id:"req-missing-chunk", chunk_index:0, total_chunks:2, data:"only half"}],
  ["execute_prompt", {request_id:"req-missing-chunk", chunked:true, total_chunks:2}]
]) doc.dispatchEvent(Object.assign(new MockEvent("message"), {
  source:vmWindow, origin:mockLocation.origin, data:{channel:"GEB_ISOLATED_TO_MAIN", action, payload}
}));
await new Promise(r => realSetTimeout(r, 30));
assert.ok(emittedEvents.some(e => e.type === "request_error" && e.request_id === "req-missing-chunk" && /分段/.test(e.error)));

console.log("ALL_SHADOW_DOM_DRIVER_TESTS_PASSED");

{
 const saved=(sendBtn._listeners.get('click')||[]).slice();
 sendBtn._listeners.set('click',[()=>{
  const reply=new MockElement('ucs-markdown',{text:'正在读取文件。未完成的排版'});
  chatShadow.appendChild(reply);chatShadow.appendChild(stopBtn);
  realSetTimeout(()=>{reply._text='正在读取文件。\n\n最终排版';void finishNetworkReply(reply._text);stopBtn.remove();},900);
 }]);
 await api.executePromptJob({request_id:'req-dom-rewrite-regression',prompt:'rewrite test',reset_chat:false});
 const events=emittedEvents.filter(e=>e.request_id==='req-dom-rewrite-regression');
 const streamed=events.filter(e=>e.type==='stream_chunk').map(e=>e.delta).join('');
 const result=events.find(e=>e.type==='request_complete');
 assert.equal(streamed,'正在读取文件。\n\n最终排版');
 assert.equal(result.reply,streamed);
 assert.equal(result.source,'network_stream');
 sendBtn._listeners.set('click',saved);
 console.log('PASS: rewritten DOM never reaches client stream or completion');
}
// A terminal callback cannot emit after the job has already completed.
{
 let finishedJob;
 const saved=(sendBtn._listeners.get('click')||[]).slice();
 sendBtn._listeners.set('click',[()=>{finishedJob=sandbox.__GEB_MAIN_STATE__.getActiveJob();void finishNetworkReply('terminal');}]);
 await api.executePromptJob({request_id:'req-late-delta',prompt:'late callback test',reset_chat:false});
 const count=emittedEvents.length;
 sandbox.__GEB_MAIN_STATE__.parsePacket({groundedContent:{content:{text:'stale data'}}},finishedJob);
 finishedJob.onNetworkDelta('stale data');
 assert.equal(emittedEvents.length,count);
 assert.equal(finishedJob.networkText,'terminal');
 sendBtn._listeners.set('click',saved);
 console.log('PASS: completed response rejects late callbacks');
}


{
 const saved=(sendBtn._listeners.get('click')||[]).slice();
 const id='req-missing-original-response';
 sendBtn._listeners.set('click',[()=>{
  const job=sandbox.__GEB_MAIN_STATE__.getActiveJob();
  chatShadow.appendChild(new MockElement('ucs-markdown',{text:'A complete-looking page must not become a response'}));
  realSetTimeout(()=>{job.networkError='original response unavailable';},1900);
 }]);
 await api.executePromptJob({request_id:id,prompt:'no original response test',reset_chat:false});
 const events=emittedEvents.filter(e=>e.request_id===id);
 assert.ok(events.some(e=>e.type==='request_error' && /unavailable/.test(e.error)));
 assert.ok(!events.some(e=>e.type==='stream_chunk' || e.type==='request_complete'));
 sendBtn._listeners.set('click',saved);
 console.log('PASS: a missing original response never becomes DOM success');
}

// Direct clipboard paste fallback when no <input type="file"> exists on the page
{
 fileInput.remove();
 let pastedFiles = [];
 const onPaste = (ev) => {
  const dt = ev.clipboardData || ev.dataTransfer;
  if (dt && dt.files) {
   pastedFiles = Array.from(dt.files);
   const remBtn = new MockElement('button', { attrs: { 'aria-label': 'Remove file' } });
   searchShadow.appendChild(remBtn);
  }
 };
 proseMirror.addEventListener('paste', onPaste);
 await api.attachFilesToComposer([{ data_url: 'data:image/png;base64,iVBORw0KGgo=', name: 'clipboard-pasted.png', mime_type: 'image/png' }]);
 assert.equal(pastedFiles.length, 1);
 assert.equal(pastedFiles[0].name, 'clipboard-pasted.png');
 for (const b of searchShadow.querySelectorAll('button')) {
  if (b.getAttribute('aria-label') === 'Remove file') b.remove();
 }
 searchShadow.appendChild(fileInput);
 console.log('PASS: clipboard paste fallback attaches files directly when file input is absent');
}

// Flexible attachment payload formats: empty-MIME data URL, plain text data URL, and raw base64
{
 let uploaded = [];
 fileInput._listeners.set('change', [() => {
  uploaded = Array.from(fileInput.files || []);
  for (let i = 0; i < uploaded.length; i += 1) {
   searchShadow.appendChild(new MockElement('button', { attrs: { 'aria-label': 'Remove file' } }));
  }
 }]);
 await api.attachFilesToComposer([
  { data_url: 'data:;base64,SGVsbG8=', name: 'empty-mime.txt', mime_type: 'text/plain; charset=utf-8' },
  { data_url: 'data:text/plain;charset=utf-8,Hello%20World', name: 'plain-url.txt', mime_type: 'text/plain' },
  { data_url: 'iVBORw0KGgo=', filename: 'raw-b64.png', mime_type: 'image/png' }
 ]);
 assert.equal(uploaded.length, 3);
 assert.equal(uploaded[0].name, 'empty-mime.txt');
 assert.equal(uploaded[0].type, 'text/plain');
 assert.equal(uploaded[1].name, 'plain-url.txt');
 assert.equal(uploaded[2].name, 'raw-b64.png');
 assert.equal(uploaded[2].type, 'image/png');
 for (const b of searchShadow.querySelectorAll('button')) {
  if (b.getAttribute('aria-label') === 'Remove file') b.remove();
 }
 await assert.rejects(
  () => api.attachFilesToComposer([{ data_url: '', name: 'empty.bin', mime_type: 'application/octet-stream' }]),
  /附件内容格式无效/
 );
 console.log('PASS: flexible attachment formats accepted and empty payloads rejected cleanly');
}

process.exit(0);
