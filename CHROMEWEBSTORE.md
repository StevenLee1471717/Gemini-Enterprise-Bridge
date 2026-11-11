# Chrome Web Store Listing & Permissions Reference

**Extension Name:** Gemini Enterprise Web-to-API Bridge  
**Current Version:** 1.0.0  
**Last Updated:** 2026-11-11  
**Distribution Target:** Local Developer Unpacked Extension / Private Enterprise Internal Distribution

---

## Short Description (<= 132 characters)

Connects your signed-in Google Cloud Gemini Enterprise web tab to a local OpenAI-compatible gateway on your own computer.

## Detailed Description

Gemini Enterprise Web-to-API Bridge lets developers and researchers use their active Google Cloud Vertex AI Search / Agentspace web session with local OpenAI-compatible desktop tools such as ChatBox, NextChat, and Cursor.

### Key Benefits
- **Zero credential extraction:** Uses your normal signed-in browser tab directly without copying cookies or tokens to external servers.
- **Local-only gateway:** Connects exclusively to `127.0.0.1` / `localhost` on your own machine.
- **Direct model selection:** Choose Gemini 3.1 Pro, Gemini 4 Argon, Gemini 3.8 Flash, or custom enterprise models directly from your local client.
- **Agentic & tool-calling ready:** Supports multi-step local tool execution workflows alongside real-time streaming responses.
- **Multi-account workspace support:** Select which signed-in `/u/0/`, `/u/1/`, or `/u/3/` Gemini Enterprise tab handles your requests.
- **One-click diagnostics:** Check page readiness, start a fresh conversation, or pause bridging anytime from the toolbar popup.

---

## Permissions Justification

Every permission in `manifest.json` is strictly scoped to local bridging:

| Permission / Host | Plain-English Justification |
|---|---|
| `storage` | Saves your local gateway URL, selected tab preference, and bridge enable/pause state across browser restarts. |
| `tabs` | Identifies open `vertexaisearch.cloud.google.com` tabs (including multi-account `/u/N/` paths) so you can see and choose which tab receives prompts. |
| `alarms` | Periodically checks and restores the connection to your local `127.0.0.1` gateway when the browser background worker wakes up. |
| `scripting` | Attaches the page bridge to already-open `vertexaisearch.cloud.google.com` tabs when the extension is first installed or reloaded without forcing a manual refresh. |
| `https://vertexaisearch.cloud.google.com/*` | Reads assistant responses and submits your prompts inside the Gemini Enterprise / Vertex AI Search web interface. |
| `http://127.0.0.1/*` & `http://localhost/*` | Communicates with the local OpenAI-compatible proxy server running on your own computer. |

---

## Privacy & Data Use Disclosure

- **Data Collection:** None. This extension does not collect analytics, telemetry, personal information, or browsing history.
- **Data Transmission:** Prompts and responses travel exclusively between your open `vertexaisearch.cloud.google.com` tab and your local loopback server (`127.0.0.1` / `localhost`). No third-party or external servers are contacted.
- **Compliance Note:** Intended strictly for personal local development in accordance with your organization's IT and data governance policies.

---

## Version History

- **1.0.0 (2026-11-11):** Initial release with local WebSocket gateway connection, multi-account tab selector, real-time response streaming, custom selector overrides, and page diagnostics.
