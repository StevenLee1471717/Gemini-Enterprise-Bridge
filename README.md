# Gemini Enterprise Web-to-API Bridge

将已登录的 **Google Cloud Gemini Enterprise（Vertex AI Search / Agentspace，`vertexaisearch.cloud.google.com`）** 安全、稳定地包装为标准本地 **OpenAI 兼容接口（`/v1/chat/completions` & `/v1/models`）**，无缝接入 **ChatBox、NextChat、Cursor、Cherry Studio、Open WebUI** 及 OpenAI Python/JS SDK。

> **⚠️ 免责声明 (Unofficial / Disclaimer)**：本项目为非官方开源技术研究与个人本地开发工具桥接方案，与 Google LLC / Google Cloud 无任何隶属、合作或官方背书关系。使用者须严格遵守所在企业/机构的 IT 安全与数据合规制度以及 Google 服务条款（Terms of Service）。如拥有企业 GCP 项目 IAM 权限，建议优先启用内置的 **方案一（GCloud 官方 ADC 模式）**。
>
> **🔒 安全建议 (Security Note)**：请务必在 `.env` 中保持 `BRIDGE_HOST=127.0.0.1`（仅限本机回环访问，切勿暴露至公网或 `0.0.0.0`），并强烈建议配置非空的强随机 `BRIDGE_API_KEY`（如 `BRIDGE_API_KEY=sk-your-private-random-key`），防止本机其他未授权进程调用本地代理接口。本项目全程**绝不提取、硬编码或落盘任何 Google 账号 Cookie / Token**。

---

## 为什么采用「MV3 浏览器扩展 + 混合驱动 + FastAPI」架构？

直接抓包提取企业账号 Cookie 或使用普通 `@grant none` 油猴脚本在 `vertexaisearch.cloud.google.com/u/3/...` 企业工作区会遇到三大阻碍，本项目逐一给出了工程级解决：

| 痛点 | 传统脚本/纯逆向的问题 | 本项目的解决方案 |
|---|---|---|
| **CSP 与私有网络拦截 (PNA)** | 企业页 `Content-Security-Policy (connect-src)` 直接拦截页面内 `new WebSocket("ws://127.0.0.1:8000")` | **主方案**：Manifest V3 扩展由后台 Service Worker 维持 WebSocket，天然绕过页面 CSP；**备选油猴**：采用 `GM_xmlhttpRequest` 特权通道长轮询，同样 100% 免疫 CSP |
| **多层嵌套 Shadow DOM + 动态签名** | Google 企业端基于 Lit / Angular Web Components（`ucs-standalone-app` / `ucs-chat` / `ProseMirror`），且内部 RPC 含动态 `SAPISIDHASH` 与 `at` 令牌 | **混合页面驱动 (`page-driver.js`)**：被动嗅探页面原生 `StreamAssist` 网络流实现毫秒级流式输出与企业引用提取，同时配备深度 Shadow DOM 穿透自动输入与增量稳定性检测，**第一条消息即可开箱即用**，完全复用真实浏览器 TLS 指纹与登录态 |
| **第三方客户端兼容性与并发冲突** | Cursor / NextChat 发送数组型 `content`、多轮完整历史及并发请求会导致普通脚本 `422` 报错或网页输入框串台 | **完整 OpenAI 网关 (`server.py`)**：原生支持 `stream: true` (SSE) 与非流式、字符串与多模态数组 `content`、**智能多轮增量上下文 (`smart` delta)** 以及 **异步 FIFO 互斥队列** |

```mermaid
flowchart LR
    Client["ChatBox / NextChat / Cursor\nOpenAI SDK"] -->|"HTTP / SSE\n127.0.0.1:8000/v1"| Server["FastAPI 本地网关\n(FIFO 队列 + 智能多轮上下文)"]
    Server <-->|"WebSocket /ws/browser\n(绕过页面 CSP)"| SW["Chrome/Edge MV3 扩展\nbackground.js"]
    SW <-->|"chrome.tabs + postMessage"| Driver["页面混合驱动 page-driver.js\n(StreamAssist 流嗅探 + 深度 Shadow DOM)"]
    Driver <-->|"原生会话 & TLS 指纹"| Vertex["vertexaisearch.cloud.google.com\n(/u/0 .. /u/3 企业工作区)"]
    Server -.->|"可选方案一"| GCloud["gcloud ADC 官方接口\nVertex AI / Agentspace API"]
```

