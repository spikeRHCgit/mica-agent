# Phase 6：配置与模型

## 目标

用户能在 TUI 里登录多个供应商、发现和切换模型、调整共享设置并在多窗口生效，配置可靠保存；三类接口（Chat Completions、Responses、Messages）和 ChatGPT 官方订阅都能正常推理并保留续接数据。

## 交付项与验收

| 交付项 | 目标 | 验收方式 |
|---|---|---|
| A2 · 会话内模型菜单与切换 | 空闲时用 `/model` 打开菜单、筛选、切换并保存会话选择；切换到更小上下文时警告并确认 | 用户在 Windows Terminal 验收 `/` 候选和 `/model` 菜单 |
| B · Responses 与 Messages 接口 | 三类接口各有独立适配器，推理、工具、思考和取消正常工作；跨接口切换时保留通用对话、丢弃不兼容续接 | 用户用三种接口各跑一条真实渠道的工具对话，并跨接口切换一次 |
| C1 · API Key 登录与目录 | `/login` 输入 URL/Key 后自动发现模型并保存；OpenRouter、DeepSeek、OpenCode Go 三个渠道正常登录 | 用户验收三个渠道的登录和目录 |
| C2 · ChatGPT 官方订阅 | 浏览器授权、token 刷新、推理和工具调用；本地回调服务器有界运行；ID token 经过签名验证 | 用户用真实 ChatGPT 授权并跑一次工具对话；账号或额度不足时标为未验收 |
| D · 设置、重载与多窗口 | `/settings` 修改三项共享设置并保存到工作区或全局；`/reload` 重读配置；运行中快捷键切换权限时标记待生效，Run 收口后保存；忙时编辑命令草稿会提醒 | 用户验收设置、重载和忙时提醒 |

主线收口时，运行 `uv run pytest -q -W error`、`ruff check`、`ruff format --check`；依赖变化后运行 `uv lock --check` 和 `uv build`。

---

## A2 · 会话内模型菜单与切换

### 要实现

**命令候选**：
- 单行草稿以 `/` 开头时，即时弹出可用命令候选列表，支持 ↑/↓ 选择及继续输入筛选；
- 只有候选目录中的命令能在本地执行；未匹配内容提交时完整作为普通消息发送，不被本地拦截；
- 忙时可以编辑命令草稿，提交时提醒「运行中，命令暂不可用」；
- 命令候选与可编辑 composer 共存，进入配置功能后才替换底部区。

**模型菜单**：
- 空闲时输入 `/model` 并提交，打开模型选择菜单；
- 菜单显示当前供应商的所有可用模型，按名称排序；支持 ↑/↓ 导航、继续输入筛选；
- Enter 选中目标模型，弹出确认：显示新模型名称、上下文窗口大小；如果新模型的 `contextWindow` 小于当前模型，警告「上下文会缩小，历史可能超限」；
- 确认后切换连接、保存会话选择、关闭旧连接；菜单关闭并回到空闲；
- Esc 只关闭菜单，不取消 Run；菜单打开时屏蔽普通输入。

**容量提醒**：
- 只比较新旧模型的 `contextWindow` 字段，不估算历史 token；
- 真正超限由服务端返回错误，前端正常展示。

**会话选择保存**：
- 切换成功后，更新当前会话的 `provider_id` 和 `model_id`；
- 以后恢复该会话时使用保存的选择，不随全局默认模型变更；
- 新建会话时，如果实际采用了有效配置默认模型（未使用 CLI 临时覆盖），保存该初始选择。

### 实现方式

**模块与文件**：
- 新建 `src/mica_agent/tui/main_buffer/commands.py`：固定命令登记（名称、说明、是否可用、处理函数）、匹配和分派；候选列表和实际执行用同一份登记；
- 新建 `src/mica_agent/tui/main_buffer/menus.py`：本地菜单的状态、筛选和绘制，复用 `interactions.py` 的 `PanelView` 和 `fit_panel_rows`；本地菜单与 Ask/Permission 语义分开，Esc 只关菜单；
- `src/mica_agent/tui/main_buffer/screen.py`：调整 running 状态的输入门禁，让忙时可以编辑草稿；调用 commands 的候选和分派；
- 扩充 `src/mica_agent/app.py`：Provider 创建从 CLI 挪到 App，新增空闲切换接口，顺序为校验 → 准备新连接 → 保存会话选择 → 安装 → 关闭旧连接；准备或保存失败关闭候选、保留旧连接；
- 扩充 `src/mica_agent/session_service.py`：新增更新会话选择的方法，复用 `JsonlSessionStore.update` 的锁内变换。

