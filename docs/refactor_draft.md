# Mica 历史重构草案（原 GenericAgent）

> 状态：历史讨论与决策记录，不再作为实施规范或当前项目状态来源。
> 正式来源：Mica 的产品方向、范围和架构边界统一以根目录 `PROJECT_PLAN.md` 为准。
> 使用方式：仅在追溯决定理由或核对未提炼背景时读取；不得直接据此生成阶段计划或 worker 任务。

## 1. 正式定调

本次重构的核心是“快刀斩乱麻”和“化繁为简”：

- 重构混乱、职责不清且高度耦合的代码。
- 删除臃肿、重复、偏离核心定位的功能。
- 最终产出一个轻量、高效、安全、可维护性强、可扩展性高的 Agent 基座。
- 重构过程也承担学习目标：通过真实架构决策理解一个完整 Agent 项目的组成、契约和取舍。
- 重构后的项目正式名称确定为 **Mica**；它是一个新生项目，不作为 GenericAgent 的小版本升级进行包装。

所有后续设计都必须接受以下检查：

1. 是否直接服务核心 Agent 能力？
2. 是否降低了理解、修改、测试或部署成本？
3. 是否可以用更简单的契约实现？
4. 是否把可选功能错误地塞进核心？
5. 是否引入了当前阶段不需要的复杂性？

## 2. 状态标记

- `已确认`：用户已经明确决定，可以作为后续设计前提。
- `工程约束`：由当前源码、协议或运行环境决定，不能只凭偏好忽略。
- `待讨论`：尚未形成结论，禁止在实施时擅自选择。
- `候选项`：有价值但不进入第一阶段核心范围。

## 3. 项目定位

### 3.1 已确认

- 项目定位为单用户优先的个人 Agent 基座，不建设多租户系统。
- 一个用户可以拥有多个会话或 Agent 实例；多 Agent 不等于多用户。
- 项目允许他人在自己的电脑上独立安装、运行和二次开发。
- 第一阶段正式前端只保留 CLI 和 TUI。
- 第三方通讯平台接入保留为候选扩展，不进入核心依赖。
- Web、Qt 桌面端、Streamlit、Hub 和 Conductor 不属于第一阶段必须保留的正式入口。
- 浏览器操作不进入底层核心；后续通过插件或 MCP 提供。
- 当前复杂记忆系统删除；启动当前对话时只自动载入全局 `AGENTS.md` 和当前工作区项目级 `AGENTS.md`。

### 3.2 高可扩展的具体含义

“高可扩展”特指代码层面的积木式扩展：

- 开发者通过添加独立模块并进行一次显式注册即可增加功能。
- 新增功能不应要求修改 Agent 核心循环。
- 不应在 Python 实现、JSON Schema、提示词等多个隐含位置重复登记同一能力。
- 插件之间通过公开、稳定、可测试的契约接入。

优先扩展点：

- Tool：本地工具和外部能力。
- Slash Command：CLI/TUI 会话命令。
- Provider：LLM API 协议实现。
- Frontend Adapter：CLI、TUI 和候选通讯平台。
- Policy：权限策略。
- Event Plugin：日志、追踪和统计。

## 4. 部署与配置

### 4.1 已确认

- 第一阶段优先适配 Windows。
- 首期把 Node.js/npm 和 Python 或 uv 视为外部前置环境；安装器不负责自动安装或修改这些系统级环境。
- Python 与 uv 至少存在一个即可；缺少前置环境时应给出明确、可操作的错误，然后退出。
- 目标体验是一条终端命令完成安装并注册全局 `ga` 命令。
- 首期正式安装入口使用 npm，目标形式为 `npm install -g @<scope>/mica`；最终 npm scope 在发布准备阶段确认。
- npm 只承担分发、版本管理和全局 `ga` 启动器职责；Agent 核心继续使用 Python 实现，不为安装方式重写成 Node.js。
- npm 安装过程保持无交互，不使用 `npx` 专用安装器，也不通过安装钩子询问用户是否补齐系统环境。
- 用户首次执行 `ga` 时，启动器优先检测 uv、后备检测 Python，在 `~/.ga/runtime/` 创建专用环境并安装与 npm 包完全同版本的 Python 核心，然后进入首次配置向导。
- 类似 `powershell -c "irm <url>/install.ps1 | iex"` 的 PowerShell 一行安装脚本保留为后续候选，不属于首期交付要求。
- 首次启动必须进入引导性强的交互配置流程。
- 首次配置只收集直接决定 API 调用能否成功且通常不会在会话中频繁变化的字段，例如 API 协议、API 地址和 API Key；同时只要求选择一个默认模型。
- 普通可配置项使用专门的 JSON 配置文件，不采用 TOML。
- 高级用户可以使用环境变量覆盖配置。
- `config.json` 是整个应用的用户配置入口，不只保存模型供应商信息。
- 配置文件采用接近 Claude Code `settings.json` 的简单键值风格；权限模式的默认行为不在配置文件中重复声明。
- 用户可以配置默认思考强度、默认权限模式、默认工作模式和界面偏好。
- API Key 第一版允许保存在受限文件权限的用户级 JSON 配置中，并允许环境变量覆盖。
- 全局用户数据统一存放在用户主目录下的 `~/.ga/`，不使用 `%APPDATA%`，也不写入 Python 或 npm 包安装目录。
- 全局配置文件为 `~/.ga/config.json`；会话、日志、扩展和缓存分别存放在该目录的独立子目录。
- 项目级配置文件为当前工作区的 `.ga/config.json`，其配置项覆盖全局 `~/.ga/config.json` 中的同名项。
- `ga config` 管理全局 `~/.ga/config.json`；`ga config --project` 管理当前工作区 `.ga/config.json`；会话内 `/config` 仍只修改当前会话临时配置。
- 项目配置向导默认不把明文 API Key 写入可能被 Git 跟踪的 `.ga/config.json`；项目可以覆盖 API URL、模型和偏好，密钥优先来自环境变量或全局配置。若高级用户手工在项目配置中写入 API Key，系统允许读取但必须提示泄露风险。

配置首期以少量稳定键为主，示意如下：

```json
{
  "apiUrl": "https://api.example.com",
  "apiKey": "<secret>",
  "model": "default-model",
  "thinkingLevel": "medium",
  "permissionMode": "default",
  "workMode": "direct",
  "sessionCleanupDays": 30,
  "permissions": {
    "allow": ["Read", "Glob", "Grep"]
  }
}
```

字段含义：

- `apiUrl`、`apiKey`、`model`：当前 Provider 连接和默认模型。
- `thinkingLevel`、`permissionMode`、`workMode`：新会话的默认行为。
- `sessionCleanupDays`：会话文件不活跃清理时限。
- `permissions.allow`：跨越三种普通权限模式都自动允许的工具或操作匹配规则。

`permissions.allow` 不是权限模式定义，也不负责写入目录、网络主机等复杂边界配置。它只是一个用户级的全局自动允许列表，设计上参考 Claude Code 的 `permissions.allow`。

### 4.2 工程约束

- 当前 `pyproject.toml` 只打包 `ga_cli`，核心模块、前端和资源没有形成完整可分发包。
- 当前 `ga` 命令依赖源码仓库相对路径，属于源码启动器，不满足安装后从任意目录运行的目标。
- npm 并不是 Python 的运行时。正式 npm 包需要提供薄启动器；启动器在首次运行时优先使用 uv、后备使用系统 Python，在 `~/.ga/runtime/` 建立项目专用环境并安装与 npm 包完全同版本的 Python wheel。
- npm 包和 Python wheel 必须作为同一版本发布、升级和卸载；禁止安装时不固定版本地拉取最新 Python 包。
- 使用 npm 安装意味着 Node.js/npm 是明确前置条件；Python 或 uv 仍是运行 Python 核心所需的前置条件。任一类前置条件缺失时都不自动安装，错误信息必须指出缺失项和用户可采取的修复操作。
- JSON 不支持注释。配置项的解释、默认值和校验不能依赖在配置文件中书写大量说明，应由配置向导、`ga config` 和文档提供。
- 思考强度需要由统一枚举映射到各供应商支持的参数；不能要求用户理解 `reasoning_effort`、`thinking_type` 等供应商私有字段。
- 工作模式和权限模式必须分开。Plan mode 决定 Agent 先规划还是直接执行；权限模式决定工具是否允许执行、需要确认或必须拒绝。
- 权限模式的默认行为由程序代码中的策略表定义，不能在 `config.json` 中重复展开。
- `permissions.allow` 只表示用户希望跨模式自动允许的工具或操作匹配规则，不替代模式策略。
- 权限决策优先级固定为：硬性拒绝 > `permissions.allow` 命中 > 当前权限模式。
- `permissions.allow` 不能覆盖危险指令、提权、凭据读取、系统级持久化或其他硬性拒绝规则。