---

## v1.2.x 架构与核心能力速览

当前版本（`v1.2.0`）在本地 OpenAI 兼容网关基础上构建了完整的生产级多模态、多会话与自动化运维闭环：

| 能力模块 | 核心实现与运行机制 |
|---|---|
| **全链路多模态文件与媒体支持 (`multimodal.py`)** | 原生兼容图片、视频、音频、PDF、Office 文档（`.docx`/`.xlsx`/`.pptx`）、代码与任意二进制文件（支持 Data URI、Base64、HTTP URL 与本地路径）；多轮对话按 `SHA-256` 增量去重免重复上传；通过令牌保护的 `/api/bridge/attachments/{id}` 与零 `.innerHTML` 三级 Shadow DOM 注入安全上传至网页输入框，同时支持官方 GCloud `inlineData`/`fileData` |
| **1M 上下文窗口与客户端上限同步** | `/v1/models` 与 `/v1/models/{model_id}` 全量声明 **`1,048,576` 输入 / `65,536` 输出** Token 上限（覆盖 **Gemini 4 Carbon**、**4 Argon**、**3.1 Pro**、**3.8 Flash** 及 `@会话名` 别名）；启动时自动运行 `sync_client_limits.py` 同步修复本地 GitHub Copilot App (`~/.copilot/data.db`) BYOK 模型的 `128K` 默认回退限制 |
| **上下文水位监控与官方 API 等价账本 (`6.8` 汇率)** | 实时计算各并行会话在 `1M` 窗口中的占用水位（历史消息 / 工具 Schema / 增量输入），并通过 `usage_stats.py` 按 Google 官方分段定价持久化累计输入/输出 Token 与等价节省费用（默认按 **`1 USD = 6.8 CNY`** 折算，可通过 `BRIDGE_USD_TO_CNY_RATE` 自定义，支持扩展弹窗、Web 控制台与 `/api/usage` 实时查看及重置） |
| **多标签页真并行池 (`WorkerSlot` Pool)** | 每个已打开的 Gemini Enterprise 标签页作为独立并发工作槽位，支持多客户端同时流式请求、对话前缀自动粘性路由、`@会话名` 显式固定绑定，以及并发繁忙时后台静默新建标签页扩容（`BRIDGE_AUTO_SPAWN_TABS=true`） |
| **GitHub Releases 双向自动更新** | `.github/workflows/ci-release.yml` 自动测试并发布带 `SHA-256` 清单的插件与后端产物；后端每次启动自动检查更新并保留 `.env` / `usage_stats.json`；MV3 扩展内置 `/health` 静默预检、bfcache 保活端口清理、后台空闲自动重载（`chrome.runtime.reload()`）及跨端版本不一致一键升级提示 |

---

## 3 分钟快速上手（Windows 一键启动）

### 第一步：启动本地 FastAPI 代理网关

双击运行 `start.bat`，或在 PowerShell 中执行：

```powershell
cd I:\Projects\Gemini-Enterprise-Bridge
.\start.ps1
```

脚本会自动：
1. 在 `%LOCALAPPDATA%\Gemini-Enterprise-Bridge\venv` 创建独立虚拟环境并安装依赖（保持项目目录纯净）；
2. 自动生成默认 `.env` 配置文件；
3. 自动导出带高清图标的独立扩展目录 `I:\Projects\Gemini-Enterprise-Bridge\extension`；
4. 启动本地服务 `http://127.0.0.1:8000`。

> 若希望直接使用现有 Python 环境手动启动：
> ```powershell
> pip install -r requirements.txt
> python server.py
> ```

### 第二步：在 Chrome 或 Edge 加载浏览器扩展（推荐主方案）

1. 打开浏览器访问 `chrome://extensions/`（Edge 访问 `edge://extensions/`）；
2. 开启右上角 **开发者模式 (Developer mode)**；
3. 点击 **加载已解压的扩展程序 (Load unpacked)**，选择目录：
   - 推荐选择启动后自动生成的 `I:\Projects\Gemini-Enterprise-Bridge\extension`（含 `16/48/128` 图标）；
   - 或直接选择项目根目录 `I:\Projects\Gemini-Enterprise-Bridge`（同样符合 Manifest V3，立即可用）。