**关键接口**：
- `commands.py` 提供：命令登记结构、`match_command(input: str)`、`suggest_commands(prefix: str)` → 候选列表、`dispatch_command(name: str, args: str, ctx)`；
- `menus.py` 提供：模型菜单状态、筛选、按键处理和绘制；
- `MicaApp` 新增：`switch_model(provider_id: str, model_id: str)` → 成功或失败及原因；
- `SessionService` 新增：`update_selection(session_id: str, provider_id: str, model_id: str)`。

**数据流**：
- screen 收到 `/` 开头的草稿 → 调用 commands 派生候选 → 渲染候选列表；
- Enter 提交 → commands 匹配并分派 → `/model` 打开 menus 的模型菜单；
- 菜单确认 → 调用 `App.switch_model()` → App 准备连接 → SessionService 保存选择 → App 安装并关闭旧连接 → 菜单关闭。

**注意事项**：
- 命令登记只包含 `/model`，`/login`、`/settings`、`/reload` 由后续交付项注册；
- 不引入命令插件框架，不为未实现命令创建空壳入口；
- 历史 token 估算和精确用量统计随阶段 7 一起做，A2 不提前实现。

---

## B · Responses 与 Messages 接口

### 要实现

**新增两个接口适配器**：
- 支持 OpenAI Responses API：`/chat/responses` 端点、reasoning item 续接、thinking 展示；
- 支持 Anthropic Messages API：`/messages` 端点、thinking block 续接、extended_thinking 展示。

**续接数据保留**：
- Responses 的 reasoning item（type、content、summary、encrypted_content 等）和 Messages 的 thinking signature（type、thinking、signature）在工具往返和会话恢复时回传给模型；
- 续接数据经过清洗判定：长度上限、绑定供应商+模型+接口+步骤，通过判定的才保存到会话；
- TUI 只接收可展示的思考文字（`ReasoningDelta`），不读取专属原文。

**跨接口切换**：
- 切换到不同接口的模型时，保留通用对话历史（`Message` 的 role/content/tool_calls/tool_call_id）；
- 丢弃不兼容的续接数据，不强行转换或保留来源不匹配的记录；
- 切换后第一次推理正常工作，不因缺少续接而失败。

**会话格式升级**：
- 新增 v6：v1～v5 继续只读可用，读入时续接为空；
- 第一次业务写入时在锁内整文件升级到 v6，和 v4、v5 的升级方式一致；
- 旧版 Mica 无法读取 v6 会话。

### 实现方式

**模块与文件**：
- 新建 `src/mica_agent/providers/openai_responses.py`：Responses 请求编码和 SSE 解析，用 httpx；reasoning item 解析和续接数据构造；
- 新建 `src/mica_agent/providers/anthropic_messages.py`：Messages 请求编码和 SSE 解析，用 httpx；thinking block 解析和续接数据构造；
- 新建 `src/mica_agent/continuation_contracts.py`（公共契约组）：独立的模型步骤续接记录契约，强类型、有长度上限，绑定供应商+模型+接口+步骤；
- 扩充 `src/mica_agent/providers/base.py`：`ProviderRequest` 新增续接字段、`Finish` 返回续接；三个适配器共用的 SSE 读取和错误分类，只在确实重复时抽到这里；
- 扩充 `src/mica_agent/engine.py`：在本轮工具往返中携带续接；
- 扩充 `src/mica_agent/app.py`：按模型的 `api` 字段从一张固定表里选适配器（completions / responses / messages），创建对应 Provider；`prepare_run()` 装配时传入 factory；
- 扩充 `src/mica_agent/session_contracts.py`：v6 格式定义、续接字段的序列化和反序列化；
- 扩充 `src/mica_agent/checkpoint_contracts.py`：`RunCheckpointProgress` 新增续接字段；
- 扩充 `src/mica_agent/jsonl_store.py`：v6 读取、升级和写入；
- 扩充 `src/mica_agent/session_service.py`：恢复时返回消息加续接的上下文视图；
- 扩充 `src/mica_agent/message_sanitizer.py`：续接数据的安全判定和清洗。

