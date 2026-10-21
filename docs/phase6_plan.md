# Phase 6：配置与模型

## 目标

用户能在 TUI 里登录多个供应商、发现和切换模型、调整共享设置并在多窗口生效，配置可靠保存；三类接口（Chat Completions、Responses、Messages）和 ChatGPT 官方订阅都能正常推理并保留续接数据。

## 交付项与验收

| 交付项 | 目标 | 验收方式 |
|---|---|---|
| A1 · 配置文件与启动选择 | 三份 JSON 配置的读取、来源、安全保存，未配置启动，会话选择保存与恢复 | 已由 T-021 交付 |
| A2 · 统一命令入口与模型切换 | `/` 即时候选；空闲时用 `/model` 切换当前模型或保存全局默认；切换到更小上下文时警告并确认 | 完成（T-024、T-025；2026-10-10 用户 Windows Terminal 冒烟验收通过） |
| B · Responses 与 Messages 接口 | 三类接口各有独立适配器，推理、工具、思考和取消正常工作；跨接口切换时保留通用对话、丢弃不兼容续接 | 用户用三种接口各跑一条真实渠道的工具对话，并跨接口切换一次 |
| C1 · API Key 登录、目录与连接维护 | `/login` 添加内置或自定义 API Key 连接，自动发现目录并保存勾选模型；已有连接可维护 | 用户验收 OpenRouter、DeepSeek、OpenCode Go 的登录和目录 |
| C2 · ChatGPT 官方订阅 | 浏览器授权、身份验证、token 刷新、推理和工具调用 | 用户用真实 ChatGPT 授权并跑一次工具对话；账号或额度不足时标为未验收 |
| D · 设置、重载与多窗口 | `/settings` 修改三项共享设置；`/reload` 重读配置；运行中快捷切换权限待 Run 收口后保存；其他窗口按时机读到新值 | 用户验收设置、重载、忙时提醒和双窗口生效 |

交付顺序：A2 → B → C1 → C2 → D。每项在最后一张卡通过后由用户做真实验收。主线收口时，运行 `uv run pytest -q -W error`、`uv run ruff check src/mica_agent tests`、`uv run ruff format --check src/mica_agent tests`；依赖变化后运行 `uv lock --check` 和 `uv build`。

---

## 通用规则（各交付项共同遵守）

**渠道与接口**：
- 渠道只包括 ChatGPT 官方订阅、OpenRouter、DeepSeek、OpenCode Go 和自定义供应商，支持多个连接与同名模型，不扩充大量预置渠道；
- 接口为 `openai-completions`、`openai-responses`、`anthropic-messages`；模型可覆盖供应商默认接口；供应商与模型 ID 决定能力，接口 ID 决定适配器；
- 取消内置工作模式及 `workMode` 字段。

**配置文件**（A1 已落地的读取规则不变）：
- 全局三份 JSON：`config.json`（默认模型和三项共享设置）、`models.json`（按稳定供应商 ID 索引的连接与模型）、`auth.json`（按供应商 ID 保存的 API Key 或 OAuth 凭据）；工作区只有 `<workspace>/.mica/config.json`，禁止存凭据；
- 不增加配置 `version` 或版本迁移；唯一配置入口负责读取、校验、合并、来源说明和保存，CLI、应用和菜单不各自读 JSON 或环境变量；
- 能力缺失时按默认补齐并在界面标注是默认值，不把未知能力伪装成发现结果；`maxTokens` 是输出预算，不是上下文长度；
- 保存前在锁内重读最新文件，只改目标项、保留无关字段；损坏文件不自动覆盖，写失败保留原字节；多文件分步提交时，所需记录全部有效后才标记连接可用，默认值和运行状态不领先于保存结果；并发编辑不能无提示覆盖其他窗口刚保存的项。

**菜单与输入所有权**（呈现细节见 `docs/tui_design.md` §13.1）：
- 四个命令 `/login`、`/model`、`/settings`、`/reload`，不保留 `/config` 别名；
- 已识别的本地命令只在空闲执行；Ask/Permission 属于忙状态；忙时完整命令提交只在输入区给短暂灰色提醒，不排队、不取消 Run、不发给模型；
- 配置菜单取代整个 composer/facts/working 区，上方历史仍可见；关闭后恢复草稿、光标和事实信息；配置菜单、Ask、Permission、普通编辑互斥；配置菜单 Esc 只关闭，Ask/Permission 的 Esc 仍取消 Run；
- 目录请求和登录等待不是 Agent Run，可取消；操作绑定菜单生命周期，关闭或更换后丢弃迟到结果，由创建者收口资源；网络等待不卡住键盘、动画或退出；
- 事实区保持既有布局，不增加未经确认的字段。