配置优先级确定为：

```text
当前会话临时设置
> 本次启动的命令行参数
> 当前进程可见的环境变量
> 项目级 .ga/config.json
> 全局 ~/.ga/config.json
> 程序默认值
```

临时切换只影响当前会话，除非用户明确选择保存为默认值。环境变量只作为覆盖层，不反向写入任何 JSON 配置文件；Windows 用户级环境变量会自然覆盖所有项目，当前终端临时设置的环境变量只影响由该终端启动的进程。

首次配置向导必须依次收集 API URL、API Key，执行连接测试，并选择或手动输入默认模型；其他偏好可以跳过并使用程序默认值。连接测试失败时允许保存配置并进入 CLI/TUI，但必须持续显示“必要配置未完成，对话当前不可用”或等价状态，直到连接配置验证成功。

### 4.3 待讨论

- 项目正式名称已经确定为 **Mica**。未加作用域的 npm 包 `mica` 和 PyPI 分发包 `mica` 已被其他项目占用；仍需确定 npm scope、Python 分发名、Python 导入包名、终端命令和用户数据目录之间的映射。
- npm 包的最终作用域、发布仓库和 Windows 升级/卸载细节。
- 旧 `mykey.py`、日志和会话数据是否迁移到 `~/.ga/`，以及迁移失败时的回退规则。
- 全局与项目级 `AGENTS.md` 的实际发现路径、缺失行为和覆盖优先级。
- `~/.ga/sessions/` 按工作区隔离时使用规范化路径哈希、项目 ID 还是可读目录名。

## 5. LLM 供应商与模型

### 5.1 已确认范围

- 第一阶段不建设复杂的多供应商路由系统。
- 系统只维护一份当前启用的供应商连接，设置和更换供应商必须易于操作、引导性强。
- 当前供应商下可以发现多个模型，并在会话中随时切换模型。
- 不在首次配置时预先维护完整模型列表；用户执行 `/model` 时才实时探测当前供应商的可用模型。
- 切换模型时不能丢失当前对话上下文。
- 当目标模型的已知上下文上限小于当前会话预计占用量时，必须明确告知用户并请求确认；用户确认后仍可继续切换，不得直接拒绝。
- 模型请求失败后只重试当前模型，不自动切换模型或供应商。
- 后台模型探测只收集模型 ID 和最大上下文，不探测最大输出长度、工具调用、视觉或其他能力。
- 无法从供应商获知某模型最大上下文时，第一阶段使用 `200K` 默认上下文；该值允许用户覆盖。
- `/model` 探测失败时保留手动输入模型 ID 的后备入口。
- `/compact` 纳入首期斜杠命令；用户可以主动触发会话压缩，达到用户配置的上下文使用百分比阈值时也可以自动触发。压缩算法、确认交互和精确配置键在 CLI/TUI 阶段集中确定。

### 5.2 目标概念边界

- `ProviderConnection`：API 协议、API 地址、API Key、代理和网络参数。
- `ModelProfile`：模型 ID 和最大上下文。
- `Conversation`：由 Agent 核心维护的供应商无关标准消息历史。
- 切换模型只替换当前 `ModelProfile`，不搬移或重建 `Conversation`。
- Provider 负责把标准消息转换成具体 API 协议格式。

### 5.3 工程约束

- OpenAI 或 Anthropic 兼容的模型列表接口不保证返回最大上下文，很多接口只返回模型 ID。
- 最大上下文缺失时不能伪造为已探测值。可行来源只有供应商返回值、内置模型目录或用户覆盖。
- 切换到上下文更小的模型时，系统必须先明确告知用户风险并请求确认；用户确认后才允许切换，不能静默丢失历史。首期不因上下文不足自动触发压缩作为切换的隐含副作用。
- 重试应位于 Provider 网络请求边界，避免把已经产生副作用的整轮 Agent 或工具执行重复运行。

### 5.4 重试原则

- 仅对临时网络失败、超时、限流和部分服务端错误执行有限重试。
- 鉴权失败、模型不存在、参数错误、上下文超限和用户取消不自动重试。
- 优先遵守供应商返回的 `Retry-After`。
- 达到上限后返回结构化错误，不进入自动模型故障转移。

### 5.5 当前项目事实

- 当前项目能够配置多个 LLM Client、运行时使用 `/llm` 切换，并通过 Mixin 自动故障转移。
- 当前供应商协议主要通过配置变量名中的 `native`、`claude`、`oai` 等字符串判断。
- 当前配置向导会调用模型列表接口，但只提取模型 ID。
- 当前 `context_win` 主要是手工配置或静态预设，并以字符数近似 token 进行历史裁剪。
- 当前切换模型通过复制 Provider 内部 `backend.history` 保留上下文，耦合具体实现。
- 新版不保留 Mixin 自动故障转移作为第一阶段核心能力。

## 6. 权限与安全

### 6.1 已确认

- Agent 必须具有可配置的权限边界。
- 用户可以根据实际场景切换权限策略。
- Agent 可以申请权限，但不能自行批准或提升权限。
- 权限策略至少支持 `ALLOW`、`DENY` 和 `ASK` 三种判定。
- 权限检查必须发生在工具真实执行前；仅隐藏模型可见工具不能构成安全边界。
- 用户可以在 `config.json` 中设置默认权限模式，并在会话中临时切换。
- 本项目首期正式使用三个权限模式：`default`、`accept_edits`、`solo mode`。
- `solo mode` 是显示名称；配置和内部协议使用无空格的 `solo` 值。
- `danger` 不属于首期权限模式，不进入 `Shift+Tab` 循环，也不提供斜杠命令入口。

权限模式只改变普通工具调用是否需要用户确认，不改变工作模式、思考强度、工具集合或会话上下文。三种模式的默认权限范围由代码中的内置策略表定义，不在 `config.json` 中重复配置。

三种模式的决策矩阵如下：

| 操作类别 | `default` | `accept_edits` | `solo mode` |
|---|---|---|---|
| 普通读取工具 | 自动允许 | 自动允许 | 自动允许 |
| 文件创建和编辑工具 | 每次确认 | 自动允许 | 自动允许 |
| 文件删除、移动和批量修改工具 | 每次确认 | 每次确认 | 自动允许 |
| 普通命令和网络工具 | 每次确认 | 每次确认 | 自动允许 |
| `permissions.allow` 命中的普通工具或操作 | 自动允许 | 自动允许 | 自动允许 |
| 未注册工具、越过内置工作区/Provider 边界的请求 | 拒绝 | 拒绝 | 拒绝 |
| 提权、读取凭据存储、进程注入、系统级持久化和危险指令 | 拒绝 | 拒绝 | 拒绝 |

这里的工作区边界、Provider 网络边界和危险操作表由代码中的 `BuiltinSafetyPolicy` 定义；用户通过 `permissions.allow` 只能额外声明普通操作自动通过，不能扩大系统边界。`solo mode` 的含义是“除硬性危险操作外，普通工具调用不再询问”。

硬性拒绝的操作不能通过 Agent 或 `permissions.allow` 解除。若未来需要更高权限，应另行设计显式的高风险启动流程，而不是把规则继续堆进普通配置文件。

### 6.2 工作模式与权限模式

两类模式必须正交，不能组合成一个含混的总开关：

- 工作模式控制 Agent 如何推进任务，例如直接执行或先形成计划。
- 权限模式控制工具调用的安全判定，例如自动允许低风险操作、请求确认或拒绝。
- 进入 Plan mode 不代表权限自动降低或提高。
- 切换权限模式不应改变 Agent 的思考和计划流程。