**续接契约结构**（示意）：
```python
@dataclass(frozen=True)
class ContinuationRecord:
    provider_id: str
    model_id: str
    api_type: str  # "completions" | "responses" | "messages"
    step_index: int
    data: dict[str, object]  # 强类型白名单，有长度上限
```

**数据流**：
- Engine 请求模型 → ProviderRequest 携带上一步的续接 → 适配器编码专属字段并发送；
- SSE 返回 → 适配器解析专属字段 → Finish 携带本步续接 → Engine 保存到本轮上下文；
- Turn/Checkpoint 提交 → 经 sanitizer 清洗判定 → 只保存通过的记录；
- 恢复会话 → SessionService 投影消息加续接 → Engine 下一步携带。

**接口映射表**（App 装配时使用）：
- `"openai-completions"` → `OpenAICompatibleProvider`
- `"openai-responses"` → `OpenAIResponsesProvider`
- `"anthropic-messages"` → `AnthropicMessagesProvider`

**注意事项**：
- 不先改已验收的 Chat Completions 适配器，三个适配器独立实现；
- 续接数据不改变 `Message`/`ToolCall` 的公共 API；
- 纯思考、无正文的模型步骤也能保存续接，不受「中间消息只允许工具往返」约束；
- 跨接口切换的兼容性测试要覆盖 completions ↔ responses ↔ messages 各方向。

---

## C1 · API Key 登录与目录

### 要实现

**登录流程**：
- `/login` 命令打开 API Key 登录界面，提示输入 API URL 和 API Key；
- Key 输入用遮蔽模式（显示为 `*`），原始 Key 只在提交时交给认证业务，不进入 composer、用户事件或调试 repr；
- 提交后校验 URL 格式，用 Key 请求模型目录（有界、可取消）；
- 目录请求成功 → 解析模型列表 → 保存连接到 `auth.json` 和 `models.json` → 提示「已保存 N 个模型」→ 关闭界面；
- 目录请求失败 → 显示错误（认证失败 / 超时 / 服务端错误）→ 保留输入，用户可重试或取消。

**目录发现**：
- 按渠道请求模型列表：OpenRouter (`/models`)、DeepSeek (`/models`)、OpenCode Go (`/models`)；
- 解析出模型 ID、名称和明确的能力（`contextWindow`、是否支持 tool calling、是否支持 reasoning）；
- 有界请求：超时 30 秒、最多重试 1 次；
- 内置渠道的基址、目录地址和接口映射写成固定表。

**凭据安全保存**：
- `auth.json`：先创建目录和文件并设置权限（Windows ACL / POSIX 0600），再写明文，最后原子替换；
- 锁内重读、合并其他供应商的连接、原子保存；
- 保存后立即用受保护读取验证权限。

**模型保存**：
- `models.json`：和 `config.json` 共用一个内部原子替换步骤（mkstemp、flush/fsync、os.replace）；
- 锁内重读、合并、原子保存。

**渠道支持**：
- OpenRouter：Chat Completions 接口，基址 `https://openrouter.ai/api/v1`，目录 `/models`；
- DeepSeek：Chat Completions 接口，基址 `https://api.deepseek.com`，目录 `/models`；
- OpenCode Go：Chat Completions 接口，基址 `https://api.opencode.dev/v1`，目录 `/models`。

### 实现方式

**模块与文件**：
- 新建 `src/mica_agent/credential_store.py`（配置与选择组）：`auth.json` 的安全创建、锁内重读合并、原子保存、删除和刷新共用归属锁；
- 新建 `src/mica_agent/model_catalog.py`（配置与选择组）：按渠道用 httpx 请求模型列表，解析 ID、名称和能力，有界、可取消；内置渠道的固定表；
- 扩充 `src/mica_agent/config_store.py`：`models.json` 的写入，和 `config.json` 共用内部原子替换步骤；
- 扩充 `src/mica_agent/tui/main_buffer/commands.py`：注册 `/login` 命令；
- 扩充 `src/mica_agent/tui/main_buffer/menus.py`：遮蔽 Key 输入组件、多行输入（URL + Key）、提交和取消。