**凭据安全**：
- Key 输入遮蔽，不进入 editor 历史、用户消息、用户事件、调试 repr、日志或诊断；菜单只显示安全标签和状态，不显示 token；
- `auth.json` 用实际可验证的用户权限保护（Windows 查 ACL，不以 chmod 代替；POSIX 0600）；写入原子完成，失败不截断旧文件。

---

## A2 · 统一命令入口与模型切换

### 要实现

**统一命令入口**：
- 斜杠命令的识别、登记、可用性判断和分派统一管理；各命令只承接自己的菜单或业务操作；分批交付的命令不复制输入入口或运行状态判断；
- 只把已实现的命令列为候选，不提前提供空实现。

**命令候选**：
- 单行草稿以 `/` 开头时即时弹出候选列表；继续输入实时筛选，↑/↓ 移动高亮，Enter 执行选中的匹配命令；仅输入或移动高亮不执行；
- 候选列表与 composer 共存，用户仍可编辑草稿；Esc 关闭候选并保留草稿；去掉开头 `/` 或变为多行时隐藏候选；
- 无匹配时不执行任何本地命令，Enter 把完整原文作为普通消息提交（仍走正常的空闲与配置检查）；不用「未知命令」拦截，不截取前缀、吞掉余文或改写原文；不能用默认高亮误执行命令；
- 运行中输入区允许保留命令草稿；提交时给短暂提醒；Ask/Permission 仍独占输入。

**`/model` 菜单**：
- 只读本地保存的目录，不联网；
- 每行显示「模型名称 [供应商]」，当前与 default 标记分开；文本筛选名称或供应商，↑/↓ 导航；
- Enter 切换当前会话的模型并关闭；Ctrl+S 把高亮项保存为全局默认，不切换当前、不关闭；Esc 只关闭，不撤销已保存的默认；
- 跨供应商切换时提示后续历史会发送到目标渠道，不误称只发新一句话。

**空闲切换**：
- 顺序为：校验目标与历史 → 准备新连接 → 保存会话选择 → 安装运行选择并更新事实区 → 收口旧连接；
- 准备或保存失败保留旧选择并关闭候选资源；安装后旧资源清理失败单独报告，不伪装成目标未保存；
- 下一 Run 固定开始时的连接、模型能力、思考与权限，不半轮替换；取消后等真实任务和检查点收口才算空闲；
- 切换不重读已冻结的系统提示词与 AGENTS，不新建会话、清空历史或重复工具；
- 切换后按新模型能力重算 `reasoningEffort` 的有效值，保留偏好并说明当前有效值。

**容量提醒**：
- 新模型的 `contextWindow` 小于当前模型时，先警告「上下文会缩小，历史可能超限」并要求确认；
- 只比较 `contextWindow`，不估算历史 token；容量是默认值或估计值时标明；确认不代表能突破容量，真超限由服务端正常报错；不悄悄删历史，本阶段不做压缩。

**会话选择**：
- `/model` 明确切换时更新会话保存的 `provider_id`、`model_id`；以后恢复该会话用保存的选择；
- 启动覆盖与初始选择规则沿用 A1（D-020、D-021）。

### 实现方式

**模块与文件**：
- 新建 `src/mica_agent/tui/main_buffer/commands.py`：固定命令登记（名称、说明、是否可用、处理函数）、匹配和分派；候选列表和实际执行用同一份登记；
- 新建 `src/mica_agent/tui/main_buffer/menus.py`：本地菜单的状态、筛选和绘制，复用 `interactions.py` 的 `PanelView` 和 `fit_panel_rows`；本地菜单与 Ask/Permission 语义分开；C1、D 的菜单以后也放这里；
- `src/mica_agent/tui/main_buffer/screen.py`：仍是唯一按键入口，只负责调用 commands 和 menus；调整 running 状态的输入门禁，让忙时可以编辑命令草稿并提醒（用可注入时钟或现有刷新机制）；
- 扩充 `src/mica_agent/app.py`：Provider 创建从 CLI 挪到 App；新增空闲切换接口，按上面的顺序执行；
- 扩充 `src/mica_agent/session_service.py`：新增更新会话选择的方法，复用 `JsonlSessionStore.update` 的锁内变换；
- 全局默认保存复用 `ConfigStore` 已有的目标项锁内原子保存。