### 6.3 权限策略应覆盖

- 工作区根目录、允许读取路径和允许写入路径。
- 工作区之外的文件访问。
- 命令和程序执行、超时、输出限制及子进程生命周期。
- 网络访问和域名边界。
- 浏览器控制。
- 必须确认、始终拒绝和可临时授权的操作。
- 权限允许、拒绝和确认事件的审计记录。

### 6.4 仍需确定的实现细节

- 内置 `BuiltinSafetyPolicy` 的第一版危险操作清单和路径/网络边界细节。

### 6.5 代码层面的权限控制方案

当前代码的工具路径是：

```text
BaseHandler.dispatch(tool_name, args, response)
  -> getattr(self, f"do_{tool_name}")
  -> GenericAgentHandler.do_<tool>()
```

重构后，真实执行路径必须变为：

```text
工具请求
  -> ToolRegistry 找到 ToolSpec
  -> PolicyEngine.check(ToolRequest, SessionPolicy, BuiltinSafetyPolicy)
  -> DENY：返回结构化拒绝结果
  -> ASK：向前端发出确认事件；拒绝则不执行
  -> ALLOW：调用真正的工具处理器
```

实现上的关键点：

- `ToolSpec` 声明工具需要的能力，例如 `filesystem.read`、`filesystem.write`、`process.execute`、`network.request`；权限引擎根据声明和实际参数共同判断，不能只根据工具名称判断。
- `ToolRequest` 在检查前统一规范化路径、命令工作目录和网络目标，防止用 `..`、相对路径或别名绕过边界。
- `PolicyEngine` 只返回 `ALLOW`、`ASK`、`DENY` 决策，不直接执行工具，也不依赖 LLM 的自我约束。决策顺序固定为硬性拒绝、全局 `permissions.allow` 匹配、当前权限模式。
- `ToolExecutor` 是唯一调用工具处理器的入口。即使前端隐藏了某个工具，或模型错误地请求了某个工具，真实执行前仍会再次检查。
- `default` 的确认不缓存为后续授权；用户批准一次，只允许当前这一次工具调用。
- `accept_edits` 只自动放开允许写入目录内的创建和修改，不自动放开删除、移动、命令执行或网络请求。
- `solo mode` 自动放开所有通过 `BuiltinSafetyPolicy` 的普通工具动作，但硬性拒绝规则仍由 `PolicyEngine` 强制执行。

因此权限控制不是给 `do_write()`、`do_code_run()` 等函数分别加几个 `if`，而是把所有工具调用收敛到一个可测试的策略入口。工具本身只负责业务动作，权限引擎只负责是否允许动作，二者职责分离。

### 6.6 权限切换与会话恢复

- `Shift+Tab` 在任务空闲时立即切换权限模式。
- Agent 循环运行期间，快捷键只写入 `pending_permission_mode`；当前 Turn 和当前工具调用继续使用旧模式。
- 当前循环自然结束后，Agent Engine 在安全边界提交待生效模式，并刷新状态栏。
- 模式切换结果写入当前会话的 `SessionMeta` 或会话配置记录，程序退出时保留最后生效的模式。
- `--resume` 恢复会话时恢复该会话结束时的权限模式。
- 新建会话不继承其他会话的临时模式，而是读取 `config.json` 中的默认权限模式。

## 7. 初步核心边界

目标调用关系暂定为：

```text
CLI / TUI
    -> ApplicationService
    -> AgentEngine
       -> ConversationStore
       -> Provider
       -> ToolRegistry
       -> PolicyEngine
       -> EventSink
       -> CancellationToken
```

前端只提交结构化任务并消费结构化事件，不直接操作 Handler、Provider 历史、线程和内部字典。

### 7.1 Agent Engine 与事件协议（已确认）

`AgentEngine` 只负责推进一次 Agent 运行：调用 Provider、接收流式响应、执行工具、把工具结果送回模型、处理轮数与取消，并报告运行结果。以下职责必须留在 Engine 之外：

- CLI/TUI 输入、快捷键、Markdown 渲染和状态栏布局。
- 会话命令解析与 JSONL 文件的具体存取。
- Provider 私有协议转换和重试细节。
- 权限规则、工具注册信息和工具业务实现。

一次用户指令对应一个 `Run`，一次模型调用及其后续工具处理对应一个 `Turn`。首期正式运行状态为：

```text
RUNNING_MODEL
STREAMING_TEXT
EXECUTING_TOOLS
WAITING_INPUT
WAITING_PERMISSION
COMPLETED
FAILED
CANCELLED
LIMIT_REACHED
```

其中 `WAITING_INPUT` 和 `WAITING_PERMISSION` 是可以恢复的暂停状态，不是退出。`ask` 等待用户回答后继续同一个 `run_id`；权限确认批准后只执行当前这一笔工具调用，拒绝后把结构化拒绝结果交回模型。

核心只发出结构化事件，不包含终端颜色、Markdown 模板或控件布局。第一版事件集合为：

```text
run_started
text_delta
tool_started
tool_output
tool_finished
input_requested
permission_requested
status_changed
run_finished
run_failed
run_cancelled
run_limit_reached
```

每个事件至少携带 `run_id`、`turn`、`sequence`、`timestamp` 和类型化数据。`sequence` 用于保证流式文本、工具状态和跨线程消息的处理顺序。

取消统一通过 `CancellationToken` 传入核心。CLI/TUI 负责把双击 `Esc`、`Ctrl+C` 或其他交互转换成取消请求；Engine 不感知具体快捷键。Provider 应尝试关闭当前流，`bash` 必须终止其子进程树，Engine 最终发出 `run_cancelled`。应用退出属于前端生命周期，不与取消当前 Run 混为一体。

Engine 的终态结果只区分 `completed`、`failed`、`cancelled` 和 `limit_reached`。等待用户输入或权限确认时返回可恢复的 `waiting` 快照，但它不是 Run 的最终结束。

旧控制方式按以下原则替换：

- 删除用 `StepOutcome.next_prompt` 字符串隐式控制循环的方式；工具只返回统一 `ToolResult`。
- 删除含义混杂的 `StepOutcome.should_exit`；完成、等待、取消和失败由 Engine 状态明确表达。
- `BaseHandler` 回调不得通过返回字符串暗中修改主循环；扩展通过受限事件订阅或生命周期接口接入。
- `display_queue` 的 `next`/`done` 字典不再作为核心公开协议；CLI/TUI 统一消费 Engine 事件。
- 内部首期可以继续使用生成器或队列，但不能把具体调度方式暴露为稳定扩展契约。

## 8. 唯一重构讨论队列

后续讨论严格按以下顺序推进。每一轮只处理第一个 `待讨论` 项，形成结论后将其改为 `已确认`，再进入下一项；未轮到的项目不提前做实现决策。

1. 配置文件与权限策略：`已确认`。包括简单键值配置、`permissions.allow`、三种权限模式、硬性拒绝和会话恢复规则。
2. 内置工具契约与注册机制：`已确认`。首期工具为 `read`、`write`、`edit`、`ls`、`bash`、`ask`；使用单一 `ToolSpec`、统一 `ToolResult`、显式 `ToolRegistry`，并为 MCP/Skill 定义可选扩展入口。
3. Agent Engine 与事件协议：`已确认`。Engine 只负责编排 Run/Turn；等待输入和权限确认可恢复；前端消费结构化事件；取消统一使用 `CancellationToken`。
4. 会话 JSONL：`已确认`。用户指令粒度、完整恢复、选择式 `/rewind`、整会话 `/branch`、原子写入和 30 天默认清理已确定；最终字段 schema 与 Provider 标准消息契约联动定稿。
5. Provider、模型切换与重试：`已确认`。单一当前 Provider、实时模型探测、上下文不足确认、未知上下文使用 200K 默认值、手动模型 ID 后备、当前模型有限重试且不自动故障转移。
6. CLI/TUI 工作流：`已确认`。单次命令注册、常驻状态栏、`/config` 当前会话面板、`Shift+Tab` 待生效切换、状态相关的 `Ctrl+C`/`Esc` 快捷键和 `/compact` 入口已确定；压缩算法另行细化。
7. Goal Mode：`已删除`。首期不保留目标驱动的持续执行模式、预算调度或专门状态；普通多轮会话作为唯一核心运行方式。未来如再次需要目标模式，作为独立扩展重新设计。
8. Windows 部署与首次配置向导：`已确认`。首期使用全局 npm 命令分发无交互薄启动器；Node.js/npm 与 Python 或 uv 是外部前置环境，不自动补齐；首次执行 `ga` 时创建专用 Python 环境并进入 Provider 配置向导。PowerShell 一行安装脚本延期。
9. 迁移、删除与验收：`讨论中`。项目名已确定为 Mica，并确认采用“同仓建立新核心”；新版包结构、职责边界和依赖方向已经确认。当前只继续确认影响全局构建的技术基线、兼容边界、删除原则和项目级验收原则；阶段交付切片与文件级施工细节延期。