**关键接口**：
- `CredentialStore`：
  - `__init__(data_root)`
  - `save_connection(provider_id, api_url, api_key, models)` → 成功或失败
  - `remove_connection(provider_id)` → 成功或失败
- `ModelCatalog`：
  - `discover(api_url, api_key, channel_hint)` → `list[ModelInfo]` 或错误
  - `ModelInfo`：id、name、contextWindow、supportsTools、supportsReasoning
- `ConfigStore` 新增：
  - `save_models(provider_id, models)` → 成功或失败

**凭据安全流程**：
1. 创建 `~/.mica/auth.json` 的父目录（如果不存在）；
2. 用 mkstemp 创建临时文件，立即设置权限（Windows：只允许当前用户和 SYSTEM/Administrators；POSIX：0600）；
3. 写入 JSON 明文（包含 URL 和 Key）；
4. flush + fsync；
5. os.replace 原子替换；
6. 删除临时文件（失败时记录但不阻塞）；
7. 用 `_credential_permissions.py` 的受保护读取验证权限。

**目录请求示例**（OpenRouter）：
```
GET https://openrouter.ai/api/v1/models
Authorization: Bearer <api_key>
→ { "data": [{"id": "...", "name": "...", "context_length": 128000, ...}] }
```

**注意事项**：
- 目录格式按渠道区分，不按协议（OpenRouter 和 DeepSeek 都用 Chat Completions，目录格式不同）；
- 目录请求和推理请求分开，不改 Provider 协议；
- C2 的 OAuth 整组刷新也用 `CredentialStore`。

---

## C2 · ChatGPT 官方订阅

### 要实现

**授权流程**：
- `/login` 命令增加「ChatGPT 官方订阅」选项；
- 选中后，用浏览器打开 ChatGPT 授权页面，用户在浏览器里登录并授权；
- 本地回调服务器接收授权码，用授权码交换 access token 和 refresh token；
- 验证 ID token 的签名（用 `PyJWT` 和 OpenAI JWKS）；
- 保存 token 到 `auth.json`，提示「ChatGPT 已授权」→ 关闭界面。

**Token 刷新**：
- 推理前检查 access token 是否过期（根据 `expires_at`）；
- 过期时用 refresh token 换新的 access token，更新 `auth.json`；
- refresh token 也过期时，提示「授权已过期，请重新登录」。

**推理**：
- 复用 B 的 Responses 适配器，由连接装配时传入订阅选项：
  - 认证头用 OAuth token（`Authorization: Bearer <access_token>`）；
  - 发送 `store: false`（不保存对话）；
  - 不发送 `max_output_tokens`；
  - 系统指引转成 `instructions` 字段；
  - 工具用订阅允许的形式。

**本地回调服务器**：
- 用标准库 `ThreadingHTTPServer`，在受控线程里运行；
- 只绑 `127.0.0.1`，端口动态分配；
- 有超时（120 秒）和取消；
- 结果安全交回事件循环；
- 关闭后迟到的请求直接拒绝。

**安全**：
- state、nonce、PKCE 用标准库生成（`secrets.token_urlsafe`、`hashlib.sha256`）；
- 校验回调的 state、nonce；
- 授权码不进入日志或 UI；
- ID token 签名验证用 `PyJWT[crypto]`。

### 实现方式

**模块与文件**：
- 新建 `src/mica_agent/chatgpt_auth.py`（配置与选择组）：管授权流程、token 交换和刷新；本地回调服务器；state、nonce、PKCE 生成；
- 引入运行依赖 `PyJWT[crypto]`（MIT；后端 cryptography 为 Apache-2.0 或 BSD）；
- 扩充 `src/mica_agent/providers/openai_responses.py`：接受订阅选项（OAuth 认证、store、instructions、工具形式）；
- 扩充 `src/mica_agent/credential_store.py`：OAuth 整组（access token、refresh token、expires_at）的保存和刷新；
- 扩充 `src/mica_agent/config_service.py`：识别 `type: "chatgpt_subscription"` 并装配订阅连接；
- 扩充 `src/mica_agent/tui/main_buffer/menus.py`：登录方式选择（API Key / ChatGPT 订阅）。