4. 在同一浏览器中打开并登录你的 Gemini Enterprise 页面（例如 `https://vertexaisearch.cloud.google.com/u/3/home`），进入任意对话页或工作区主页。
5. 查看浏览器右上角扩展图标角标变为绿色的 **`ON`**（也可点击图标打开弹窗，点击 **检测页面元素** 验证输入框与连接状态，或访问 `http://127.0.0.1:8000/` 在线流式测试）。

> **备选方案（Tampermonkey 油猴脚本）**：若不想加载未打包扩展，可在 Tampermonkey 中新建脚本并粘贴 `gemini-enterprise-bridge.user.js`（启动后亦同步生成于 `userscript/`）。该脚本通过 `GM_xmlhttpRequest` 与本地网关通信，不会触发页面 CSP 报错（注意：请勿在同一标签页同时启用扩展与油猴脚本；若检测到扩展已激活，油猴脚本会自动让出控制权）。

### 第三步：接入 ChatBox / NextChat / Cursor

在任意支持自定义 OpenAI 接口的客户端中填入以下参数：

| 配置项 | 填写值 |
|---|---|
| **API Host / Base URL** | `http://127.0.0.1:8000/v1` |
| **API Key** | 任意非空字符串（如 `sk-local-bridge`；若在 `.env` 设置了 `BRIDGE_API_KEY` 则填对应值） |
| **Model（保持网页当前模型）** | `gemini-enterprise-web`（新建对话：`gemini-enterprise-web-newchat`） |
| **Model（自动切换 Gemini 3.1 Pro）** | `gemini-3.1-pro`（简写：`3.1pro` / `3.1-pro`；新建对话：`gemini-3.1-pro-newchat`） |
| **Model（自动切换 Gemini 4 Argon）** | `gemini-4-argon`（简写：`4argon` / `4-argon`；新建对话：`gemini-4-argon-newchat`） |
| **Model（自动切换 Gemini 4 Carbon）** | `gemini-4-carbon`（简写：`4carbon` / `4-carbon`；新建对话：`gemini-4-carbon-newchat`） |
| **Model（自动切换 Gemini 3.8 Flash）** | `gemini-3.8-flash`（简写：`3.8flash` / `3.8-flash`；新建对话：`gemini-3.8-flash-newchat`） |
| **Model（多会话固定绑定语法）** | 任意模型名后追加 `@会话名`（如 `gemini-4-argon@agent1`、`3.1pro@agent2`）或请求头 `X-Session-Id` |
| **Model（其他网页模型透传）** | `gemini-2.5-pro`、`gemini-2.5-flash` 或任意 `gemini-web:<网页菜单名称>` |
| **Model（官方 GCloud 模式）** | `gemini-enterprise-gcloud` |

> **提示**：网关同时兼容将 Base URL 填为 `http://127.0.0.1:8000` 或 `http://127.0.0.1:8000/v1`。`GET /v1/models` 与单模型检索接口 `GET /v1/models/{model_id}`（含 `4argon`、`4carbon`、`gemini-4-carbon@sess1` 等别名）均完整声明 `1,048,576` 上下文窗口与 `65,536` 输出上限（`max_prompt_tokens`、`max_output_tokens`、`context_window`、`limits` 等全生态字段）。若此前在 GitHub Copilot App 中添加 BYOK 模型时 `max_prompt_tokens` 为空导致被回退限制为 `128,000`，启动网关（或手动运行 `python sync_client_limits.py` / `python updater.py --sync-client-limits`）会自动同步修复本地 `provider_models` 上限。

---

## 核心特性说明

### 1. 客户端直接切换网页模型（3.1 Pro / 4 Argon / 4 Carbon / 3.8 Flash）

无需切回浏览器手动点选模型菜单：在 Cursor、Cline、ChatBox、NextChat 或代码中指定 `model`（例如 `gemini-3.1-pro`、`4argon`、`4carbon`、`3.8flash`），页面混合驱动会自动穿透多层 Shadow DOM 检查当前激活模型；若与目标不一致，则自动打开模型下拉框完成切换后再发送请求（若企业管理员锁定了单模型，也会自动记录提示并平滑降级发送，绝不中断请求）。