**数据流**：
- screen 收到 `/` 开头的单行草稿 → commands 派生候选 → 渲染候选；
- Enter → commands 匹配并分派 → `/model` 打开 menus 的模型菜单；
- 菜单 Enter → `App` 空闲切换 → SessionService 保存选择 → App 安装并收口旧连接 → 菜单关闭；
- 菜单 Ctrl+S → ConfigStore 保存全局 `defaultModel` → 菜单刷新 default 标记。

**注意事项**：
- 本项命令登记只有 `/model`；`/login`、`/settings`、`/reload` 由后续交付项注册；
- 不引入命令插件框架。

---

## B · Responses 与 Messages 接口

### 要实现

**三类接口统一产出**：完整文本、独立思考事件、完整工具调用和真实终态；Engine 保持唯一的模型、工具、权限、取消循环，各适配器不建立自己的 Agent Loop。

| 接口 | 要求 |
|---|---|
| `openai-completions` | 保留已验收行为；合并工具参数碎片，处理独立 reasoning 字段；回传 DeepSeek 的 `reasoning_content`，解析并回传 OpenRouter 的 `reasoning`/`reasoning_details`（D-027）；OpenCode Go 的 Completions 模型暂不发送续接；不依据正文标签猜思考 |
| `openai-responses` | 转换 input items，关联 function call 和 output，保留必要的 phase 与受支持的 reasoning items；failed、incomplete、流结束缺少正常终态不冒充 Finish |
| `anthropic-messages` | 分离 system 和 messages，转换 tool_use/tool_result，完整组装 block 和 JSON 参数，处理 thinking/signature、输出上限（Messages 必须发送输出预算）和工具往返 |

**能力与思考强度**：
- 能力按手工声明、明确官方信息、默认值依次采用；未知上下文 200000 tokens，默认文本输入、不支持思考；图片能力只记录不实现；
- `reasoning` 缺失或 false 时只允许 `off`，不发送思考强度参数；为 true 且 `thinkingLevelMap` 缺失或为空时用适配器经过测试的默认档位映射；非空时只展示明确列出的档位。

**续接数据**（D-027）：
- 专属续接记录有明确字段、长度上限、来源绑定和校验，不保存 SDK 对象、整个原始响应或任意字典；绑定供应商 + 模型 + 接口 + 模型步骤，不能因接口名相同认定兼容；
- 每个模型步骤一条记录，按接口只存白名单字段：

| 接口 | 白名单 |
|---|---|
| `openai-responses` | reasoning item 的 `id`、`summary`、`encrypted_content`；assistant message 的 `phase` |
| `anthropic-messages` | thinking 的 `thinking`、`signature`；redacted_thinking 的 `data` |
| `openai-completions` | DeepSeek 的 `reasoning_content`；OpenRouter 的 `reasoning`、`reasoning_details` |

- 出现未识别的项类型或字段时，该步标为不可回放，只保留通用历史；
- 续接随每轮持久化到会话（DeepSeek 带 tools 时要求回传此前所有轮次的思考）；每轮续接合计不超过 1 MiB，超出时丢弃该轮续接并标为不可回放，会话照常保存；
- 回放条件：供应商 ID、模型 ID、接口 ID 都与保存时一致；不按账号绑定；
- Engine 在同轮工具往返中携带，并按回放条件携带此前轮次的续接；App/SessionService 在安全边界提交；TUI 只使用允许展示的思考文字；
- 活跃请求所需的原始协议内容只留在有界运行上下文，不进日志、repr 或普通持久化；签名或加密字段不能正则替换后仍声称有效；保存候选通过既有敏感边界且关联内容未被清洗改变，才可标记可回放，否则明确不可回放、只存清洗后的通用事实；不为签名绕过凭据、路径、工具参数清洗；
- 纯思考、无正文的模型步骤也能保存续接。

**跨接口与恢复**：
- 兼容的原连接继续时完整回传协议要求的数据；跨模型、供应商或接口时过滤不兼容续接，重编码普通历史与真实工具事实，不承诺继承内部思考；
- 不能合法转换的旧工具/思考组合用明确的 Mica 历史工作记录表示，保留实际结果，不伪造目标签名、思考或成功的工具结果；切回原连接时重新检查绑定，不保证旧签名在混合历史中仍有效；
- 不使用 Messages 的 beta 选项 `prefix_mismatch_behavior: "drop_block"`；服务端以签名或绑定无效拒绝时报告真实错误，不自动剥离续接后重试；
- 工具调用与结果在目标请求中一一关联；ID 形式不同时只建请求内映射，不改保存的调用身份；
- 系统指引与历史分开转换；工具输出和历史正文不得提升为 system/developer 指令；
- 中断检查点保留用户指令、已完成结果、部分正文和真实状态；跨接口恢复不自动执行旧工具，也不为未完成工具补结果；
- 可见思考与不可见续接分离，显示模式不影响必需的回传。