## 9. 当前功能盘点与初步取舍

> 本节是基于当前源码的重构建议，尚未全部由用户确认。状态含义：`保留`、`简化保留`、`插件化`、`删除`、`延期`。

### 9.1 核心运行与模型

| 当前功能 | 当前实现概况 | 初步建议 | 理由 |
|---|---|---|---|
| 多轮 Agent Loop | `agent_loop.py` 连接 LLM、工具结果和下一轮 | 保留并重写边界 | Agent 基座核心；保留循环语义，拆开响应处理、工具执行和结束策略 |
| 流式文本输出 | 生成器经 `GenericAgent.run()` 写入队列 | 保留 | CLI/TUI 的核心体验；改为结构化事件，避免累计文本与 delta 混用 |
| 工具调用与多工具调用 | Schema + `do_<name>` 动态分发 | 简化保留 | 改为显式 `ToolRegistry`、参数校验和权限检查 |
| `StepOutcome` 控制语义 | 工具返回继续、完成或主动退出 | 简化保留 | 三种结果有价值；改成更明确的 `ToolResult` 和引擎控制状态 |
| OpenAI 兼容协议 | Chat Completions、Responses、SSE、原生工具调用 | 保留 | 主流 Provider 协议 |
| Anthropic 兼容协议 | Messages、SSE、thinking、原生工具调用 | 保留 | 主流 Provider 协议 |
| 文本模拟 Tool Calling | 从 XML/JSON 文本解析工具调用 | 延期或兼容插件 | 增加解析脆弱性；第一阶段优先原生 Tool Calling，确有兼容需求再接入 |
| 多供应商同时配置与手动切换 | 多个 Client 存在 `llmclients` 中 | 删除第一阶段实现 | 已确认只维护当前供应商连接 |
| Mixin 自动故障转移 | 多 Session 重试和回切 | 删除 | 用户明确不需要自动换模型或供应商 |
| 当前模型网络重试 | `_stream_with_retry()` | 保留并收敛 | 只重试可恢复网络错误，使用统一策略和结构化错误 |
| `/model` 会话内切换 | 当前 `/llm` 按 Client 索引切换 | 简化保留 | 改为按需探测当前供应商模型并切换 `ModelProfile` |
| 对话历史裁剪 | 字符数近似 token 后压缩和删除 | 重做 | 保留容量管理需求，删除粗糙字符估算和隐式修改 |
| Prompt Cache 特殊处理 | 按模型名称注入缓存字段 | 延期 | 非第一阶段基础能力，且供应商差异大 |
| 思考强度设置 | 直接暴露 `reasoning_effort`、`thinking_type` | 简化保留 | 用户只配置统一强度，由 Provider 映射 |
| Token/费用统计 | `cost_tracker.py` monkey patch | 延期 | 有用但不是首版核心；未来通过正式 Usage 事件实现 |

### 9.2 内置工具

| 当前功能 | 已确认处理 | 理由 |
|---|---|---|
| `file_read` | 重写为 `read` | 只负责按路径和行范围读取文件，不再混入搜索、记忆日志或 SOP 提示注入 |
| `file_write` | 重写为 `write` | 只负责新建或完整覆盖；首期删除 append、prepend 和文件引用模板 |
| `file_patch` | 重写为 `edit` | 使用唯一旧文本到新文本的精确替换；删除 `{{file:...}}` 隐式引用 |
| 文件枚举 | 新增 `ls` | 提供跨平台、只读的文件枚举和 glob 匹配，不要求模型通过 shell 完成基础发现 |
| `code_run` | 删除并重写为 `bash` | 保留命令执行能力，删除 `inline_eval`、代码块隐式执行和访问 Agent 内部对象的路径 |
| `ask_user` | 重写为 `ask` | 作为模型可调用的交互工具，调用后由引擎发出询问事件并等待用户输入 |
| `update_working_checkpoint` | 从模型工具中删除 | 属于上下文管理内部职责，不应要求模型手工维护框架状态 |
| `start_long_term_update` | 从模型工具中删除 | 记忆固化不应通过强制工具和轮数规则绑入核心循环 |
| `no_tool` 内部伪工具 | 删除 | 把“模型未调用工具”的结束/重试策略放回 AgentEngine |
| Schema 中英文双份维护 | 删除 | 单一工具定义生成 Schema；描述国际化另行处理 |

首期内置工具固定为：

```text
read
write
edit
ls
bash
ask
```

工具职责：

- `read(path, start_line, line_count)`：读取文本文件的指定范围。
- `write(path, content)`：新建文件或完整覆盖文件。
- `edit(path, old_text, new_text)`：执行一次唯一、精确的局部替换。
- `ls(path, pattern, max_depth)`：列出或匹配文件；属于只读能力。
- `bash(command, cwd, timeout)`：执行明确命令；Windows 由平台 Shell Adapter 调用 PowerShell，Linux/macOS 调用 Bash，模型可见名称统一为 `bash`。
- `ask(question, options)`：暂停当前 Agent 流程，等待用户回答后把结果送回模型。

首期不内置独立文本搜索工具。Agent 可以通过 `bash` 使用 `rg`、PowerShell `Select-String` 或其他当前环境命令；未来若需要稳定的跨平台搜索能力，可以新增 `grep` 扩展工具，不修改核心循环。

所有工具必须通过单一 `ToolSpec` 同时声明名称、描述、参数 Schema、权限能力和处理器。`ToolRegistry` 从 `ToolSpec` 生成模型可见 Schema、执行参数校验、帮助信息和权限请求；不再维护独立的 `assets/tools_schema*.json`。

所有工具统一返回 `ToolResult`：

```text
status: success | error | denied | cancelled
content: string | structured data
error_code: optional string
metadata: optional structured data
```

### 9.2.1 扩展标准

当前项目没有正式 MCP 客户端，也没有运行时 Skill 加载系统。`memory/skill_search` 只是独立检索包，`plugins/langfuse_tracing.py` 依赖 monkey patch，不能作为新版扩展标准。

新版扩展必须通过 Python 包入口显式注册，禁止 monkey patch、导入副作用和直接访问 Agent 私有字段。建议使用：

```toml
[project.entry-points."genericagent.extensions"]
my_extension = "my_extension:setup"
```

扩展只导出一个入口：

```python
def setup(api: ExtensionAPI) -> None:
    ...
```

`ExtensionAPI` 首期只暴露稳定注册接口：

```text
api.tools.register(ToolSpec)
api.skills.register(SkillSpec)
api.events.subscribe(event_type, handler)
```

MCP 不写入基础核心实现。未来由可选的 `genericagent-mcp` 扩展负责连接 MCP Server、发现远程工具，并把每个 MCP 工具转换为普通 `ToolSpec` 后注册到同一个 `ToolRegistry`。模型可见名称采用 `mcp__<server>__<tool>`，所有调用仍经过统一参数校验、`PolicyEngine` 和 `ToolResult` 转换。

Skill 不是工具，而是按需加载的说明、流程和资源包。建议沿用目录约定：

```text
skill-name/
  SKILL.md
  scripts/
  references/
  assets/
```

`SkillSpec` 至少声明名称、描述、入口文件、资源目录和所需工具能力。Skill 扩展负责发现、选择和加载 Skill 内容；Skill 可以调用已经注册的内置工具、扩展工具或 MCP 工具，但不能直接绕过 `ToolRegistry` 和 `PolicyEngine` 执行代码。