### 2. 完整 OpenAI Tool Calling (`tools` / `tool_calls`) 与多轮 Agentic Loop 支持
完美适配 **Cursor Agent、Cline、Roo Code、LangChain、OpenAI Agents SDK** 等智能体工具循环：
- **DOM 免疫型工具协议**：采用纯文本边界标记 `[TOOL_CALL]{"name": "...", "arguments": {...}}[/TOOL_CALL]`（同时兼容 `<tool_call>`、```` ```tool_call ```` 与强制函数 JSON 兜底解析），确保无论是原生 `StreamAssist` 流嗅探还是网页 Markdown DOM 渲染提取，工具调用参数均 100% 无损还原。
- **流式 SSE 零泄漏缓冲 (`ToolStreamProcessor`)**：在 `stream: true` 模式下实时流式输出助手思考/前言文本，自动拦截工具标记并转换为标准 OpenAI `delta.tool_calls` 分片与 `finish_reason: "tool_calls"`，绝不向客户端正文泄漏原始标签。
- **同会话增量 Agentic Loop (`smart_incremental_delta`)**：在多步工具调用循环（含单工具、并行多工具调用 `parallel_tool_calls`、连续多轮 `tool` 结果回传）中自动匹配历史前缀，保持 `reset_chat=false` 在同一网页对话内增量投递 `[TOOL_RESULT]`，且不重复刷入冗长的工具 JSON Schema。
- **100KB+ 超长上下文与大参数无损分块 + 异步 HTTP 回调 (`/api/bridge/callback`)**：
  - 下行超长工具 Schema / `[TOOL_RESULT]`（`>32KB`）自动拆分为 `execute_prompt_chunk` 经 `content-bridge.js` 的 `ReassemblyBuffer` 无损重组，并通过单次 `DocumentFragment.replaceChildren()` 高速写入编辑器；
  - 上行超长回复或大体积 `write_file` 工具调用自动通过 `POST /api/bridge/callback` 异步直传服务端，且 SSE 流式输出自动按 `<=12KB` 安全切片推送 `delta.tool_calls` 参数片段，彻底规避 WebSocket 帧上限与 64KB SSE 行缓冲截断。
- **后台静默执行与 Google Cloud `Trusted Types` 安全合规**：
  - 消除后台标签页永久挂起的 `requestAnimationFrame` 依赖，采用 `MessageChannel` + MV3 Service Worker 非降频计时器 (`GEB_SW_SLEEP`) + `autoDiscardable: false` 防休眠保护，支持 Chrome 置于后台或最小化时静默收发；
  - 全仓零 `.innerHTML` 赋值，100% 兼容 Google Cloud 强制 `Trusted Types` (`require-trusted-types-for 'script'`) 安全策略。
- **结构化 JSON 输出 (`response_format`)**：支持 `{"type": "json_object"}` 与 `json_schema`，自动剥离网页端多余的 Markdown 代码块围栏并抑制参考链接尾注。

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:8000/v1", api_key="sk-local-bridge")

resp = client.chat.completions.create(
    model="gemini-4-argon",  # 或 "gemini-3.1-pro" / "gemini-3.8-flash"
    messages=[{"role": "user", "content": "查询香港天气并检索差旅政策"}],
    tools=[
        {
            "type": "function",
            "function": {
                "name": "get_weather",
                "description": "获取城市实时天气",
                "parameters": {
                    "type": "object",
                    "properties": {"city": {"type": "string"}},
                    "required": ["city"],
                },
            },
        }
    ],
)
print(resp.choices[0].message.tool_calls)
```

### 2.5 全类型多模态文件与媒体直传（图片 / 视频 / 音频 / PDF / Office / 代码文档）

原生兼容 OpenAI / Cursor / NextChat / Cherry Studio / Anthropic 风格的多模态消息结构（`image_url`、`input_audio`、`audio_url`、`video_url`、`file`、`input_file`、`document`、Data URL、Base64、HTTP(S) 链接、`gs://` URI 与本机文件路径）：