**会话格式**：
- 新增 v6：v1～v5 继续只读可用，读入时续接为空；第一次业务写入时在锁内整文件升级到 v6，和 v4、v5 的升级方式一致；旧版 Mica 无法读取 v6 会话；
- 精确 wire 形状见下方「续接与 v6 wire」，不预塞未定义的占位字段。

### 续接与 v6 wire（设计审查稿）

**模型步骤续接记录**：一个模型步骤（一次 Provider 请求到 Finish）最多一条，不产出续接的步骤记为空：

```json
{
  "provider_id": "<供应商 ID>",
  "model_id": "<模型 ID>",
  "api": "openai-completions | openai-responses | anthropic-messages",
  "status": "replayable | unreplayable",
  "items": [<项>, ...]
}
```

- `unreplayable` 时 `items` 为空数组，只表示「这一步有过续接但不能回放」；`replayable` 时至少一项；
- `items` 按原始输出顺序排列；正文和工具调用不重复保存，用引用项标出位置，内容从同一步的通用消息取。

**项**（每种项的字段都必须齐全，缺省值写 `null`，不允许多余字段）：

| `type` | 字段 | 允许的接口 |
|---|---|---|
| `reasoning` | `id`、`summary`（字符串数组，可为空）、`encrypted_content`（非空） | responses |
| `thinking` | `thinking`（可为空串）、`signature`（非空） | messages |
| `redacted_thinking` | `data`（非空） | messages |
| `text` | `phase`：`commentary`、`final_answer` 或 `null`；messages 只能为 `null` | responses、messages |
| `tool_call` | `call_id` | responses、messages |
| `chat_reasoning` | `reasoning_content`、`reasoning`（字符串或 `null`）、`reasoning_details`（数组）；三者至少一个有内容 | completions，且只能有这一项 |

`reasoning_details` 的元素字段为 `type`、`id`、`format`、`index`、`summary`、`data`、`text`、`signature`：

- `type` 为 `reasoning.summary` 时 `summary` 必填；
- `type` 为 `reasoning.encrypted` 时 `data` 必填；
- `type` 为 `reasoning.text` 时 `text` 必填，`signature` 可选；
- 与 `type` 无关的内容字段必须为 `null`；`id`、`format` 为字符串或 `null`，`index` 为非负整数或 `null`。

**校验与关联**：
- `id`、`call_id` 非空，不含空白和控制字符，不超过 256 个字符；`provider_id`、`model_id` 非空；
- 同一步 `text` 至多一项（原始输出有多段正文时整步不可回放）；`tool_call` 的 `call_id` 不重复；
- 关联：`tool_call` 项的顺序和集合与这一步 assistant 消息的 `tool_calls` 完全一致；这一步正文非空时恰有一个 `text` 项，正文为空时没有；completions 不使用引用项。

**清洗判定**：
- 这一步的 assistant 消息经过既有工具消息清洗后有任何改变（正文或工具参数），续接改为不可回放；
- 明文字段（`summary`、`thinking`、`reasoning_content`、`reasoning`、details 的 `summary`、`text`）经过既有文本清洗后有任何改变，整步不可回放；
- 不透明字段（`encrypted_content`、`signature`、`data`、details 的 `data`、`signature`）不做正则清洗，只校验为不含控制字符的非空字符串；
- 单轮上限：同一 Turn 或检查点内全部续接记录按紧凑 JSON（UTF-8、`sort_keys`）计算合计超过 1 MiB 时，全部改为不可回放。

**v6 会话记录**：
- `session_meta`：只把 `format_version` 升到 6，字段不变；
- `turn`：新增 `continuations` 数组，长度等于 `messages` 中 assistant 消息的条数，按顺序一一对应，每个元素为续接记录或 `null`；
- `run_checkpoint`：`progress` 的每个元素新增 `continuation`，为续接记录或 `null`；`partial_assistant_message` 没有 Finish，不带续接；
- v1～v5 读入时续接全部为空；v6 读取时续接违反上述校验或超过单轮上限，按格式错误拒绝。