因此内置工具、Python 扩展工具和 MCP 工具共享一套工具契约；Skill 使用独立的上下文契约。新增任何扩展都不修改 Agent Loop。

### 9.3 浏览器与计算机控制

| 当前功能 | 初步建议 | 理由 |
|---|---|---|
| `web_scan` 网页读取和 HTML 简化 | 从当前项目删除 | 浏览器不是底层必要能力；未来由独立插件或 MCP 自行提供，不保留核心占位实现 |
| `web_execute_js` 完全控制浏览器 | 从当前项目删除 | 高权限且平台依赖明显；未来接入时必须经过外部能力边界和权限策略 |
| TMWebDriver/CDP 浏览器扩展 | 删除 | 当前轮询、全局字典和扩展桥接复杂且不稳定，不迁入新核心 |
| `simphtml.py` DOM 简化与变化检测 | 删除 | 只服务旧浏览器链路，不进入基础安装包；未来插件按自身需求重新选择实现 |
| 键鼠控制 `ljqCtrl` | 删除或外部插件 | 高风险、平台专属且不属于通用 Agent 核心 |
| ADB/UI 检测/OCR/视觉辅助 | 删除核心内置，候选插件 | 专项自动化能力，不应放在 `memory/` 或基础包中 |
| 进程内存扫描 | 删除 | 高风险、强平台相关且偏离轻量 Agent 基座 |

### 9.4 会话与用户工作流

| 当前功能 | 初步建议 | 理由 |
|---|---|---|
| 新建、重命名会话 | 保留 | `/new` 创建新会话，`/rename` 设置标题；运行时会话列表、恢复和切换不再作为斜杠命令 |
| 会话持久化和恢复历史 | 保留并重做 | 使用正式 `SessionStore` 和 `/resume`，完整恢复会话文件，不再解析响应日志或降级为摘要 |
| 停止当前任务 | 保留并重做 | 使用统一取消令牌，关闭 HTTP 流、工具和子进程；交互入口采用两次 `Esc`，不再占用斜杠命令 |
| 会话分支 `/branch` | 保留简化版 | 首期只复制当前完整会话为新的独立 JSONL 文件；暂不支持从任意历史节点分支，也不建设共享会话树 |
| 回退 `/rewind` | 保留无参数选择界面和破坏性截断 | 用户执行命令后从可读的历史指令列表选择恢复点；首期只回退会话，不回滚工作区文件，也不建立隐式分支 |
| `/clear` | 保留并改变语义 | 采用方案 A：原子重写当前会话文件，只保留新的 `session_meta`，清空全部 TurnEntry、Provider 上下文和 TUI 显示 |
| 导出回复 `/export` | 删除首期命令 | 不属于核心交互闭环；后续可由独立 CLI 子命令或插件提供 |
| 恢复原始模型日志 `/restore` | 删除 | 正式会话存储建立后不应解析调试日志恢复 |
| `/continue` 的日志解析兼容层 | 删除现有实现 | 新版统一使用 `/resume` 读取正式会话文件；是否保留 `/continue` 临时别名待迁移阶段决定 |
| `/btw` 不打断主 Agent 的旁路提问 | 保留并重做 | 用户明确需要旁路提问；应基于标准化会话快照和独立请求实现，禁止复制 Provider 私有历史或 monkey patch |
| 图片/文件粘贴与输入 | 延期 | 当前任务图片链路并未完整接通；基础文本 Agent 稳定后再做 |
| 会话命名 sidecar | 删除现有实现 | 名称应是会话元数据的一部分 |

### 9.5 记忆与知识

| 当前功能 | 初步建议 | 理由 |
|---|---|---|
| 当前任务短期状态 | 由标准 Conversation/TaskState 取代 | 这是执行状态，不再称为记忆，也不由工具写 `working` 字典后每轮拼接提示词 |
| 全局长期记忆文本 | 删除 | 当前阶段不需要长期记忆，不默认注入、不保留插件接口占位 |
| SOP 文件库 | 从运行时记忆系统删除 | 普通项目文档可继续存在，但核心不负责检索、归档或自动拼接 |
| L1-L4 分层记忆与原始会话压缩 | 删除 | 当前规则、脚本和定时归档复杂，偏离轻量核心目标 |
| 记忆访问统计 | 删除 | 不属于核心目标 |
| 自动长期记忆固化 | 删除 | 不进入当前核心，也不为尚未确认的未来需求预留复杂抽象 |
| 启动上下文文件 | 简化保留 | 每个新对话只加载全局 `AGENTS.md` 和当前工作区项目级 `AGENTS.md` |
| Skill 搜索 | 候选插件 | 有独立价值，但不是 Agent Loop 必需能力 |

### 9.6 前端与接入

| 当前功能 | 初步建议 | 理由 |
|---|---|---|
| 简洁 CLI REPL | 保留并重写 | 调试和最小运行入口 |
| Textual TUI | 保留一个实现并瘦身 | 主交互前端；删除 `tuiapp.py`/`tuiapp_v2.py` 双实现 |
| Streamlit 两套 Web UI | 删除 | 已确认第一阶段只保留 CLI/TUI |
| Qt 桌面端 | 删除 | 体量大、依赖重、偏离轻量目标 |
| WebView/Tauri 桌面桥接 | 删除 | 重复桌面入口和服务层 |
| Hub/启动器 | 删除 | CLI 已能承担入口和诊断职责 |
| 桌面宠物与皮肤 | 删除 | 与 Agent 基座无关 |
| Telegram、Discord、飞书、钉钉、企业微信、QQ、微信 | 从主仓核心删除，保留 Frontend Adapter 契约 | 第三方通讯平台已确定为候选扩展，可日后独立包接入 |
| ACP Bridge | 延期为可选适配器 | 有集成价值，但不是第一阶段必需入口 |
| Conductor 多 Agent Web 管理 | 删除 | 偏离单用户轻量基座，且引入服务端和全局状态 |

### 9.7 自治、调度与多 Agent

| 当前功能 | 初步建议 | 理由 |
|---|---|---|
| `--task` 文件目录任务协议 | 删除现有实现 | 依赖轮询文件和临时目录；未来如需非交互运行，提供正式 `ga run` |
| 通用 `--reflect` 动态脚本模式 | 删除 | 动态加载、轮询和热重载边界模糊 |
| autonomous 自治模式 | 删除 | 当前只是持续提示词触发，安全和停止边界不足 |
| goal mode | 删除 | 用户确认该功能非必要，当前各类实现存在稳定性问题；不进入首期核心，也不提前保留专门兼容层 |
| goal hive | 删除 | 多目标蜂群和隐式协作超出第一阶段范围 |
| scheduler 定时任务 | 删除 | 应由操作系统任务计划或独立扩展负责 |
| agent team worker / BBS | 删除 | 多 Agent 协作不是第一阶段目标 |

### 9.8 工程辅助与可观测性

| 当前功能 | 初步建议 | 理由 |
|---|---|---|
| 结构化日志 | 保留 | 调试、审计和稳定性基础 |
| 原始请求/响应日志 | 简化保留，默认脱敏 | 不再承担会话存储职责，禁止泄露 API Key 和敏感内容 |
| Langfuse 追踪 | 候选 Event Plugin | 删除 monkey patch，未来通过正式事件订阅接入 |
| 使用量事件 | 保留契约 | Provider 返回 usage 时发事件；费用展示可延期 |
| `ga config` | 保留并重写 | 首次引导和后续设置入口 |
| `ga doctor` | 新增 | 检查配置、权限、依赖、API 连接和用户目录 |
| `ga status` 进程扫描 | 删除或延期 | 当前按命令行字符串扫描不可靠，单机 CLI/TUI 不必优先实现 |
| `ga update` 内部执行 Git/Pip | 删除 | 更新交给所选包管理器，避免应用自行修改安装环境 |

### 9.9 建议的第一阶段最小产品

第一阶段只要求形成以下闭环：

```text
一条命令安装
-> 首次 JSON 配置引导
-> CLI / TUI 提交任务
-> 单一当前 Provider 调用模型
-> 流式结构化事件
-> 文件读取、写入、局部修改和命令执行
-> 工具参数校验与权限审批
-> 标准会话保存、恢复和模型切换
-> 当前模型有限重试、取消和结构化错误
```