- **全模态文件覆盖与魔数自动嗅探 (`multimodal.py`)**：原生支持图片（PNG/JPEG/WebP/GIF/SVG 等）、视频（MP4/WebM/MOV/AVI/MKV 等）、音频（WAV/MP3/OGG/FLAC/AAC/M4A 等）、文档（PDF、Word `.docx`、Excel `.xlsx`、PPT `.pptx`）以及 Markdown / JSON / CSV / 代码源文件，自动通过文件头魔数与 OOXML ZIP 结构识别真实 MIME 类型。
- **本地受令牌保护的二进制暂存与分块中继 (`/api/bridge/attachments/{id}`)**：大体积音视频、高清图片与 PDF 无需塞入单帧 WebSocket JSON，由网关签发一次性受保护令牌暂存，MV3 Service Worker（或油猴 `GM_xmlhttpRequest`）拉取二进制流并分块重组为原生浏览器 `File` 对象，穿透多层 Shadow DOM 自动挂载至 Gemini Enterprise 输入框（优先 `<input type="file">`，支持拦截 `+` 附件菜单动态文件选择器及 `DataTransfer` 粘贴/拖拽兜底，全程零 `.innerHTML` 严格满足 `Trusted Types`）并等待上传进度完成后自动提交。
- **多轮对话附件增量去重 (`smart_incremental_delta`)**：同一会话后续追问自动通过 SHA-256 指纹识别历史轮次已上传过的图片/文档，**仅上传当前轮新增附件**；新建或切换会话（`reset_chat=True`）时自动完整附带所需文件。
- **官方 GCloud 模式同步适配 (`gcloud_provider.py`)**：Vertex AI Gemini 自动映射 `fileData`（`gs://`）、`inlineData`（图片/音频/视频/PDF）并对 Office/代码文本文件自动提取结构化正文；Discovery Engine `streamAssist` 自动内联文档与表格文本。

### 3. 多会话真并行工作池 + 后台自动扩容 (`WorkerSlot` Pool & Auto-Spawn)
不再受限于全局单会话串行锁——支持在多个客户端窗口、多个 Copilot / Cursor / Cline 智能体会话中**同时并发调用** Gemini Enterprise：
- **每标签页独立并发锁 (`WorkerSlot`)**：每个已打开的 `vertexaisearch.cloud.google.com` 标签页（包含不同 `/u/0/`、`/u/3/` 账号标签页或不同浏览器实例）均注册为独立并行工作槽位，多个会话可同时流式生成且互不阻塞。
- **按需后台静默扩容 (`BRIDGE_AUTO_SPAWN_TABS=true`)**：哪怕你最初只打开了 **1 个** Gemini Enterprise 标签页，当第 2、第 3 个并行会话到来时，MV3 扩展会在后台静默新建工作标签页（`active: false`，不抢占当前窗口焦点，默认上限 `BRIDGE_MAX_PARALLEL_TABS=4`，可在 `.env` 或扩展弹窗调整；也可随时点击扩展弹窗中的 **+ 新增并行标签页**）。
- **零配置历史前缀粘性路由 + 可选 `@会话名` 强绑定**：
  - **自动粘性路由**：网关自动比对各工作槽位的对话前缀（含多步 Tool Call 循环），确保会话 A 的追问精准回到标签页 #1、会话 B 的追问精准回到标签页 #2，互不覆盖、无需重置。
  - **显式会话绑定（可选）**：在客户端模型名后加上 `@任意标识`（如窗口 1 填 `gemini-4-argon@code`、窗口 2 填 `gemini-3.8-flash@doc`，或传入请求头 `X-Session-Id` / JSON 字段 `session_id`），即可为每个窗口永久分配专属工作标签页。

### 4. 三种页面执行模式（扩展弹窗可随时切换）
- **`Auto 混合模式`（默认推荐）**：通过深层 Shadow DOM 自动填入问题并触发发送，同时在 `MAIN` 世界被动监听 `StreamAssist` / `converse` 响应流，实时推送流式文本分片与企业知识库参考链接；若接口格式变动，自动无缝回退至 Shadow DOM 增量文本提取。
- **`DOM 纯组件模式`**：仅依赖网页 DOM 渲染增量与生成完成状态判定，兼容性最强。
- **`Replay 页面内重放模式`**：在网页正常产生过 1 次对话请求后，直接复用页面上下文内的 `fetch` 模板替换 `query.text` 重放请求。