### 实现方式

**模块与文件**：
- 新建 `src/mica_agent/providers/openai_responses.py`、`src/mica_agent/providers/anthropic_messages.py`：各自负责请求编码和 SSE 解析，都用 httpx；
- 新建 `src/mica_agent/continuation_contracts.py`（公共契约组）：独立的模型步骤续接记录契约，和 `Message`/`ToolCall` 分开；
- 扩充 `src/mica_agent/providers/base.py`：`ProviderRequest` 新增续接输入、`Finish` 返回续接；三个适配器共用的 SSE 读取和错误分类，只在确实重复时抽到这里；
- 扩充 `src/mica_agent/engine.py`：在本轮工具往返中携带续接；
- 扩充 `src/mica_agent/app.py`：按接口 ID 从一张固定表选适配器；ProviderFactory 接收完整 ConnectionBinding，CLI 与空闲切换共用该选型入口（D-028）；
- 扩充 `src/mica_agent/config_contracts.py`、`src/mica_agent/config_service.py`：将已校验的供应商 channel 透传到连接绑定，供 Completions 显式启用 DeepSeek/OpenRouter 续接；保留临时 custom/Completions 身份。已有连接的 URL、Key、接口或渠道在磁盘变化时拒绝继续发送，要求重启，不让旧 Provider 混用新来源；普通能力与设置仍在下一轮刷新（D-028）；
- 扩充 `src/mica_agent/session_contracts.py`、`src/mica_agent/checkpoint_contracts.py`、`src/mica_agent/jsonl_store.py`：v6 格式、TurnEntry 和中断检查点的续接字段、读取升级写入；
- 扩充 `src/mica_agent/session_service.py`：恢复时返回消息加续接；
- 扩充 `src/mica_agent/message_sanitizer.py`：续接数据的安全判定。

**接口映射表**：
- `openai-completions` → 现有 `OpenAICompatibleProvider`
- `openai-responses` → `OpenAIResponsesProvider`
- `anthropic-messages` → `AnthropicMessagesProvider`

**数据流**：
- Engine 请求模型 → `ProviderRequest` 携带续接 → 适配器编码专属字段；
- SSE 返回 → 适配器解析 → `Finish` 携带本步续接 → Engine 保存到本轮上下文；
- Turn 或检查点提交 → sanitizer 判定 → 只保存通过的记录；
- 恢复会话 → SessionService 投影消息加续接 → Engine 按当前连接过滤后携带。

**注意事项**：
- 不改写已验收的 Chat Completions 适配器的既有行为，新适配器独立实现；
- 续接数据不改变 `Message`/`ToolCall` 的公共 API；
- 白名单、保存范围、上限和回放条件已由 D-027 确定；v6 wire 和关联规则见「续接与 v6 wire」，第一张卡派发前随卡过用户审查；
- 跨接口兼容测试覆盖 completions、responses、messages 两两方向；
- OpenCode Go 的自有 User-Agent 和会话头归 C1。

**拆卡**（按顺序派发，共享契约不并行）：
1. 续接契约、记录 wire、清洗判定和单轮上限（`continuation_contracts.py`、`message_sanitizer.py`）；
2. 会话 v6：TurnEntry 和检查点的续接字段、v6 读写和旧版升级（`session_contracts.py`、`checkpoint_contracts.py`，及 JSONL 升级回归）；
3. Provider 协议（`ProviderRequest`/`Finish`）、Engine 携带续接、Chat Completions 续接；Engine 在清洗 assistant 消息之前，用原始消息调用 `sanitize_step_continuation`（会话契约里的调用只是兜底）；
4. Responses 适配器——完成（T-030，2026-10-13 第二轮验收通过）；
5. Messages 适配器——完成（T-031，2026-10-14 第一轮验收通过）；
6. 应用装配：按接口选适配器、SessionService 返回续接、提交和跨接口过滤；通过后用户做真实验收。
   - 模块拆分（2026-10-14 用户审查通过）：会话投影（T-032）→ 配置接口与能力 → App/CLI 装配及集成验收，三卡顺序施工。
   - 会话投影：一次读取返回消息与逐 assistant 续接；完整检查点步骤保留，未完成批次和部分输出作为 USER 历史工作记录，不生成系统指令或补造工具结果。完成（T-032，2026-10-14 第二轮验收通过）；调用侧九项旧投影测试已同步，最终全量（含安装态）2546 passed、2 项既有平台跳过。仅完成会话投影，不代表第 6 步整体或 B 的真实渠道验收完成。
   - 配置接口与能力：开放已支持的三类接口连接，按实际接口提供默认思考映射，保留显式能力声明和无思考降级。完成（T-033，2026-10-15 第二轮验收通过）；三接口默认预算与思考映射、显式声明、来源及选择/安全边界已有自动化证据，最终全量（含安装态）2687 passed、2 项既有平台跳过。该卡仅完成配置接口与能力；App/CLI 装配及整体/真实验收的当前进展见下一子项。
   - App/CLI 装配及集成验收：固定接口表选择适配器、冻结请求来源、提交续接并携带恢复快照，覆盖三类接口两两切换和取消/保存失败。自动化实现完成（T-034，2026-10-21 第三轮验收通过）；最终全量（含安装态）2764 passed、2 项既有平台跳过，相关链路及静态检查通过。第 6 步三卡自动化实现已完成；B 整体仍待用户以三类接口各跑一条真实工具对话并跨接口切换一次，模拟验证不替代真实验收。