浏览器、长期记忆、通讯平台、追踪和 Skill 搜索作为第二阶段候选插件；自治、调度、目标蜂群、桌面端和计算机控制默认删除。

## 10. 尚未授权的事项

- 当前仍处于讨论和方案起草阶段，尚未授权重写业务代码。
- 已决定不在旧核心文件中原地重构，而是在同一仓库建立独立新核心；是否保留任何面向用户的兼容入口仍待讨论。
- 尚未决定删除旧前端的具体时间和文件范围。
- 尚未形成最终重构工期估算。
- 当前草案只是增量决策记录，不作为实施规范。所有准备项确认后，第一项正式工作必须是基于草案编写结构统一、去重且可验收的《Mica 项目重构方案》；该方案经用户批准前仍不得开始业务代码实现。
- Mica 正式重建阶段启用 cAc 协作：当前 Codex 是 master，负责方向、计划、契约、派工、审查、文档和最终集成；外部 Claude Code 是 worker，按可复制任务单执行边界明确的实现、验证或审查任务。除非用户另行要求，master 不使用当前对话的内置 subagent 代替外部 worker。

## 11. CLI/TUI 交互与命令系统

### 11.1 已确认

- 斜杠命令是 CLI 工具的重要组成部分，新增命令必须低成本、可发现、可补全。
- 每个斜杠命令只声明并注册一次；帮助、补全、参数校验和分发均从注册信息生成，新增命令不修改前端主循环中的多份列表或判断分支。
- `/btw` 保留，用于在主 Agent 运行期间发起不打断主任务的旁路提问。
- `/branch` 保留，首期从当前会话复制出一个独立的新会话文件；从任意历史节点分支属于后续增强。
- `/rewind` 保留且不接受编号参数；执行后打开历史选择界面，由用户选择恢复点。
- `/rename [title]` 保留，用于设置当前会话标题；标题必须写入会话元数据并显示在 `--resume` 的会话选择列表中。
- 会话选择和恢复在程序启动阶段通过 `--continue` / `--resume` 完成，不再提供 `/sessions`、`/resume` 或 `/switch` 斜杠命令。
- `/clear` 清除当前会话的全部内容，相当于在当前入口重开一个空会话，不只是清除显示。
- `/config` 保留，但只接受无参数形式，并打开当前会话的弹出式设置界面；不提供 `/config set key value` 等快速修改语法。
- `/config` 弹窗可以编辑当前会话配置；权限模式只读显示，切换入口固定为 `Shift+Tab`。
- 关闭会话不再设置为斜杠命令：输入框为空且会话空闲时，双击 `Ctrl+C` 关闭程序。
- 输入框有内容时，单击 `Ctrl+C` 清空整个输入框，不执行退出。
- Agent 运行中，双击 `Esc` 取消当前 Run，保留会话内容。
- 会话空闲、等待用户指令时，双击 `Esc` 打开 `/rewind` 历史选择界面。
- 任意弹窗或选择器打开时，单击 `Esc` 只关闭当前界面，不触发取消、退出或回退。

首期确认保留的斜杠命令：

- `/help`
- `/new`
- `/rename [title]`
- `/branch [title]`
- `/rewind`
- `/clear`
- `/btw <question>`
- `/model`
- `/config`
- `/compact`

明确不保留：`/sessions`、`/switch`、会话内 `/resume`、`/close`、`/stop`、`/status`、`/cost`、`/export`、`/permissions`、旧日志式 `/restore` 和旧 `/continue`。启动参数 `--resume` 继续保留，它与已删除的斜杠命令 `/resume` 不是同一入口。旧 `/llm` 由 `/model` 替代。`/quit` 同样删除，使用双击 `Ctrl+C` 关闭程序。

### 11.2 目标契约

```text
新增命令
  -> 新增独立命令模块
  -> 一次显式注册到 CommandRegistry
  -> 自动进入帮助、补全和冲突检查
  -> 核心循环与前端主逻辑无需修改
```

命令元数据至少包括：名称、用途描述、参数解析、执行处理器、补全候选和适用前端。命令处理器只能通过公开的 Application/Session 服务操作状态，不能直接访问 Provider 私有历史或 TUI 控件。

快捷键采用独立的 `KeyBindingRegistry`。每个快捷键声明按键序列、适用界面状态、优先级和处理器；输入框、弹窗、会话空闲和 Agent 运行状态由注册表统一解析。新增快捷键只新增注册项，不在主窗口继续堆叠按键判断分支。

### 11.3 `/config` 弹出式设置界面

`/config` 的交互目标是“进入一个设置层后集中修改”，而不是把配置项编码成命令参数。首期界面采用紧凑的单窗口设置面板：

- 顶部显示 `/config` 和“当前会话”作用域。
- 设置按 `agent`、`runtime`、`ui`、`session` 分组，使用统一的名称列和当前值列。
- 提供搜索输入框；搜索只过滤设置项，不触发网络探测或模型请求。
- `model` 可以从设置面板进入已有的模型选择器；`/model` 只是该选择器的快捷入口，不维护第二套模型切换逻辑。
- `permissions.mode` 在面板中显示当前值但不可直接编辑，避免形成第二个权限切换入口。
- 设置值在面板内编辑并立即校验；校验通过后只写入当前会话的临时配置，不自动覆盖 `config.json`。
- 关闭面板不改变会话历史；非法值必须保留在编辑状态并显示明确错误，不能静默回退到默认值。
- TUI 使用模态覆盖层并锁定焦点；CLI 使用相同的设置服务和事件协议，以交互式设置面板作为无浮层后备呈现。

首期不复制参考界面中的 `Usage`、`Stats` 等信息页；这些信息已有状态栏或日志/诊断能力承载，避免把设置入口再次膨胀成控制中心。

## 12. `pi` 设计参考

`pi`（pi-mono，当前仓库迁移到 `earendil-works/pi`）作为设计参考，不是实现目标。其官方资料显示：核心保持极小，默认只提供 `read`、`write`、`edit`、`bash` 四个工具；扩展通过注册工具、命令、Provider 和事件接入；会话采用 append-only JSONL 树，节点使用 `id`/`parentId`，当前位置由 leaf 表示。

适合借鉴：

- 小而稳定的 Agent Loop、Provider、Session、Frontend 分层。
- 命令注册表统一处理内置命令和扩展命令，并自动生成帮助与补全。
- 结构化会话文件支撑恢复、独立分支和可审计历史；第一阶段 `/rewind` 采用直接截断，不照搬 Pi 的非破坏性树回退。
- 以事件流连接核心与 CLI/TUI，支持取消、旁路输入和后续 SDK/RPC。
- 非必要能力放入扩展，而不是继续堆进核心工具集。

不直接照搬：

- `pi` 的核心不提供权限弹窗，而本项目必须把权限策略作为一等核心能力。
- `pi` 不提供目标调度模式；本项目同样不把目标模式纳入首期核心，未来如需要通过独立扩展重新设计。
- `pi` 的模型目录主要依赖内置目录和更新机制；本项目仍按已确认要求，在 `/model` 时实时探测当前供应商模型 ID 与最大上下文。
- `pi` 的 TypeScript 扩展默认拥有完整系统权限；本项目的 Python 扩展必须有显式注册、能力声明和权限边界，不能默认执行不受信代码。

## 13. 本轮已确认的功能边界

### 保留

- Agent Loop、多轮执行、流式输出、原生 Tool Calling。
- 文件读取、文件写入、局部修改、受权限控制的命令执行和人机提问。
- 当前模型有限重试、结构化错误、取消机制。
- JSON 配置、首次配置向导、CLI、TUI。
- 会话持久化、完整 `--resume` / `--continue`、`/rename [title]`、模型切换时保留上下文、`/btw`、`/branch`、`/rewind`。
- 普通多轮会话；目标驱动调度模式已从首期删除，未来作为独立扩展候选。
- 启动时加载全局 `AGENTS.md` 和当前工作区项目级 `AGENTS.md`。

### 删除或移出底层

