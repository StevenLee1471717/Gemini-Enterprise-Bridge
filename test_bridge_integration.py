"""Full-chain integration tests: Client -> FastAPI Server -> Extension Stack -> Strict TrustedTypes DOM -> Response."""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any, Dict, List

from fastapi.testclient import TestClient

from config import BridgeSettings
from server import create_app


_NODE_EXTENSION_PIPELINE_SCRIPT = r"""
import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";

const [driverPath, payloadJsonPath] = process.argv.slice(2);
const driverSource = fs.readFileSync(driverPath, "utf8");
const inputSpec = JSON.parse(fs.readFileSync(payloadJsonPath, "utf8"));

class MockEvent {
  constructor(type, init = {}) {
    this.type = type;
    this.bubbles = Boolean(init.bubbles);
    this.composed = Boolean(init.composed);
    this.inputType = init.inputType || "";
    this.data = init.data || "";
    this.clipboardData = init.clipboardData || null;
  }
}

class MockDataTransfer {
  constructor() { this._map = new Map(); }
  setData(k, v) { this._map.set(k, String(v)); }
  getData(k) { return this._map.get(k) || ""; }
}

function matchSimple(el, sel) {
  const s = sel.trim();
  if (!s) return false;
  if (s === "*") return true;
  if (s.startsWith(".")) return (el.className || "").split(/\s+/).includes(s.slice(1));
  if (s.startsWith("[") && s.endsWith("]")) {
    const inner = s.slice(1, -1).trim();
    const ci = /\s+i$/i.test(inner);
    const body = ci ? inner.replace(/\s+i$/i, "").trim() : inner;
    if (!body.includes("=")) return el.hasAttribute(body);
    const m = body.match(/^([a-zA-Z0-9_-]+)(\*=|\^=|=)"?([^"]*)"?$/);
    if (!m) return false;
    const [, k, op, expRaw] = m;
    const vRaw = el.getAttribute(k);
    if (vRaw === null) return false;
    const v = ci ? vRaw.toLowerCase() : vRaw;
    const exp = ci ? expRaw.toLowerCase() : expRaw;
    if (op === "=") return v === exp;
    if (op === "*=") return v.includes(exp);
    if (op === "^=") return v.startsWith(exp);
    return false;
  }
  if (s.includes("[")) {
    const idx = s.indexOf("[");
    return matchSimple(el, s.slice(0, idx)) && matchSimple(el, s.slice(idx));
  }
  return el.tagName.toLowerCase() === s.toLowerCase();
}

class NodeBase {
  constructor() {
    this.children = [];
    this.parentNode = null;
    this.parentElement = null;
    this._listeners = new Map();
  }
  appendChild(ch) {
    if (ch && ch.__isFrag) {
      for (const sub of ch.children.slice()) this.appendChild(sub);
      ch.children = [];
      return ch;
    }
    ch.parentNode = this;
    ch.parentElement = this instanceof Elem ? this : null;
    this.children.push(ch);
    return ch;
  }
  replaceChildren(...nodes) {
    this.children = [];
    for (const n of nodes) this.appendChild(n);
  }
  removeChild(ch) {
    const i = this.children.indexOf(ch);
    if (i !== -1) this.children.splice(i, 1);
    return ch;
  }
  get firstChild() { return this.children[0] || null; }
  getRootNode() {
    let c = this;
    while (c.parentNode) c = c.parentNode;
    return c;
  }
  contains(other) {
    let c = other;
    while (c) { if (c === this) return true; c = c.parentNode; }
    return false;
  }
  querySelectorAll(sel) {
    const out = [];
    const parts = sel.split(",");
    const walk = (n) => {
      for (const ch of n.children) {
        if (ch instanceof Elem && parts.some((p) => matchSimple(ch, p))) out.push(ch);
        walk(ch);
      }
    };
    walk(this);
    return out;
  }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
  addEventListener(t, fn) {
    if (!this._listeners.has(t)) this._listeners.set(t, []);
    this._listeners.get(t).push(fn);
  }
  removeEventListener(t, fn) {
    const a = this._listeners.get(t) || [];
    const i = a.indexOf(fn);
    if (i !== -1) a.splice(i, 1);
  }
  dispatchEvent(ev) {
    for (const fn of (this._listeners.get(ev.type) || []).slice()) fn.call(this, ev);
    if (ev.bubbles && this.parentNode) this.parentNode.dispatchEvent(ev);
    else if (ev.bubbles && ev.composed && this.host) this.host.dispatchEvent(ev);
    return true;
  }
}

class Shadow extends NodeBase {
  constructor(host) { super(); this.host = host; }
  getElementById(id) { return this.querySelectorAll("*").find((e) => e.id === id) || null; }
}

class Elem extends NodeBase {
  constructor(tag, opts = {}) {
    super();
    this.tagName = tag.toUpperCase();
    this.id = opts.id || "";
    this.className = opts.className || "";
    this.disabled = Boolean(opts.disabled);
    this.readOnly = false;
    this._attrs = new Map(Object.entries(opts.attrs || {}));
    this._text = opts.text || "";
    this._rect = opts.rect || { left: 100, top: 100, right: 240, bottom: 136, width: 140, height: 36 };
    this.style = { display: opts.hidden ? "none" : "block", visibility: "visible", opacity: "1" };
    this.shadowRoot = null;
    this.classList = { contains: (c) => (this.className || "").split(/\s+/).includes(c) };
  }
  attachShadow() { this.shadowRoot = new Shadow(this); return this.shadowRoot; }
  cloneNode() { return new Elem(this.tagName.toLowerCase()); }
  getAttribute(k) { return this._attrs.has(k) ? this._attrs.get(k) : null; }
  hasAttribute(k) { return this._attrs.has(k); }
  get nextElementSibling() { return null; }

  // STRICT TRUSTED TYPES ENFORCEMENT: Any HTML string sink throws immediately!
  set innerHTML(_) { throw new TypeError("TrustedHTML required on innerHTML"); }
  set outerHTML(_) { throw new TypeError("TrustedHTML required on outerHTML"); }
  insertAdjacentHTML() { throw new TypeError("TrustedHTML required on insertAdjacentHTML"); }

  // Light-DOM innerText/textContent intentionally does NOT read `.shadowRoot` (matching real browsers!)
  get innerText() {
    const sub = this.children.map((c) => c.innerText || "").filter(Boolean).join("\n");
    return [this._text, sub].filter(Boolean).join("\n");
  }
  set innerText(v) { this._text = v; }
  get textContent() {
    const sub = this.children.map((c) => c.textContent || "").filter(Boolean).join(" ");
    return [this._text, sub].filter(Boolean).join(" ");
  }
  set textContent(v) { this._text = v; }
  getBoundingClientRect() {
    return this.style.display === "none"
      ? { left: 0, top: 0, right: 0, bottom: 0, width: 0, height: 0 }
      : this._rect;
  }
  focus() {}
  click() { if (!this.disabled) this.dispatchEvent(new MockEvent("click", { bubbles: true, composed: true })); }
}

const doc = new NodeBase();
doc.title = "Gemini Enterprise";
doc.hidden = true;
doc.visibilityState = "hidden";
doc.hasFocus = () => false;
doc.body = new Elem("body", { rect: { left: 0, top: 0, right: 1440, bottom: 900, width: 1440, height: 900 } });
doc.appendChild(doc.body);
doc.createElement = (t) => new Elem(t);
doc.createDocumentFragment = () => Object.assign(new NodeBase(), { __isFrag: true });
doc.getElementById = () => null;

const app = new Elem("ucs-standalone-app");
doc.body.appendChild(app);
const appSr = app.attachShadow();
const chat = new Elem("ucs-chat");
appSr.appendChild(chat);
const chatSr = chat.attachShadow();
const box = new Elem("ucs-search-box", { rect: { left: 280, top: 700, right: 1100, bottom: 830, width: 820, height: 130 } });
chatSr.appendChild(box);
const boxSr = box.attachShadow();

// Exact real-world `<ucs-model-selector aria-label="Choose model">` whose visible text `"Auto"`
// lives ONLY inside `modelSelector.shadowRoot` (light-DOM innerText === ""!)
const modelSelector = new Elem("ucs-model-selector", {
  attrs: { "aria-label": "Choose model", role: "button", "aria-haspopup": "menu" },
  rect: { left: 880, top: 780, right: 980, bottom: 816, width: 100, height: 36 }
});
const modelSr = modelSelector.attachShadow();
const modelInnerSpan = new Elem("span", { text: "Auto" });
modelSr.appendChild(modelInnerSpan);
boxSr.appendChild(modelSelector);

// ProseMirror editor + Send button (Send starts `disabled: true` until paste/input event fires!)
const prose = new Elem("div", {
  className: "ProseMirror",
  attrs: { contenteditable: "true", role: "textbox" },
  rect: { left: 300, top: 715, right: 1050, bottom: 770, width: 750, height: 55 }
});
boxSr.appendChild(prose);

const sendBtn = new Elem("button", {
  text: "arrow_upward",
  disabled: true,
  attrs: { "aria-label": "Send" },
  rect: { left: 1000, top: 780, right: 1040, bottom: 816, width: 40, height: 36 }
});
boxSr.appendChild(sendBtn);

// Handle ProseMirror paste & input events natively (enabling sendBtn!)
prose.addEventListener("paste", (ev) => {
  const txt = ev.clipboardData ? ev.clipboardData.getData("text/plain") : "";
  if (txt) {
    prose.replaceChildren();
    const p = new Elem("p", { text: txt });
    prose.appendChild(p);
    sendBtn.disabled = false;
  }
});
prose.addEventListener("input", () => {
  if ((prose.innerText || prose.textContent || "").trim().length > 0) {
    sendBtn.disabled = false;
  }
});

// Dropdown menu options whose labels ALSO live inside each `<ucs-model-option>` shadowRoot!
const menu = new Elem("div", { attrs: { role: "menu" }, hidden: true });
doc.body.appendChild(menu);
for (const lbl of ["Auto", "3.1 Pro", "4 Argon", "4 Carbon", "3.8 Flash"]) {
  const opt = new Elem("ucs-model-option", { attrs: { role: "menuitemradio" } });
  const optSr = opt.attachShadow();
  optSr.appendChild(new Elem("span", { text: lbl }));
  opt.addEventListener("click", () => {
    modelInnerSpan._text = lbl;
    menu.style.display = "none";
  });
  menu.appendChild(opt);
}
modelSelector.addEventListener("click", () => {
  menu.style.display = menu.style.display === "none" ? "block" : "none";
});

let clickedSendButton = false;
let capturedSubmittedText = "";
sendBtn.addEventListener("click", () => {
  clickedSendButton = true;
  capturedSubmittedText = (prose.innerText || prose.textContent || "").trim();
  const replyBubble = new Elem("ucs-markdown", { text: inputSpec.simulatedReply });
  chatSr.appendChild(replyBubble);
});

const loc = {
  href: "https://vertexaisearch.cloud.google.com/u/3/home/cid/demo-cid?hl=en_US",
  origin: "https://vertexaisearch.cloud.google.com",
  pathname: "/u/3/home/cid/demo-cid",
  search: "?hl=en_US"
};

const sandbox = {
  window: null,
  globalThis: null,
  document: doc,
  location: loc,
  CSS: { escape: (s) => s },
  URL,
  Headers,
  TextDecoder,
  AbortController,
  MessageChannel,
  DataTransfer: MockDataTransfer,
  ClipboardEvent: MockEvent,
  Event: MockEvent,
  MouseEvent: MockEvent,
  PointerEvent: MockEvent,
  KeyboardEvent: MockEvent,
  InputEvent: MockEvent,
  PopStateEvent: MockEvent,
  // Strict CSP: `trustedTypes.createPolicy` throws if policy name isn't allowlisted!
  trustedTypes: {
    createPolicy(name) {
      throw new TypeError(`Policy "${name}" disallowed by strict CSP trusted-types header`);
    }
  },
  HTMLTextAreaElement: function () {},
  HTMLInputElement: function () {},
  XMLHttpRequest: function () {},
  setTimeout,
  clearTimeout,
  requestAnimationFrame: () => 0, // Frozen rAF in hidden tab!
  console: { log() {}, warn() {}, error() {} }
};
sandbox.XMLHttpRequest.prototype = { open() {}, send() {} };
sandbox.window = sandbox;
sandbox.globalThis = sandbox;
sandbox.window.location = loc;
sandbox.window.innerHeight = 900;
sandbox.window.fetch = async () => ({ ok: true, body: null });
sandbox.window.getComputedStyle = (el) => el.style;
sandbox.window.addEventListener = (t, fn) => doc.addEventListener(t, fn);
sandbox.window.removeEventListener = (t, fn) => doc.removeEventListener(t, fn);
sandbox.window.postMessage = () => {};

vm.createContext(sandbox);
vm.runInContext(driverSource, sandbox);
const vmWin = vm.runInContext("window", sandbox);

const outEvents = [];
const chunks = new Map();
sandbox.window.postMessage = (msg) => {
  if (!msg || msg.channel !== "GEB_MAIN_TO_ISOLATED" || !msg.payload) return;
  const p = msg.payload;
  if (p.type === "sw_sleep") {
    setTimeout(() => {
      doc.dispatchEvent(Object.assign(new MockEvent("message"), {
        source: vmWin,
        origin: loc.origin,
        data: { channel: "GEB_ISOLATED_TO_MAIN", action: "sw_sleep_done", payload: { sleep_id: p.sleep_id } }
      }));
    }, 10);
    return;
  }
  if (p.type === "reply_chunk") {
    if (!chunks.has(p.request_id)) chunks.set(p.request_id, []);
    chunks.get(p.request_id)[p.chunk_index] = p.data;
    return;
  }
  if (p.type === "request_complete" && p.chunked) {
    outEvents.push({ ...p, reply: (chunks.get(p.request_id) || []).join(""), chunked: false });
    return;
  }
  outEvents.push(p);
};

await sandbox.__GEB_MAIN_API__.executePromptJob({
  request_id: inputSpec.requestId,
  prompt: inputSpec.prompt,
  target_model: inputSpec.targetModel,
  reset_chat: false,
  timeout_ms: 8000
});

const done = outEvents.find((e) => e.type === "request_complete" && e.request_id === inputSpec.requestId);
assert.ok(done, "Integration page-driver execution must emit request_complete");

process.stdout.write(JSON.stringify({
  ok: true,
  clickedSendButton,
  submittedLength: capturedSubmittedText.length,
  activeModel: done.active_model,
  reply: done.reply
}));
process.exit(0);
"""