---

## C1 · API Key 登录、目录与连接维护

### 要实现

**`/login` 入口**：提供订阅登录（C2 交付）、内置渠道 API Key、自定义供应商和已有连接维护。

**添加连接**：
- 内置渠道（OpenRouter、DeepSeek、OpenCode Go）输入 Key；自定义连接填写名称、URL、Key、接口；
- 稳定供应商 ID 与显示名称分离，改名不破坏历史引用；
- Key 输入遮蔽，原始 Key 只在提交时交给认证业务。

**目录发现**：
- 保存连接后自动尝试有界、可取消的目录请求，不发额外推理测试；
- 按渠道解析模型 ID、名称和明确的能力；
- 失败时保留连接并允许手填模型 ID；空列表与请求失败区别显示；
- 目录菜单 ↑/↓ 导航、Space 勾选、Enter 保存所选模型；只保存用户勾选的模型。

**保存后去向**：没有当前可用模型时转入 `/model` 明确选择；已有可用模型时返回草稿，不自动改当前或全局默认。

**连接维护**：
- 已有供应商可改 Key、显式刷新目录、改显示名称、禁用、启用或确认删除；自定义连接可改 URL 和接口；
- 刷新保留已有选中状态和手工字段，只补缺失项；远端暂未返回的已有模型不自动删除；
- 新认证验证并写入成功前保留旧记录；
- 禁用保留连接、目录和凭据，但不再可选或用于新请求；当前、默认或恢复引用保留并提示启用或重选；
- 确认删除只清理本地连接、目录和凭据，不删历史、不改用别的供应商、不声称远端撤销；刷新迟到不能重建已删配置；
- 多文件部分失败时报告未完成的范围，残留不当作有效连接。

**内置渠道**：
- 按官方文档确定目录地址、基址和认证，写成固定表；不把当前模型名称、额度或价格硬编码为承诺；
- OpenCode Go 按模型使用不同接口，发送 Mica 自己的 User-Agent 和稳定、不含工作区路径的会话标识，不冒充其他客户端；
- 官方目录缺少可靠接口信息时，用已核实的映射或让用户显式指定，不按模型名称猜协议。

### 实现方式

**模块与文件**：
- 新建 `src/mica_agent/credential_store.py`（配置与选择组）：`auth.json` 的安全创建（先建目录和文件并设权限，再写明文）、锁内重读合并、原子保存、删除；删除和 token 刷新共用凭据归属锁；保存后用 `_credential_permissions.py` 的受保护读取验证；
- 新建 `src/mica_agent/model_catalog.py`（配置与选择组）：按渠道用 httpx 请求模型列表，解析 ID、名称和明确能力，有界、可取消；内置渠道的基址、目录地址和接口映射写成这里的固定表；
- 扩充 `src/mica_agent/config_store.py`：`models.json` 的写入，和 `config.json` 共用一个内部原子替换步骤；
- 扩充 `commands.py` 注册 `/login`；扩充 `menus.py`：登录方式选择、遮蔽输入、表单、目录勾选、连接维护列表。

**注意事项**：
- 目录格式按渠道区分，不按协议（OpenRouter 和 DeepSeek 都用 Chat Completions，目录格式不同）；
- 目录请求和推理请求分开，不改 Provider 协议；
- 内置渠道的地址、超时和重试次数，由 master 写卡前按官方文档核实；
- 测试只用隔离目录和本地模拟服务，不访问真实渠道或真实 `~/.mica`。