- 浏览器操作及其 CDP/HTML 清理链路。
- 复杂长期记忆、L1-L4 体系、SOP 自动记忆、记忆统计和自动固化。
- `/close`、`/stop` 这两个斜杠命令；分别由双击 `Ctrl+C`、双击 `Esc` 替代。
- Streamlit、Qt、WebView/Tauri、Hub、桌面宠物、Conductor。
- 多供应商同时路由、自动故障转移、自治/调度/多 Agent 协作等旧模式。

### 候选扩展

- 第三方通讯平台、ACP、Langfuse、Skill 搜索、浏览器/MCP 能力。

## 14. 讨论执行规则

- 不直接开始大规模业务代码重写；先完成当前队列项目的设计确认。
- 每个项目必须记录：目标、当前项目事实、拟议方案、替代方案、最终取舍和验收条件。
- 新结论写入对应章节，并同步更新第 8 节的唯一讨论队列状态。
- 如果新结论推翻旧结论，保留变更理由，不在不同章节保留互相冲突的规则。
- 当前正在处理第 9 项“迁移、删除与验收”。项目名称、同仓新核心、包结构和职责边界已确认；下一子项只讨论会影响整个项目的技术标识、技术基线和兼容边界。
- 第 9 项全部确认后，先把本草案提炼为正式《Mica 项目重构方案》并单独验收，再制定实施计划；禁止直接把增量草案当作施工清单。
- 正式实施采用 cAc master/worker 链路：master 先确定阶段目标、跨模块契约、修改边界和验收方式，再向外部 Claude Code worker 输出可复制任务单；master 审查 worker 报告和实际改动后决定验收或返工。
- 当前草案和正式项目方案只固定长期边界与跨阶段契约，不提前规定每个阶段的文件清单、类名、函数签名、任务顺序或测试用例。每个阶段开始前再根据当时源码和目标编写阶段计划，允许在不破坏已确认全局边界的前提下调整实现。

## 14.1 启动时会话恢复参数

### 已确认

CLI 和 TUI 都需要支持同一组启动参数：

```text
ga cli --continue
ga tui --continue
```

`--continue` 的行为：

- 在当前工作区的会话目录中寻找最后一次活跃的会话。
- 按 `updated_at` 或等价的最后活动时间判断“上一次会话”。
- 找到后完整恢复该会话，不修改历史内容。
- 找不到可恢复会话时，启动一个新会话并给出明确提示。

```text
ga cli --resume
ga tui --resume
```

`--resume` 的行为：

- 只列出当前工作区下的会话记录，不混入其他工作区的会话。
- 按最后活动时间倒序排列。
- 有标题时显示：标题 + 会话 ID。
- 没有标题时显示：第一条用户指令的节选 + 会话 ID。
- 由用户选择一条记录后完整恢复。
- 不自动根据会话内容生成标题。

启动参数与当前会话命令的职责不同：

```text
--continue / --resume
    启动程序时决定加载哪个会话

/new、/clear、/branch
    程序已经启动后创建或重置当前会话
```

当前源码尚未实现这些参数：`ga_cli` 会把额外参数转发给子进程，但旧 TUI 和 `tuiapp_v2.py` 的参数解析器没有恢复选项，`agentmain.py` 也没有读取正式会话文件的启动逻辑。

## 16. 常驻会话状态栏

### 16.1 已确认方向

CLI/TUI 应在输入区域下方提供一条常驻、低干扰的状态栏，集中显示当前会话运行所需的关键状态。视觉布局参考用户提供的示例，但不复制具体品牌、颜色或图标。

首期字段：

```text
模型及上下文上限
· 当前工作区/项目名
· Git 分支和工作区变更状态
· 上下文使用量（百分比 + 已用 token）
· 当前任务耗时
· 权限模式
```

示意：

```text
model  context  ·  project  ·  git-branch dirty  ·  5.1% / 51.3k tokens  ·  1s  ·  default
```

### 16.2 数据来源与刷新

- 模型和上下文上限来自当前 `ModelProfile`，模型切换后刷新。
- 项目名来自当前工作区根目录，切换工作区或会话后刷新。
- Git 分支和 dirty 状态来自当前工作区；不应在每次渲染时执行 Git 命令，采用缓存和低频刷新。
- 上下文使用量由核心会话/Provider Usage 事件提供；不能继续只依赖 TUI 对 `backend.history` 的字符数猜测。
- 当前任务耗时在任务开始和结束时记录，运行中按固定间隔更新。
- 权限模式来自当前会话有效配置，临时切换后立即刷新。

### 16.3 响应式规则

- 状态栏必须单行、不换行、不推动输入框高度变化。
- 窄终端优先保留模型、项目、上下文百分比和权限模式。
- Git 详细信息、token 绝对值和耗时可以按优先级隐藏，但不能遮挡输入框。
- 状态栏是只读展示，不再通过 `/status` 查看基础运行状态；`/status` 首期删除。

### 16.4 会话配置与权限快捷键

- `/config` 是当前会话的弹出式配置查看和编辑入口，不承担首次安装向导或用户级默认配置文件管理。
- `/config` 默认展示当前会话的有效配置，包括模型、思考强度、工作模式、权限模式、界面偏好、重试和超时参数。
- `/config` 不接受键值参数；修改必须在设置面板内完成，并在当前会话范围内立即生效。
- `/config` 中的权限模式为只读状态；权限模式的唯一切换入口是 `Shift+Tab`。
- `Shift+Tab` 按 `default -> accept_edits -> solo mode -> default` 的顺序循环；空闲时立即生效，运行中的 Agent 循环结束后才提交并刷新状态栏。
- `Shift+Tab` 不允许 Agent 自己触发，只能由用户输入触发。
- `Shift+Tab` 不进入 `danger`；`danger` 不属于首期实现。
- 模式切换只改变当前会话的临时权限策略，不自动修改 `config.json`；会话结束时保存最后生效的模式，恢复该会话时一并恢复。
- 当前 `tuiapp_v2.py` 只绑定普通 `Tab` 进行命令补全，没有占用 `Shift+Tab`，因此该快捷键可以作为新的权限模式入口。

## 15. 会话模型讨论基线

### 15.1 当前源码事实

- `frontends/tuiapp_v2.py` 的 `AgentSession` 是一个 TUI 运行时容器，混合保存 Agent 实例、线程、任务状态、输入框状态、流式缓冲和 `ChatMessage` 显示列表；它不是持久化会话模型。
- `ChatMessage` 同时包含用户可见内容和 Textual Widget 缓存、折叠状态、选择器回调等界面状态，不能作为核心上下文数据。
- `agentmain.GenericAgent` 把完整 LLM 历史放在当前 Provider 的 `llmclient.backend.history` 中；切换模型和 `/branch` 都通过复制这个私有字段保持上下文。
- `/rewind` 直接按 `backend.history` 中的 user 消息定位截断点，然后原地删除后续历史；这会破坏原分支，无法审计或恢复。
- `/continue` 通过扫描 `temp/model_responses/model_responses_*.txt`，解析 `=== Prompt ===`/`=== Response ===` 文本块恢复历史；`session_names.json` 只是日志文件名到显示名称的旁车文件。
- 日志本来用于调试，却被同时当作会话存储、恢复来源、侧栏预览和跨进程会话发现依据。

### 15.2 当前问题的直接后果

- UI 状态、模型协议状态和持久化状态互相泄漏，任何一层格式变化都可能破坏其他功能。
- Provider 私有消息格式成为会话格式，切换 Provider 或模型时必须复制内部字段。
- `/branch` 当前只能做整段深拷贝；首期将明确保留这种简单行为，不增加任意节点分支。
- `/rewind` 是破坏性截断，不是真正的回退。
- 文本日志解析依赖正则、`ast.literal_eval` 和当前日志格式，日志格式变化会导致恢复失败或降级成摘要。

### 15.3 初始模型提案（已被本轮粒度决策细化）

以下是设计提案，不是已确认实现：

```text
SessionStore
  -> 负责会话文件、追加、截断、恢复和独立复制

Session
  -> id, title, workspace, created_at, updated_at

TurnEntry
  -> id, user_input, assistant_output, complete_execution_record, model_snapshot, created_at
```

### 15.4 本轮已确认的会话决策