**关键接口**：
- `ChatGPTAuth`：
  - `start_authorization()` → 浏览器打开授权页面，返回回调服务器地址
  - `wait_for_callback(timeout)` → 授权码或超时/取消
  - `exchange_code(code)` → access token、refresh token、expires_at
  - `refresh_access_token(refresh_token)` → 新 access token、expires_at
  - `verify_id_token(id_token)` → 验证签名并返回 claims

**授权流程**：
1. 生成 state、nonce、PKCE code_verifier 和 code_challenge；
2. 启动本地回调服务器（127.0.0.1:动态端口）；
3. 用 `webbrowser.open_new_tab()` 打开授权 URL（包含 state、nonce、code_challenge、redirect_uri）；
4. 等待回调（超时 120 秒）；
5. 校验 state、nonce；
6. 用授权码 + code_verifier 换 token；
7. 验证 ID token 签名；
8. 保存到 `auth.json`；
9. 关闭回调服务器。

**依赖审查**（派卡前由 master 核实）：
- `PyJWT` 当前版本和许可证（MIT）；
- `cryptography` 当前版本和许可证（Apache-2.0 或 BSD-3-Clause）；
- 传递依赖的许可证；
- 添加到 `pyproject.toml` 的 `dependencies`。

**注意事项**：
- 只维护一套 Responses 编解码，订阅和 API Key 共用；
- 回调服务器只接收一个请求就关闭；
- 真实 ChatGPT 授权和推理由用户验收，账号或额度不足时标为未验收。

---

## D · 设置、重载与多窗口

### 要实现

**设置菜单**：
- `/settings` 命令打开设置菜单，显示三项共享设置：
  - `permissionMode`：default / solo；
  - `reasoningEffort`：off / low / medium / high；
  - `reasoningDisplay`：full / collapsed / hidden。
- 支持 ↑/↓ 导航、Enter 切换选项；
- 每项显示当前值、来源（program default / global / workspace / CLI override）；
- 底部显示「Save to: Workspace / Global / Cancel」，↑/↓ 选择保存目标；
- 确认后保存到对应配置文件，提示「已保存到 <workspace/global>」→ 关闭菜单。

**配置重载**：
- `/reload` 命令重读全局和工作区配置；
- 重读成功 → 提示「配置已重载」→ 下一轮 Run 使用新值；
- 重读失败 → 显示错误（JSON 格式错误 / 字段非法 / 凭据权限错误）→ 阻止新 Run，保持当前配置。

**待生效机制**：
- 运行中用快捷键切换权限模式时，只记待生效值，不立即保存；
- 等消费任务结束、检查点提交、App 忙状态释放后再保存；
- 下一轮 `prepare_run()` 读到新值。

**忙时提醒**：
- 运行中编辑命令草稿并提交时，提醒「运行中，命令暂不可用」；
- 提醒持续 2 秒后自动消失，保留草稿。

**多窗口生效**：
- 一个窗口修改全局配置 → 另一个窗口下一轮 Run 时读到新值；
- 一个窗口修改工作区配置 → 同工作区的另一个窗口下一轮 Run 时读到新值；
- 不实时同步：只在 Run 开始时重读配置。

**CLI override 撤销**：
- 当前窗口用户在设置菜单修改某项 → 撤销该项的 CLI override；
- 其他窗口的文件写入 → 不撤销本窗口的 CLI override。

### 实现方式

**模块与文件**：
- 扩充 `src/mica_agent/app.py`：持有待生效的设置意图；在 Run 收口后、reservation 释放时保存；
- 扩充 `src/mica_agent/config_store.py`：新增按目标项比较保存工作区和全局设置的方法（`save_setting(name, value, workspace, expected_value)`）；
- 扩充 `src/mica_agent/config_service.py`：`reload()` 方法，重读配置并返回新快照或错误；
- 扩充 `src/mica_agent/tui/main_buffer/commands.py`：注册 `/settings`、`/reload` 命令；
- 扩充 `src/mica_agent/tui/main_buffer/menus.py`：设置菜单组件（三项设置、保存目标选择）；
- 扩充 `src/mica_agent/tui/main_buffer/screen.py`：快捷键切换权限时更新 App 的待生效意图；忙时提醒组件。