---

## C2 · ChatGPT 官方订阅

### 要实现

**授权**：
- 采用官方 Sign in with ChatGPT 开源客户端流程；不复用 pi/Codex 凭据或登录文件，不读密码，不依赖私有 backend-api，不启动 Codex app-server；
- 用户在 `/login` 主动选择后打开浏览器授权；本地回调只监听 `127.0.0.1`，每次独立的 state、nonce、PKCE；
- 用成熟库验证 ID token 的签名、issuer、audience、到期和 nonce，并确认权限允许 ChatGPT plan usage，不手写密码学；
- 保存实际发放的 client ID 和本机稳定 host ID；
- 取消、拒绝、超时或没有 plan usage 权限时报告真实状态，不把身份登录当成可推理。

**多账号与刷新**：
- 每条记录绑定验证过的账号和注册身份，可独立保存多组订阅连接，返回登录不串账号；
- 刷新在同一凭据锁内串行，锁内重读最新 token，原子替换整组，避免多进程轮换竞争；
- 撤销或刷新失败时提示重新登录，不自动换渠道。

**目录与推理**：
- 目录按账号实际可见项解析；
- 推理走官方 Responses 路径：`store=false`、`stream=true`、发送完整 input，不依赖 `previous_response_id`，不发送 `max_output_tokens`；
- 冻结的系统指引转成允许的 instructions/developer，不发送会被拒绝的 system item；本地工具用允许的形式，不照搬 API Key 的字段；
- 只有 `response.completed` 算正常完成；额度错误、failed、incomplete、中途断流按真实失败处理。

**验收**：真实授权、可用性和工具请求需要用户参与，模拟 OAuth/SSE 不替代；遇到官方资格或额度限制及时说明，不靠非官方端点绕过。

### 实现方式

**模块与文件**：
- 新建 `src/mica_agent/chatgpt_auth.py`（配置与选择组）：授权流程、token 交换和刷新；本地回调用标准库 `ThreadingHTTPServer`，在受控线程里运行，有超时和取消，结果安全交回事件循环，关闭后迟到的请求被拒绝；用 `webbrowser` 打开浏览器；state、nonce、PKCE 用标准库生成；
- 引入运行依赖 `PyJWT[crypto]`，只管 JWT/JWK 验签；OAuth 流程本身用 httpx；
- 扩充 `providers/openai_responses.py`：由连接装配时传入订阅选项，只维护一套 Responses 编解码；
- 扩充 `credential_store.py`：OAuth 整组记录的保存和刷新；
- 扩充 `config_service.py`、`app.py`：识别订阅连接并装配；
- 扩充 `menus.py`：订阅登录项和等待状态。

**依赖审查**（派 C2 第一张卡前由 master 核实）：PyJWT 和 cryptography 的当前版本、许可证、传递依赖，以及对锁文件和分发的影响；官方端点和字段按 §技术依据 复核。

---

## D · 设置、重载与多窗口

### 要实现

**`/settings`**：只管理三项，显示当前有效值和来源：

| 项 | 值和生效 |
|---|---|
| `reasoningEffort` | off/minimal/low/medium/high/xhigh/max 中当前模型实际支持的档位；切换模型后重算有效值，保留偏好并说明当前有效值，不硬发旧档位 |
| `permissionMode` | default/solo；下一 Run 使用；不复制权限矩阵到 JSON，不开放自定义策略 |
| `reasoningDisplay` | full/collapsed/hidden；当前界面立即采用，不参与 Ctrl+O |

- Enter/Space 循环切换选中项并保存到工作区；Ctrl+S 只把这一项同时应用到全局，不整组复制，不改其他工作区的覆盖；
- 写失败不显示虚假生效；
- 设置作用于该工作区当前、新建、切换、恢复的会话，不被历史旧值覆盖。

**来源与 CLI 覆盖**：
- 启动时显式的 CLI 覆盖和 argparse 默认值区分并显示来源；当前窗口用户明确修改该项后以新操作为准；
- 其他进程的文件写入不撤销本进程的显式启动覆盖，界面解释来源。

**多窗口**：修改窗口立即生效；其他进程在下一 Run、打开设置或 `/reload` 时读到；进行中的 Run 固定不变；不做文件监听。

**权限快捷切换**：`Shift+Tab` 接入既有权限切换，保存到工作区；运行中只记待生效值，当前 Run 收口后提交；不在 Ask/Permission 中抢 Tab。