- 每个持久化会话使用一个独立 JSONL 文件。
- 会话文件不能无限期保留；用户可以在 `config.json` 中设置“文件不活跃多久后清理”的时限。
- 清理策略必须基于最后活动时间，而不是文件创建时间；配置键暂定为 `sessionCleanupDays`，默认值为 `30`。程序启动时检查一次，长时间运行时最多每 24 小时检查一次；当前正在使用或被锁定的会话不清理，清理失败不阻止 Agent 启动。
- `/branch` 复制当前完整会话并创建新的独立会话文件，不与父会话共用持久化数据；首期不支持从任意历史节点分支，也不与 `/rewind` 组成树形操作系统。
- `/rewind` 不接受序号或节点 ID 参数。执行后打开弹出式历史选择界面，按时间顺序展示每次用户指令的节选，并提供 `(current)` 选项；用户使用方向键选择，`Enter` 确认，`Esc` 取消。
- 选中某条用户指令的含义是“恢复到执行该指令之前”：删除被选中指令及其后的全部 `TurnEntry`。选择 `(current)` 或按 `Esc` 不修改会话。
- `/rewind` 第一阶段只回退会话内容和模型上下文，不恢复 Agent 已修改的文件。界面必须明确显示 `Conversation only` 或等价提示，禁止暗示代码也会回滚。
- `/rewind` 仍采用破坏性截断，不自动保留隐藏分支，也不维护 leaf 指针。弹窗展示用户可读节选，内部通过稳定的 `entry_id` 精确定位，用户不需要接触内部编号。
- `--resume` 必须从正式会话 JSONL 文件完整恢复此前保存的会话信息，禁止摘要替代、静默裁剪、格式降级或修改原内容。
- 当前会话支持 `/rename [title]`；标题写入该会话文件的元数据，并用于 `--resume` 列表展示和选择。
- `/clear` 采用方案 A：保留当前会话身份和标题，原子重写 JSONL 文件，只写入新的 `session_meta`，删除全部 TurnEntry，并清空 Provider 上下文和前端显示。
- `/btw` 默认读取主会话的只读上下文快照，结果不写入主会话。
- `/btw` 是一次性请求；默认回答完成后旁路请求结束，用户不能继续向这个旁路请求追加问题。
- 会话树的节点粒度是用户每一次提交的指令，而不是每一次 Provider 消息、工具调用或工具结果。
- 工具调用和工具结果不作为独立会话节点，完整保存在所属用户指令的执行记录中；这不改变它们不是树节点的决定。

### 15.5 调整后的待讨论模型

建议把一次用户指令视为一个完整的 `TurnEntry`：

```text
SessionMeta
  session_id
  title
  workspace
  created_at
  updated_at

TurnEntry
  id
  user_input
  assistant_output
  complete_execution_record
  model_snapshot
  created_at
  last_activity_at
```

会话文件第一条记录保存 `SessionMeta`，之后每条记录对应一次用户指令。工具调用、工具结果、模型中间消息和必要协议信息不单独成为会话节点，但必须完整保存在所属 `TurnEntry.complete_execution_record` 中，确保 `/resume` 能原样恢复完整上下文。

建议的操作语义：

- `append(entry)`：在当前会话文件末尾追加一个完整 `TurnEntry`。
- `rewind(target_entry_id)`：由选择界面传入内部 ID，原子重写会话文件，删除目标节点及其后的全部 `TurnEntry`；该 ID 不属于斜杠命令的用户参数。
- `branch(title)`：复制当前完整会话为新的独立文件；复制完成后两个会话互不影响。
- `rename(title)`：原子更新 `SessionMeta.title`，同时更新 `updated_at`。
- `build_context()`：按文件顺序读取所有 `TurnEntry`，重建完整标准化消息历史，再交给 Provider 转换。
- `resume(session_id)`：完整加载会话文件，不解析调试日志，不做摘要降级。

`/btw` 如何在不复制 Provider 私有历史的前提下构建只读快照，以及“完整执行记录”的供应商无关格式，进入 Provider 适配层讨论时确定。

### 15.6 会话 JSONL 状态

第 4 项“会话 JSONL”已确认。具体文件字段和执行记录的最终 schema 将在实现前根据 Provider 的供应商无关消息契约定稿，但不得改变本节已经确认的用户行为和持久化边界。

## 17. 迁移、删除与验收

### 17.1 已确认：同仓建立新核心

- 不在旧 `agent_loop.py`、`agentmain.py`、`ga.py` 等核心文件中继续原地重构。
- 在当前仓库创建独立的新版 `src/` 包；旧代码暂时冻结，只在核对行为或提取必要知识时读取。
- 新版不以兼容旧项目内部接口为默认目标，只实现已经确认保留的用户功能和公开行为。
- 删除功能采用“不迁入新核心”的方式处理，不先为浏览器、复杂记忆、Goal Mode、旧前端等已删除能力编写兼容层。
- 按端到端可运行切片推进，每个阶段都必须能独立验证，而不是先创建大量空模块后集中联调。
- 新核心完成验收后，再删除旧代码、切换正式入口并完成项目命名相关标识的统一替换。
- 第一条 Provider 链路直接使用用户提供的真实 API 服务和测试 Key 进行集成验收，不在 Mica 产品代码中实现 `FakeProvider`。
- 测试 Key 只通过本机环境变量或 Git 忽略的本地配置注入，不写入项目计划、worker 任务单、日志、会话文件或版本控制。

新版使用 `src` 布局，最终职责边界如下；目录只在开始实现对应能力时创建，不预先生成空包：

```text
src/mica_agent/
  __init__.py
  __main__.py
  app.py
  engine.py
  events.py
  models.py
  config/
  providers/
  sessions/
  tools/
  permissions/
  commands/
  extensions/
  frontends/

tests/
pyproject.toml
README.md
```

- `app.py`：应用装配和统一入口，连接配置、会话、Engine、Provider、工具与权限；CLI/TUI 只调用 `MicaApp`。
- `engine.py`：只负责编排模型调用、工具调用和 Run 生命周期，不读取配置、不持久化会话、不渲染界面。
- `events.py`：定义前端消费的结构化运行事件，替代旧 `display_queue` 的松散字典协议。
- `models.py`：只保存确实跨多个模块共享的供应商无关数据，禁止演变成杂物文件。
- `config/`：配置合并、校验和首次向导，不负责创建 Provider 或执行连接请求。
- `providers/`：Provider 接口、注册和协议适配；首期实现 OpenAI-compatible Provider，切换 Provider 不改变标准会话数据。
- `sessions/`：会话模型、JSONL 存储和上下文构建，Provider 不再拥有会话真相。
- `tools/`：`ToolSpec`、注册、执行及六个内置工具；所有工具调用统一经过 `ToolExecutor`。
- `permissions/`：内置模式矩阵和统一 `PolicyEngine`，具体工具不自行决定授权。
- `commands/`：斜杠命令单次注册、帮助、补全、校验和分发。
- `extensions/`：显式扩展 API 和加载入口；禁止 monkey patch、导入副作用和访问私有状态。
- `frontends/`：只保留 CLI 和 TUI；两者共享 `MicaApp` 与事件协议，但互不导入。

依赖方向固定为 `CLI/TUI -> MicaApp -> AgentEngine`。Engine 只依赖 Provider 契约和 `ToolExecutor`；`ToolExecutor` 统一依赖 `ToolRegistry` 与 `PolicyEngine`。Engine 不导入前端，Provider 不读取界面状态，工具不修改会话历史，SessionStore 不保存供应商私有对象，扩展不修改 Engine 内部实现。

### 17.2 待讨论

- Mica 与 Python 分发名、Python 导入包名、npm scope、终端命令和用户数据目录的映射。
- 项目级技术基线，例如 Python/异步运行模型、CLI/TUI 基础框架和依赖控制原则；具体库的局部用法在对应阶段确认。
- 旧配置、会话数据和用户命令的兼容或放弃范围。
- 项目级测试与验收原则；具体测试用例和阶段失败回退方式在对应阶段计划中确认。
- 旧模块的最终删除顺序和发布收尾流程。
- `/compact` 等已经保留但尚未确定长期数据契约的能力，只确认会影响会话格式的核心语义；具体交互和算法在对应阶段确认。