def test_full_chain_client_server_extension_dom_integration(tmp_path: Path) -> None:
    """Simulate full `Client -> FastAPI -> Chunked WS -> SW Reassembly -> TrustedTypes DOM -> Tool Call -> Final Reply`."""
    node_bin = shutil.which("node")
    assert node_bin is not None

    harness_mjs = tmp_path / "run_dom_step.mjs"
    harness_mjs.write_text(_NODE_EXTENSION_PIPELINE_SCRIPT, encoding="utf-8")
    driver_js = Path(__file__).resolve().parent / "page-driver.js"

    app = create_app(
        BridgeSettings(
            auto_export_extension=False,
            request_timeout_seconds=20.0,
            queue_timeout_seconds=10.0,
        )
    )

    # Create a large (>40KB) tool schema set so Turn 1 exercises `execute_prompt_chunk` + `reset_chat=True` simultaneously!
    large_tools: List[Dict[str, Any]] = [
        {
            "type": "function",
            "function": {
                "name": f"enterprise_tool_{idx}",
                "description": f"Enterprise connector tool #{idx} " + ("detailed_schema_doc " * 60),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Search query " * 25},
                        "limit": {"type": "integer"},
                    },
                    "required": ["query"],
                },
            },
        }
        for idx in range(28)
    ]

    dom_runs: List[Dict[str, Any]] = []

    with TestClient(app) as client:
        ready = threading.Event()
        with client.websocket_connect("/ws/browser?client_id=ext-integ") as ws:
            ws.receive_json()  # welcome

            # Also verify stale socket race protection: open & close a duplicate client_id socket?
            # First register tab status on primary socket:
            ws.send_json(
                {
                    "type": "tab_status",
                    "tab_id": 301,
                    "tab_url": "https://vertexaisearch.cloud.google.com/u/3/home/cid/demo-cid/r/session/1000000000000000001?hl=en_US",
                    "ready": True,
                }
            )
            ws.receive_json()  # ack

            def extension_bridge_worker() -> None:
                ready.set()
                sw_prompt_buffer: Dict[str, Dict[int, str]] = {}

                for step_idx in range(2):
                    while True:
                        pkt = ws.receive_json()
                        if pkt["type"] == "execute_prompt_chunk":
                            rid = pkt["request_id"]
                            sw_prompt_buffer.setdefault(rid, {})[pkt["chunk_index"]] = pkt["data"]
                            continue
                        if pkt["type"] == "execute_prompt":
                            rid = pkt["request_id"]
                            if pkt.get("chunked"):
                                parts = sw_prompt_buffer.pop(rid, {})
                                full_prompt = "".join(parts[i] for i in range(pkt["total_chunks"]))
                            else:
                                full_prompt = pkt["prompt"]

                            # Simulate `background.js` resetting tab BEFORE injecting chunks into `page-driver.js`!
                            simulated_reply = (
                                '[TOOL_CALL]{"name": "enterprise_tool_0", "arguments": {"query": "Q4 OKR", "limit": 5}}[/TOOL_CALL]'
                                if step_idx == 0
                                else "根据检索到的 Q4 OKR 数据，核心指标已全部达成。"
                            )

                            spec_file = tmp_path / f"step_{step_idx}.json"
                            spec_file.write_text(
                                json.dumps(
                                    {
                                        "requestId": rid,
                                        "prompt": full_prompt,
                                        "targetModel": pkt.get("target_model"),
                                        "simulatedReply": simulated_reply,
                                    },
                                    ensure_ascii=False,
                                ),
                                encoding="utf-8",
                            )

                            proc = subprocess.run(
                                [node_bin, str(harness_mjs), str(driver_js), str(spec_file)],
                                capture_output=True,
                                text=True,
                                encoding="utf-8",
                                errors="replace",
                                timeout=15,
                                check=False,
                            )
                            assert proc.returncode == 0, f"Node DOM integration failed:\n{proc.stderr}"
                            dom_result = json.loads(proc.stdout)
                            dom_runs.append(dom_result)

                            cb = client.post(
                                "/api/bridge/callback",
                                json={
                                    "client_id": "ext-integ",
                                    "request_id": rid,
                                    "reply": dom_result["reply"],
                                    "active_model": dom_result["activeModel"],
                                    "source": "integration_dom",
                                },
                            )
                            assert cb.status_code == 200
                            break

            worker = threading.Thread(target=extension_bridge_worker, daemon=True)
            worker.start()
            ready.wait(timeout=3.0)

            # Step 1: Large (>35KB) chunked tool request + `4argon` + automatic new-chat reset
            msgs: List[Dict[str, Any]] = [
                {"role": "system", "content": "Enterprise OKR Context:\n" + ("Departmental OKR baseline record; " * 650)},
                {"role": "user", "content": "请查询 Q4 OKR 进展"},
            ]
            r1 = client.post(
                "/v1/chat/completions",
                json={"model": "4argon", "messages": msgs, "tools": large_tools},
            )
            assert r1.status_code == 200
            msg1 = r1.json()["choices"][0]["message"]
            assert msg1["tool_calls"][0]["function"]["name"] == "enterprise_tool_0"

            # Step 2: Send tool execution result -> SSE streaming final answer
            msgs.append(msg1)
            msgs.append(
                {
                    "role": "tool",
                    "tool_call_id": msg1["tool_calls"][0]["id"],
                    "content": '{"okr_completion": "100%"}',
                }
            )
            with client.stream(
                "POST",
                "/v1/chat/completions",
                json={"model": "gemini-4-carbon", "stream": True, "tools": large_tools, "messages": msgs},
            ) as r2:
                assert r2.status_code == 200
                final_answer = ""
                for ln in r2.iter_lines():
                    if ln and ln != "data: [DONE]":
                        ch = json.loads(ln[6:]).get("choices", [])
                        if ch and ch[0]["delta"].get("content"):
                            final_answer += ch[0]["delta"]["content"]
                assert "核心指标已全部达成" in final_answer

            worker.join(timeout=5.0)
            status_snapshot = client.get("/api/status").json()
            # Must report "4 Carbon" after switching from "4 Argon" on Turn 2, NEVER "Choose model"!
            assert status_snapshot["hub"]["active_client"]["active_model"] == "4 Carbon"

    assert len(dom_runs) == 2
    assert dom_runs[0]["clickedSendButton"] is True
    assert dom_runs[0]["submittedLength"] > 33000  # Proves chunked + reset_chat NEVER produces len=0!
    assert dom_runs[0]["activeModel"] == "4 Argon"
    assert dom_runs[1]["clickedSendButton"] is True
    assert dom_runs[1]["activeModel"] == "4 Carbon"


def test_stale_websocket_close_does_not_evict_newer_connection() -> None:
    """Ensure closing an older WebSocket sharing the same `client_id` never disconnects the newer socket."""
    app = create_app(BridgeSettings(auto_export_extension=False))
    with TestClient(app) as client:
        ws1_cm = client.websocket_connect("/ws/browser?client_id=ext-same-id")
        ws1 = ws1_cm.__enter__()
        ws1.receive_json()

        with client.websocket_connect("/ws/browser?client_id=ext-same-id") as ws2:
            ws2.receive_json()
            ws2.send_json(
                {
                    "type": "tab_status",
                    "tab_id": 10,
                    "tab_url": "https://vertexaisearch.cloud.google.com/u/3/home",
                    "ready": True,
                }
            )
            ws2.receive_json()

            # Close old ws1 AFTER ws2 is active
            ws1_cm.__exit__(None, None, None)

            # Verify ws2 is STILL registered and healthy!
            health = client.get("/health").json()
            assert health["browser_connected"] is True