**`/reload`**：
- 重读 config、models、auth；不强制联网刷新目录，不重载 AGENTS、提示词或扩展；
- 成功时更新当前设置、能力和事实；当前模型被删除或禁用时保留引用，禁止新请求并提示；
- 语法或读取错误时保留旧显示快照，明确未成功重载，阻止错误配置下的新请求；不半份更新，不声称错误配置可用于新 Run。

### 实现方式

**模块与文件**：
- 扩充 `src/mica_agent/app.py`：持有待生效的设置意图，等消费任务结束、检查点提交、忙状态释放后保存；下一轮 `prepare_run()` 读到新值；重载失败的阻止在 `prepare_run()` 入口检查；
- 扩充 `src/mica_agent/config_store.py`：按目标项比较保存工作区和全局设置的方法；
- 扩充 `src/mica_agent/config_service.py`：重载并返回新快照或错误；
- 扩充 `commands.py` 注册 `/settings`、`/reload`；扩充 `menus.py`：设置菜单；
- 扩充 `screen.py`：`reasoningDisplay` 修改后立即重绘；Shift+Tab 在运行中改为更新 App 的待生效意图。

---

## 不做清单

1. 整体架构重构（D-014）；
2. 阶段 7 的会话命令（`/new`、`/rename`、`/branch`、`/rewind`、`/clear`、`/btw`、`/compact`）；
3. 图片、文件等多模态输入；
4. 多 Provider 自动路由和故障转移；
5. 配置文件监听和实时同步；
6. 登录时额外的推理连接测试；
7. 内置工作模式；
8. 历史 token 估算和上下文用量统计（阶段 7）；
9. 命令插件框架；
10. 未实现命令的空壳入口；
11. 任意 HTTP header、原始响应 JSON 路径和环境变量插值配置。

## 依赖变更

新增运行依赖 `PyJWT[crypto]`（MIT；后端 cryptography 为 Apache-2.0 或 BSD-3-Clause）。派 C2 第一张卡前，master 核实当前版本、许可证和传递依赖。

## 数据格式变更

- 会话 JSONL 升级到 v6，新增模型步骤续接记录（白名单与上限见 D-027）；v1～v5 只读兼容，首次业务写入锁内升级；精确形状在 B 第一张卡派发前的设计审查中确定；
- `models.json`、`auth.json` 开始由 Mica 写入（A1 只读），字段沿用 A1 已落地的格式；OAuth 记录的新增字段在 C2 的设计审查中确定。

## 测试要求

- 每张卡：`uv run pytest -q -W error`、`uv run ruff check src/mica_agent tests`、`uv run ruff format --check src/mica_agent tests` 通过；依赖变化后 `uv lock --check`、`uv build` 通过；
- 默认测试不依赖真实 Key、外部网络或真实 `~/.mica`；
- 新增公共契约和 wire 格式有直接断言；凭据文件权限在隔离目录做真实 ACL/POSIX 验证；
- 每项交付完成后由用户做真实验收（见交付项表格）。

## 技术依据

原 Phase 6 草案在 2026-10-03 核对的官方入口；写卡时复核字段，不把外部当前模型名或账号资格写成永久保证。

- ChatGPT：[注册与登录](https://developers.openai.com/siwc/token-sharing-open-source/sign-in)、[账号与刷新](https://developers.openai.com/siwc/token-sharing-open-source/profiles-and-sessions)、[模型与推理](https://developers.openai.com/siwc/token-sharing-open-source/models-and-inference)、[HTTP 限制](https://developers.openai.com/siwc/token-sharing-open-source/preview-limitations)。
- 思考与工具：[OpenAI 会话状态](https://developers.openai.com/api/docs/guides/conversation-state)、[Claude 工具与多轮思考](https://platform.claude.com/docs/en/build-with-claude/thinking-tool-workflows)。
- 目录：[OpenRouter](https://openrouter.ai/docs/api/api-reference/models/list-all-models-and-their-properties)、[DeepSeek](https://api-docs.deepseek.com/zh-cn/api/list-models/)、[OpenCode Go](https://opencode.ai/docs/go/)。

## 参考

- D-017：Phase 6 配置与多接口方案定稿
- D-020～D-024：启动覆盖、初始选择、分开验收、统一命令入口、即时候选
- T-021：A1 交付；T-023：剩余交付项的代码事实调研
- 原 `docs/configuration_spec.md` 已并入本计划后删除，历史版本见 Git
