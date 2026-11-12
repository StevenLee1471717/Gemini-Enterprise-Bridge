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
const baseFetch = async () => {
  rawFetchCalls += 1;
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

// Simulate both short background replies AND 140KB+ large tool-call replies without TrustedHTML errors!
let nextSimulatedReplyOverride = "";
sendBtn.addEventListener("click", () => {
  const submittedText = (proseMirror.innerText || proseMirror.textContent || "").trim();
  const replyBody = nextSimulatedReplyOverride || `Background reply to: ${submittedText}`;
  nextSimulatedReplyOverride = "";
  const replyNode = new MockElement("ucs-markdown", { text: replyBody });
  chatShadow.appendChild(replyNode);
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
const expectedLargeReply = `[TOOL_CALL]{"name": "write_file", "arguments": {"path": "big.ts", "content": "${largeCodeArg}"}}[/TOOL_CALL]`;
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

console.log("ALL_SHADOW_DOM_DRIVER_TESTS_PASSED");
process.exit(0);