### 5. 智能多轮对话与本地会话自动隔离 (`BRIDGE_CONTEXT_MODE` & `BRIDGE_AUTO_RESET_NEW_CHAT`)
- **`smart` + `BRIDGE_AUTO_RESET_NEW_CHAT=true`（默认开启）**：
  - **本地新建会话 -> 网页自动新建会话**：当你在 ChatBox / NextChat / Cursor 中点击新建对话发送第 1 条消息时（无需手动改选 `-newchat` 模型名），网关会自动识别并触发网页端开启全新会话（若网页本身已在空白新会话页则 0ms 秒级跳过）。
  - **同一本地会话多轮追问 / Agentic Loop -> 增量续聊 (`reset_chat=False`)**：同一会话内的第 2..N 轮提问或工具调用结果回传，网关严格保持在同一网页会话内**仅发送最新增量内容**，完整保留上下文与企业连接器。
  - **切换本地历史会话 -> 自动开新网页会话并打包上下文**：在客户端侧边栏切回另一条旧会话继续提问时，自动开启干净网页会话并注入该会话历史，彻底杜绝不同本地会话串台。
- 如希望始终在当前打开的固定网页对话中追加而不自动重置，可在 `.env` 中设 `BRIDGE_AUTO_RESET_NEW_CHAT=false`（此时仍可通过 `*-newchat` 模型名或 `[NEW_CHAT]` 前缀按需新建）。

### 6. GitHub CI/CD 自动发布与「浏览器插件 + 本地后端」双向自动更新

客户端扩展、油猴脚本与本地后端的更新包统一托管于 **GitHub Releases (`StevenLee1471717/Gemini-Enterprise-Bridge/releases/latest/download/...`)**，形成全链路闭环：

- **GitHub Actions 自动测试与打包发布 (`.github/workflows/ci-release.yml`)**：每次推送至 `main` 或打 `v*` 标签自动执行 `pytest`、`node test_page_driver_dom.mjs`、JS 语法检查及 `python extension_exporter.py --package`，并在版本号变更时自动发布含 `SHA-256` 校验清单（`release-manifest.json`）的 `gemini-enterprise-bridge-extension.zip`、`gemini-enterprise-bridge-backend.zip`、`gemini-enterprise-bridge.user.js` 与 `update-manifest.xml`。
- **本地后端每次启动自动检查更新 (`start.ps1` + `updater.py --startup-update`)**：运行 `start.bat` / `start.ps1` 时自动检查 GitHub 最新 Release（同时支持公开仓库与已登录 `gh` CLI / 配置 `BRIDGE_GITHUB_TOKEN` 的私有仓库），自动拉取更新并严格保留本地 `.env` 与 `usage_stats.json`（可通过 `start.ps1 -SkipUpdate` 或 `BRIDGE_AUTO_UPDATE=false` 关闭）。
- **浏览器扩展后台自动同步热重载 + 跨端版本协同提示**：
  - MV3 扩展定期轮询 GitHub Releases，发现新版本后自动请求同步 `extension/` 目录并在无进行中对话时调用 `chrome.runtime.reload()` 无感热重载；
  - 若**浏览器扩展版本高于本地后端**（或检测到 GitHub 新版），扩展图标自动显示琥珀色 `UPD` 角标，扩展弹窗与 `http://127.0.0.1:8000` 控制台顶部同步弹出醒目提示条，支持点击 **「立即自动更新并重启后端」**（经 `POST /api/update/apply` + 退出码 `42` 守护循环约 3 秒平滑热重启）；
  - 若**后端启动时率先完成自动升级**，WebSocket 握手后自动推送 `extension_reload_available` 通知浏览器扩展空闲热重载，保持两端版本严格一致。

