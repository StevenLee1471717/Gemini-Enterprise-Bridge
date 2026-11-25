# Chrome Web Store Listing & Permissions Reference

**Extension Name:** Gemini Enterprise Web-to-API Bridge  
**Current Version:** 1.5.4  
**Last Updated:** 2026-11-25  
**Distribution Target:** Local Developer Unpacked Extension / Private Enterprise Internal Distribution

---

## Short Description (<= 132 characters)

Connects your signed-in Google Cloud Gemini Enterprise web tab to a local OpenAI-compatible gateway on your own computer.

## Detailed Description

Gemini Enterprise Web-to-API Bridge lets developers and researchers use their active Google Cloud Vertex AI Search / Agentspace web session with local OpenAI-compatible desktop tools such as ChatBox, NextChat, and Cursor.

### Key Benefits
- **Zero credential extraction:** Uses your normal signed-in browser tab directly without copying cookies or tokens to external servers.
- **Local-only gateway:** Connects exclusively to `127.0.0.1` / `localhost` on your own machine.
- **Direct & future-proof model selection:** Choose Gemini 4 Hydrogen, Gemini 4 Carbon, Gemini 4 Argon, Gemini 3.1 Pro, Gemini 3.8 Flash, or newly released enterprise models automatically discovered from your web session.
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
| `https://api.github.com/*`, `https://github.com/*`, & `https://*.githubusercontent.com/*` | Checks the official GitHub Releases feed and follows release-asset CDN redirects (`release-assets.githubusercontent.com` / `objects.githubusercontent.com`) so local developer installations stay up to date without cross-origin preflight failures. |

---

## Privacy & Data Use Disclosure

- **Data Collection:** None. This extension does not collect analytics, telemetry, personal information, or browsing history.
- **Data Transmission:** Prompts and responses travel exclusively between your open `vertexaisearch.cloud.google.com` tab and your local loopback server (`127.0.0.1` / `localhost`). No third-party or external servers are contacted.
- **Compliance Note:** Intended strictly for personal local development in accordance with your organization's IT and data governance policies.

---

## Version History

- **1.5.4 (2026-11-25):** Resolved repeated unclosed client tool-block errors when `streamAssist` swallows trailing closing fences on short/medium tool calls or over-fences nested backticks, eliminated 45-second post-RPC stalls via fast pure-append fence completion (`page-driver.js` `v2.9.1`), synchronized Service Worker driver version checks to prevent redundant script re-injections, and added dual-price quota percentage telemetry.
- **1.5.2 (2026-11-25):** Prevented unexpected mid-session exits caused by leaked multimodal/turn control tokens (`<end_of_image>`, `<start_of_image>`, `<end_of_turn>`) or empty thought-only replies via stream-safe filtering, `DegenerateReplyError` protocol guarding, two-stage seamless in-session & clean-rebuild recovery, and fast empty-RPC completion (`page-driver.js` `v2.8.9`), alongside dual pre/post-discount usage ledger metrics, pre-update usage backfill, and daily token trend charts.
- **1.5.1 (2026-11-22):** Eliminated periodic MV3 background disconnects, WebSocket reconnection collisions, false 503 offline errors during reconnects, and post-reconnect busy-tab errors; unlocked reliable 10-tab parallel concurrency and on-demand worker auto-spawning for multi-agent sessions; added seamless multi-leg continuation & bounded Part-1 fallback for oversized truncated tool-call JSON payloads (`page-driver.js` `v2.8.8`).
- **1.5.0 (2026-11-21):** Merged upstream concurrency & infrastructure updates tailored for GitHub Copilot — added selective batch tool calling for GitHub Copilot CLI/App/VS Code Chat tools, up to 64 live browser seat caps & strict session affinity, Cloudflare Tunnel lifecycle management with Windows sleep/wake recovery, `/status` public status monitor, granular client quotas, Windows 10/11 compatibility (including UNC SMB hang fix), plus Gemini 4 Hydrogen and dynamic web model auto-discovery (`page-driver.js` `v2.8.7`).
- **1.4.1 (2026-11-17):** Fixed attachment upload format errors across all file, image, audio, and video attachments, added seamless compatibility during background extension updates, and preserved standalone file uploads sent without accompanying message text.
- **1.4.0 (2026-11-16):** Integrated ahead fork enhancements (SQLite session archive & URL re-entry, Codex activity tracking, multi-client API distribution & tenant isolation, compaction auto-reset, UsageLedger, manual self-use tab toggle), upgraded direct web multimodal file/clipboard-paste upload & Copilot vision capability sync, and added runtime-configurable backend debug logging (`/api/logs` & `BRIDGE_LOG_LEVEL`).
- **1.3.0 (2026-11-16):** Integrated OpenAI `/v1/responses` & `/v1/files` support, multimodal media generation/transcoding, decorated snapshot deduplication, and global worker concurrency & stream integrity hardening (`page-driver.js` `v2.6.5`).
- **1.2.0 (2026-11-13):** Added end-to-end multimodal file and media support (images, video, audio, PDFs, Office documents, code/text, and binary attachments) with token-guarded local staging, multi-turn delta deduplication, and Trusted-Types-safe Shadow DOM composer upload.
- **1.1.3 (2026-11-12):** Fixed CORS preflight errors when checking GitHub Releases by expanding `*.githubusercontent.com` CDN host coverage, preferring the local gateway / GitHub REST API before redirected release assets, and removing non-CORS-safelisted request headers.
- **1.1.2 (2026-11-12):** Added full 1,048,576 input / 65,536 output token limit metadata across `/v1/models` and `GET /v1/models/{model_id}` plus automatic GitHub Copilot BYOK `provider_models` limit synchronization to prevent false 128k prompt budget caps.
- **1.1.1 (2026-11-12):** Eliminated background WebSocket connection-refused and handshake warnings via lightweight gateway readiness checks, and improved back/forward cache keep-alive port cleanup during tab navigation.
- **1.1.0 (2026-11-12):** Added Gemini 4 Carbon support, real-time context window and API cost equivalence telemetry, and automated GitHub Releases update detection with one-click local gateway upgrade.
- **1.0.0 (2026-11-11):** Initial release with local WebSocket gateway connection, multi-account tab selector, real-time response streaming, custom selector overrides, and page diagnostics.