**关键接口**：
- `MicaApp` 新增：
  - `mark_pending_setting(name, value)` → 记待生效值
  - `commit_pending_settings()` → 保存所有待生效值
- `ConfigStore` 新增：
  - `save_setting(name, value, workspace, expected_value)` → 成功或冲突
- `ConfigService` 新增：
  - `reload()` → `ConfigSnapshot` 或错误

**数据流**：
- 用户打开 `/settings` → menus 渲染三项设置 → 确认保存目标 → 调用 `ConfigStore.save_setting()` → 成功或冲突；
- 用户输入 `/reload` → ConfigService 重读配置 → 成功返回新快照 / 失败阻止新 Run；
- 运行中快捷键切换权限 → 调用 `App.mark_pending_setting()` → Run 收口后调用 `App.commit_pending_settings()` → ConfigStore 保存。

**reasoningDisplay 即时更新**：
- 不走待生效机制；
- 设置菜单确认后，立即更新 screen 的 `_reasoning_mode`；
- 下一次重绘时按新模式渲染思考块。

**注意事项**：
- 不实现文件监听和实时同步，只在 Run 开始时重读；
- Shift+Tab 快捷键用于「切换到下一个交互项」，不用于设置相关操作；
- `/reload` 失败时的阻止机制：在 App 的 `prepare_run()` 入口检查配置有效性。

---

## 不做清单

Phase 6 **不实现**以下内容：

1. 整体架构重构（D-014）；
2. 阶段 7 的会话命令（`/new`、`/rename`、`/branch`、`/rewind`、`/clear`、`/btw`、`/compact`）；
3. 图片、文件等多模态输入；
4. 多 Provider 自动路由和故障转移；
5. 配置文件监听和实时同步；
6. 额外的推理连接测试（登录时只请求目录，不单独测试推理）；
7. 内置工作模式；
8. 历史 token 估算和上下文用量统计（阶段 7）；
9. 命令插件框架；
10. 未实现命令的空壳入口。

---

## 依赖变更

新增运行依赖：
- `pyjwt[crypto]`（MIT；后端 cryptography 为 Apache-2.0 或 BSD-3-Clause）

派 C2 的卡之前，master 先核实当前版本、许可证和传递依赖。

---

## 数据格式变更

**会话 JSONL 升级到 v6**：
- 新增字段：续接数据（`continuation`，可选，仅 v6）；
- v1～v5 继续只读可用，读入时续接为空；
- 第一次业务写入时在锁内整文件升级到 v6；
- 旧版 Mica 无法读取 v6 会话。

**新增配置文件**：
- `~/.mica/models.json`：保存各供应商的模型列表；
- `~/.mica/auth.json`：保存凭据（API Key 和 OAuth token），受保护权限。

---

## 测试要求

每张卡通过时，必须满足：
- 全量测试通过：`uv run pytest -q -W error`；
- 代码检查通过：`ruff check`、`ruff format --check`；
- 依赖变化后：`uv lock --check`、`uv build` 通过；
- 新增公共契约、wire 格式变更：有直接断言和安装态验证；
- 凭据安全：真实 Windows ACL / POSIX 权限验证。

每项交付完成后，用户做真实验收（见交付项表格）。

---

## 参考

- D-017：Phase 6 配置与多接口方案定稿
- D-020：启动模型覆盖不写回会话
- D-021：新会话保存配置默认模型的初始选择
- D-022：配置启动与会话内模型切换分开验收
- D-023：斜杠命令统一管理、功能分批交付
- D-024：即时命令候选与未匹配原文回退
- T-023：Phase 6 剩余交付项的代码事实调研
- `docs/scratchpad.md`：规划讨论记录
- `docs/configuration_spec.md`：配置行为规则（计划写成后删除）