### 7. 可选方案一：官方 Google Cloud CLI (`gcloud`) 模式
若后续获得 GCP 项目访问权限，无需修改客户端配置：
1. 安装 Google Cloud SDK 并执行：`gcloud auth application-default login`
2. 在 `.env` 中设置 `GCP_PROJECT_ID=你的项目ID`（如需直连 Agentspace 引擎可额外填写 `GCP_ENGINE_ID`）
3. 将 `BRIDGE_BACKEND_MODE` 设为 `gcloud`（或 `auto`），或在客户端模型名中选择 `gemini-enterprise-gcloud`。

---

## 运行自动化测试

```powershell
.\start.ps1 -Test
# 或直接使用 pytest：
pytest -v
```

测试套件 (`test_bridge.py`) 完整覆盖：
- `/health`、`/api/status`、`/v1/models` 与 API Key 鉴权校验；
- 网页模型直选与简写别名解析（`3.1pro`、`4argon`、`4carbon`、`3.8flash`、`*-newchat`、`gemini-web:*`）及向浏览器端下发验证；
- OpenAI Tool Calling 非流式与 SSE 流式分片解析（含并行工具控制、强制函数调用、零标签泄漏验证）；
- 多轮 Agentic Loop 同会话增量回传（`smart_incremental_delta` + `reset_chat=False`）与 `json_object` 模式校验；
- 模拟浏览器 WebSocket 扩展与 Tampermonkey HTTP 长轮询桥接及并发请求 FIFO 串行锁验证；
- 纯 Python 动态生成的 `16x16` / `48x48` / `128x128` PNG 图标尺寸与 `extension/` 打包校验。

---

## 目录结构

```text
I:\Projects\Gemini-Enterprise-Bridge\
├── .github/workflows/ci-release.yml  # GitHub Actions 自动化测试与 Release 产物发布流水线
├── version.py                        # 全局单一事实源版本号与 GitHub Releases 端点常量
├── updater.py                        # 零外部依赖 GitHub Releases 自动更新器（SHA-256 校验 + 配置保护）
├── sync_client_limits.py             # 自动同步 1M/65K Token 上限至本地 Copilot BYOK 数据库
├── server.py                         # FastAPI 主入口（支持 python server.py 直接运行）
├── config.py                         # 环境变量与 .env 配置加载
├── multimodal.py                     # 多模态文件/音视频/图片/Office 文档归一化、嗅探与文本提取器
├── schemas.py                        # OpenAI 兼容模型、多模态消息、工具参数与网页模型别名解析器
├── tool_calling.py                   # DOM 免疫工具调用协议、多级容错解析与 SSE 流式拦截器
├── context_builder.py                # 多轮对话与 Agentic Loop 智能增量上下文构建器 + 实时窗口水位监控
├── usage_stats.py                    # 官方 Google Gemini API 定价引擎与持久化用量等价账本
├── bridge_hub.py                     # WebSocket + HTTP 双通道桥接中心与多标签页并行池
├── gcloud_provider.py                # 方案一：官方 GCloud CLI / Vertex AI & Agentspace 上游
├── dashboard_html.py                 # http://127.0.0.1:8000/ 本地可视化控制台与模型测试台
├── extension_exporter.py             # 纯标准库 PNG 图标渲染与独立 extension/ 导出器
├── manifest.json                     # Chrome/Edge Manifest V3 扩展清单（最小权限集）
├── background.js                     # MV3 Service Worker（持久化存储 + 自动重连 WebSocket）
├── content-bridge.js                 # ISOLATED 世界安全消息中继
├── page-driver.js                    # MAIN 世界混合驱动（自动模型切换 + 流嗅探 + 深度 Shadow DOM）
├── popup.html / popup.css / popup.js # 扩展可视化状态弹窗、标签页绑定与页面元素诊断
├── gemini-enterprise-bridge.user.js  # CSP 免疫版 Tampermonkey 油猴备选脚本（含自动模型切换）
├── test_bridge.py                    # Pytest 自动化端到端测试套件
├── requirements.txt / pyproject.toml # Python 依赖声明
├── env.example                       # 环境变量配置模板
├── start.ps1 / start.bat             # Windows 一键初始化、测试与启动脚本
└── CHROMEWEBSTORE.md                 # 扩展权限说明与隐私披露文档
```

---

## 常见问题排查 (Troubleshooting)

1. **客户端提示 `503 未检测到挂载的 Gemini Enterprise 浏览器页面`**：
   - 请确认本地已启动 `start.bat` / `python server.py`；
   - 确认 Chrome/Edge 已加载扩展且已打开 `https://vertexaisearch.cloud.google.com/...` 页面；
   - 点击扩展图标 -> **保存并重连**，或访问 `http://127.0.0.1:8000/` 查看实时连接状态。
2. **网页改版导致找不到输入框或回复容器**：
   - 在 Gemini Enterprise 页面点击扩展弹窗中的 **检测页面元素** 查看自动识别结果；
   - 若企业定制版更换了标签名，可在弹窗 **高级：自定义 Shadow DOM 选择器** 中填入对应的 CSS 选择器（驱动会自动穿透所有层级的 Shadow Root 查找该选择器），无需修改代码即可热更新。

## Codex Responses 接入、多模态增强与稳定性加固

### 1. OpenAI Responses API 与会话持久存储
- **Responses 端点**：新增 `POST /v1/responses`（流式 SSE 与非流式）、`GET/DELETE /v1/responses/{id}`、`POST /v1/responses/input_tokens`、`POST /v1/responses/compact`，支持命名空间工具、自由格式 `apply_patch`、图片输入与内置生图调用。
- **本地 SQLite 持久存储**：`previous_response_id` 与压缩检查点默认保存在 `.cache/response_store/responses.sqlite3`（权限 `0600`，单条上限 32 MB、总有效负载上限 512 MB；普通回复默认保留 24 小时，`bridge_compact_*` 压缩检查点跨重启保留；`store: false` 不落盘）。
- **Codex 配置同步**：内置 `codex-models.json`（含 `input_modalities: ["text", "image"]`、1,048,576 上下文窗口与 524,288 整理门槛，参考 Google Gemini CLI 50% 策略，非 Google 官方公开的网页端固定压缩阈值）与 `configure_codex_context.py`（默认仅预览，加 `--apply --backup-dir <备份目录>` 后更新本地 Gemini 条目，不修改其他模型供应商）。

### 2. 多模态文件存取、自动转码与媒体生成
- **文件与媒体接口**：新增 `POST/GET /v1/files`、`GET /v1/files/{id}`、`GET /v1/files/{id}/content`、`DELETE /v1/files/{id}`、`POST /v1/images/generations`、`POST /v1/images/edits`、`GET /v1/images/files/{filename}`、`POST /v1/videos/generations`、`POST /v1/audio/transcriptions`、`POST /v1/audio/translations`，并提供零外部依赖的 stdio MCP 工具 `imagegen_mcp.py`（`generate_image`、`generate_video`、`analyze_media`）。
- **本机自动兼容转换**：针对网页输入框限制，当本机存在 `sips` / `afconvert` / `textutil` / `ffmpeg` 时，自动将 `HEIC/HEIF/TIFF/BMP/ICO/AVIF/JP2` 转为 `PNG`、`WAV/AIFF/FLAC/OGG/OPUS/AAC/WEBM/AMR` 转为 `M4A`、`MOV/WEBM/MKV/AVI/MPEG/3GP/FLV` 转为 `MP4`、`RTF` 转为 `DOCX`；超过 1 MB 的附件自动走本地暂存拉取与分片传输。
- **已知边界**：网页内部文档卡片（如网页内生成的 PDF/Word 预览卡）无公开下载流，亦不提供原生 TTS 语音合成导出；非原生格式转换依赖宿主机对应系统工具或 `ffmpeg`。

### 3. 流式工具完整性与多标签页并发加固
- **工具与思考草稿防护**：状态机解析 `[TOOL_CALL]...[/TOOL_CALL]`（忽略参数字符串字面量内的同名标记），拦截中英文明确内部思考草稿标题（如 `Thinking Process:`、`思考过程：`）及未闭合工具块并触发单次修复；剥离网页 `[[section:...]]` 与 `[[suggested_questions]]` 装饰块后再做末尾 `summaryText` 快照去重（`page-driver.js` `v2.6.5`）。
- **全局并发信号量与保活**：`BrowserBridgeHub` 强制执行 `max_parallel_tabs` 全局信号量硬上限，取消/异常时自动停止页面生成并清理槽位增量历史，长耗时工具与媒体生成全程发送 SSE `: keepalive`。
