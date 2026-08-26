# P5-01 公共工具、交互与取消契约

## 目标

建立后续工具系统共享的供应商无关公共对象、事件和取消信号，同时保持现有纯文本消息与会话 v1 行为兼容。

## 公共契约

- 工具：`ToolSpec`、`ToolCall`、`ToolRequest`、`ToolResult`、`ToolResultStatus`。
- 权限与交互：`PermissionMode`、`PolicyDecision`、`PermissionAnswer`、`PermissionResponse`、`UserInputResponse`。
- 取消：`CancellationToken`、`CancellationRequested`。
- 事件：`ToolStarted`、`ToolFinished`、`PermissionRequested`、`UserInputRequested`，以及 `ErrorCategory.LIMIT`。
- 消息：保留 `Message(role, content)`，新增 assistant `tool_calls` 与 tool `tool_call_id`。

## 关键决定

JSON object 输入在构造时递归复制为只读 mapping，并把数组复制为 tuple，形成不受原始 dict/list 后续修改影响的深层不可变快照。校验只报告字段规则，不回显原始输入。

`CancellationToken` 使用一次性异步事件；重复取消幂等，先取消后等待立即完成，所有活动等待者同时唤醒，取消异常使用固定安全文案。

## 验证与边界

目标测试、旧 session/provider/engine/app 兼容性测试及全量质量检查均通过。P5-01 未实现注册表、Schema 参数验证、权限算法、真实工具、Provider tool calling、Engine 工具循环、会话 v2 或 CLI 集成，这些仍属于 P5-02 及后续任务。

剩余风险：公共事件只约束可携带字段，后续事件生产者仍必须在进入事件前生成固定或已清洗的安全说明。

# P5-02 工具注册、参数校验与权限策略

## 目标

建立内存 `ToolRegistry`、标准 JSON Schema 参数校验和只接收规范化事实的 `PolicyEngine`，同时补齐 P5 不可变 JSON 快照到普通 JSON 值的集中转换入口。本任务不执行任何真实工具。

## JSON Schema 依赖审计

- `jsonschema` 是成熟的标准实现，支持 Draft 3、4、6、7、2019-09 和 2020-12，提供 Meta-Schema 自检；当前版本支持 Python 3.11，MIT 许可证，Windows CPython 有现成 wheel。它有 `attrs`、`jsonschema-specifications`、`referencing` 和 `rpds-py` 四个合理的运行时依赖；常规 Windows 安装不需要编译环境，但缺少 wheel 的源码安装环境需要为 `rpds-py` 准备 Rust 工具链。
- `fastjsonschema` 更轻，BSD-3-Clause、纯 Python 且无运行时依赖，但只支持 Draft 04、06、07，采用代码生成，默认值行为还可能修改待验证数据，不适合作为 Mica 的长期标准边界。
- 手写 P5 子集不增加依赖，但会重复实现类型、组合和 Meta-Schema 细节，容易形成不完整却被误认为完整标准的校验器，维护和安全风险最高。

最终只增加直接依赖 `jsonschema>=4.25.1`，锁文件解析为 4.26.0，并固定使用 Draft 2020-12；没有 `$schema` 的 ToolSpec 也按该 draft 处理。验证器使用禁止远程获取的空引用注册表，默认测试和参数校验不会访问网络。第三方 Schema/参数异常统一转换为 Mica 固定错误，不向调用方暴露原始异常或数据。

## 公共行为与边界

`ToolRegistry` 在写入前验证 `ToolSpec`、handler callable 和 Schema；重复名称不会覆盖第一次注册。`specs()` 返回仅含 `ToolSpec`、按名称排序的 tuple 快照；`resolve()` 返回未来 `ToolExecutor` 使用的不可变注册项，未知名称使用固定错误且不回显名称。注册和查询都不执行 handler。

`validate_tool_schema()` 区分开发者提供的无效 Schema；`validate_tool_arguments()` 区分可恢复的模型参数错误。required、类型、嵌套 object、array、enum、`additionalProperties` 和 bool/integer 边界由 Draft 2020-12 校验，且验证只消费独立普通 JSON 副本，不修改请求快照。

`to_mutable_json()` 是 P5 不可变 JSON 快照的唯一普通 JSON 转换入口：mapping 复制为 dict、tuple 复制为 list、标量保持 JSON 类型，结果可直接交给标准编码器，且修改结果不会影响原快照。

`PolicyEngine` 的顺序固定为 hard deny、当前调用的一次性授权、注入 allow rule、权限模式矩阵。hard deny 不能被任何后续分支覆盖；规则异常和非 bool 真值均不会放行。DEFAULT 只自动允许 read/list/ask；ACCEPT_EDITS 另允许 write/edit；SOLO 再允许明确标记的安全只读命令，但普通 command 和 other 在所有相应默认分支仍为 ASK。一次性授权不保存、不缓存，也不影响下一次同名调用。

## 验证与未实现范围

公共目标测试和 P5-01/旧核心兼容测试覆盖注册快照、Schema 安全错误、完整权限矩阵、优先级、公共导入与懒加载；全量自动化测试、Ruff、格式检查和离线构建均通过。P5-02 未实现 ToolExecutor、真实文件或命令工具、路径/命令安全分析、Provider tool calling、Engine 工具循环、交互等待、会话 v2、配置读取或 CLI/TUI；这些仍属于 P5-03 及后续任务。

新增运行依赖后，旧分发测试中“只有 httpx”以及依赖机器 uv registry cache 的假设不再成立。经用户明确授权，分发测试改为分别验证 wheel 依赖元数据、无依赖基础 CLI 懒加载，以及 Mica wheel 在当前 uv 锁定依赖环境中的离线运行；自动化测试本身仍不联网。

剩余风险：完整 JSON Schema 支持会带来传递依赖体积；`rpds-py` 在没有对应 wheel 的平台需要编译环境。当前分发测试不再独立证明 uv 能从空 cache 离线安装全部第三方 wheel，该能力依赖发布环境准备 wheelhouse 或预热 cache。未来 Schema 若引入远程引用，当前安全边界会固定拒绝解析，而不会隐式联网。

# P5-03 工作区路径边界与文件工具

## 目标与路径边界

实现 `WorkspacePathResolver` 作为四个文件工具唯一的路径入口。它只接收相对字符串，拒绝空白、NUL、绝对路径、`..`、Windows 盘符相对路径、UNC 与设备/扩展路径；在任何文件系统操作前逐个验证组件，拒绝尾随点、尾随空格、冒号（含 NTFS 备用数据流）及 Windows 保留设备名 `CON`、`PRN`、`AUX`、`NUL`、`CLOCK$`、`COM1`～`COM9`、`LPT1`～`LPT9`，及其普通扩展形式。该规则跨平台一致。逐级检查已有路径中的 symlink、junction 和其他重解析点；写入不存在目标时同样检查已有父目录。错误固定为安全摘要，不回显工作区绝对路径、HOME、内容或底层异常。

该边界不是操作系统沙箱，也不宣称消除检查与使用之间的 TOCTOU 竞态。Windows 上真实目录 junction 测试通过；当前环境禁止普通 symlink 创建，因此该单项以明确原因跳过。POSIX 分支使用标准库的 `lstat`/symlink 检查，但未在 POSIX 主机实测。

## 敏感文件与稳定工具行为

敏感规则集中在路径模块：拒绝 `.git` 及内容、`.env` 和 `.env.*`（明确允许 `.env.example`、`.env.sample`、`.env.template`）、`.npmrc`、`.pypirc`、`id_rsa`、`id_ed25519`、`.pem`、`.key`、`credentials` 目录和 `credentials.json`。`ls` 对根目录等非敏感父目录过滤敏感子项，直接访问敏感目标则安全拒绝。

`register_file_tools(registry, workspace)` 显式、一次性注册 `read`、`ls`、`write`、`edit`；注册不触碰文件、不执行 handler，遇到已有名称时先失败而不留下部分注册。Provider 仍只从 `ToolRegistry.specs()` 取得 `ToolSpec`。

`read` 和 `edit` 复用有界二进制读取：最多请求并读取 `MAX_FILE_BYTES + 1` 字节，额外一字节即整体失败，再严格 UTF-8 解码；不会使用 `Path.read_bytes()` 读取剩余内容。`ls` 只扫描最多 `MAX_LIST_ENTRIES + 1` 个实际目录项，确认超限后不继续收集，再执行敏感项过滤和稳定排序；它只列一层、目录以 `/` 标示且空目录返回固定文字。`write` 创建或替换普通 UTF-8 文件；`edit` 仅对已有普通 UTF-8 文件进行一次唯一的普通字符串替换。四者都在访问前、结果前或替换前检查既有取消令牌。

## 限制、可靠性与验证

集中限制为 `MAX_FILE_BYTES = 262144`、`MAX_LIST_ENTRIES = 1000`、`MAX_LIST_OUTPUT_BYTES = 262144`，按 UTF-8 实际字节计量；超过时整体失败，不返回部分文件或不完整目录结果。`write` 与 `edit` 使用目标同目录临时文件、写入、flush、fsync、关闭和 `os.replace`；替换或此前步骤失败时清理临时文件，旧文件保持原字节，新目标不留下半写入。故障注入后已验证可恢复重试。

真实 Windows 回归覆盖 `.npmrc.`、`.npmrc `、`.git./config`、`.git /config`、`normal.txt:secret`、保留设备名及 NUL；四个公开 handler 都安全返回 `PERMISSION_DENIED`，敏感原始字节不变，不含 traceback、绝对路径、HOME、凭据或原始异常。测试还验证 read/edit 的单次实际读取量、读/编辑超限无部分结果或临时文件、以及 ls 在第 1001 项后停止扫描。P5-01/P5-02 契约及工作区测试保持通过。P5-03 未实现 `bash`、`ask`、ToolExecutor、权限询问、Provider 工具调用、Engine 循环、CLI/TUI 集成、会话 v2 或文件回滚。

剩余风险：文件工具是同步文件 I/O，且不提供 OS 级隔离或 TOCTOU 消除；普通 symlink 在本 Windows 权限配置下未能实际创建；POSIX 重解析点行为尚未做原机验收。

# P5-04 Bash、ask 与子进程取消

`register_runtime_tools()` 原子注册 `bash` 与 `ask`，注册阶段只校验参数和名称冲突，不发现可执行文件、不访问显式 Bash 路径、不启动进程。四个 P5-04 对象通过 `mica_agent` 顶层懒加载导出；普通导入不会加载 runtime tools、App、Engine、CLI 或 Provider。

`ask` 只通过注入的 `UserInputRequester` 请求一次用户响应，支持一般 `Awaitable`（包括 Future），不读取 stdin，也不导入 CLI/Engine/UI。缺失或无效问题、requester 异常、无效响应和 interaction ID 不匹配均转换为固定安全结果；取消令牌和外部 Task 取消会回收 requester 等待任务，外部 Task 取消仍重新抛出 `CancelledError`。

Windows 默认优先发现 Git for Windows，并支持从 PATH 中的 Git `git.exe` 推导安装目录；System32、WindowsApps、PowerShell、cmd、WSL 启动器不会被当作 Bash。Windows 使用 `--noprofile --norc -c`、独立进程组、工作区 cwd 和合并 stdout/stderr；POSIX 分支使用 `start_new_session=True`，清理时按进程组发送有限的 SIGTERM/SIGKILL。POSIX 分支未在原机验收，保留该风险。

命令使用有界流式读取，最多保留 `MAX_COMMAND_OUTPUT_BYTES + 1` 字节；精确上限成功，第一个超限字节立即触发进程树回收并返回无数据的固定失败结果。成功、空输出、Unicode 输出、无效 UTF-8 替换、合并标准错误、非零退出码、启动失败、cwd 失败、超时和取消均有公开 handler 测试。真实 Windows Git Bash 已验证 Unicode/空格工作区 cwd、环境清理、精确输出边界、无限输出终止、CancellationToken 取消、超时、输出超限和外部 Task 取消。

环境只保留普通运行所需变量；常见 API key、token、secret、password、credential 后缀变量，Bash 初始化变量以及导出函数变量均不会传入或执行。测试只读取指定哨兵变量，并验证初始化脚本、提示命令和导出函数不会创建标记文件。

Windows 真实后代测试使用准备标记确认后代已经启动，再在延迟标记写入前分别触发 CancellationToken 取消、timeout 和输出超限；三条路径均在限定时间内返回，延迟标记最终不存在，测试没有 pending task、未关闭 pipe 或 traceback 警告。Windows taskkill 超时、启动失败、非零退出和进程已退出均保持安全清理路径。

P5-04 未实现 ToolExecutor、权限判定执行、Provider tool calling、Engine 工具循环、CLI/TUI、会话 v2 或 P5-05～P5-08 能力；Bash 也不是操作系统级沙箱。

# P5-05 OpenAI-compatible 工具调用

Provider 公共契约扩展为不可变的 `ProviderRequest(model, messages, tools=())` 和
`Finish(reason=None, tool_calls=())`。两者继续使用 frozen/slots 数据对象，旧位置参数和
关键字构造保持兼容；`tools` 与 `tool_calls` 必须分别是 `tuple[ToolSpec, ...]` 和
`tuple[ToolCall, ...]`，Provider 不保存半成品公共 `ToolCall`。

请求适配只在存在工具时增加标准 `tools` 字段，工具顺序与请求快照一致；参数 Schema 通过
既有公共 `to_mutable_json()` 转换为独立的普通 dict/list，不把 handler、注册表、权限对象
或 Provider 私有对象带入请求。无工具请求保持原有 model/messages/stream 载荷，不生成
`tools` 或 `tool_choice`。assistant 工具调用消息转换为标准 `tool_calls`，参数快照以
`ensure_ascii=False`、紧凑分隔符和禁止 NaN/Infinity 的 JSON 字符串发送；tool 消息只发送
role、content 和 tool_call_id。

流式适配按 `delta.tool_calls[].index` 收集碎片，支持 ID、名称、类型和交错的 arguments
字符串分片；只有结束信号或 `[DONE]` 到达后才解析参数并构造 `ToolCall`。最终检查索引从
零连续、ID 唯一、类型为 function、ID/名称/参数完整，参数顶层必须是 JSON object，并把
所有参数快照交给公共 `ToolCall` 校验。纯文本与工具调用可以混合，文本仍按原顺序产生
`TextDelta`，完整调用只出现在一次 `Finish` 中；`finish_reason` 与 `[DONE]` 不重复结束。

工具字段不是 list、调用项或 function 不是 object、索引非法、类型错误、ID/名称冲突、索引
缺口、重复 ID、缺失字段、非法或不完整 JSON、非 object 参数、缺少工具调用的
`tool_calls` 结束原因，以及工具调用后不兼容的普通结束原因，均统一为一个终端
`ProviderErrorEvent(MalformedResponseError(...))`。公开诊断不回显工具名称、调用 ID、原始
参数、响应正文、凭据或 traceback。

重试状态在观察到文本或任意工具调用增量后立即变为已产出；此后传输中断不重试、不重复发送
工具意图。取消仍传播 `CancelledError`，HTTP response 通过现有异步上下文关闭，既有纯文本
流、错误分类、有限重试和资源生命周期保持兼容。兼容旧式结束 chunk：当 choice 缺少
`delta` 但带有非空 `finish_reason` 时，将缺失字段视为空对象；因此纯文本可以直接产生
`Finish(reason="stop")`，已完整收集的工具调用也可以直接在 `finish_reason="tool_calls"`
时组装。显式存在但为 null、数组或字符串的 `delta` 仍按畸形响应处理；同时缺失 delta
和 finish_reason 的普通 chunk 仍拒绝，不会被静默忽略。

请求测试通过真实 HTTP mock transport 捕获至少两个 ToolSpec 的 JSON，验证工具名称、描述、
Schema 和顺序均与 ProviderRequest 一致；畸形工具参数测试包含伪造的凭据样式哨兵，并验证
MalformedResponseError 的字符串和 repr 都不泄漏原始参数、调用 ID、工具名称或 traceback。
P5-05 只负责协议适配，不执行工具、不访问 ToolRegistry、不接入
Engine 循环。

# P5-06 Engine 多步工具循环

P5-06 新增 `ToolExecutor` 作为工具调用唯一入口，固定执行顺序为：查找注册工具、按
ToolSpec 校验参数、通过可注入的 Policy fact factory 构造权限事实、执行 PolicyEngine、
必要时等待一次权限响应、调用 handler、校验返回的 ToolResult，并把异常转换为安全的结构化
结果。未知工具、参数错误、硬性拒绝、用户拒绝和普通 handler 异常不会伪造 handler 开始
事件；已处理结果统一可以通过 ToolFinished 反馈给模型。handler 返回错误类型、错误调用
ID 或错误工具名时也只返回固定的执行失败结果。取消使用统一 CancellationToken，未开始的
工具不会调用 handler，执行中取消不会继续下一个工具。

`InteractionBroker` 是前端无关的交互等待边界。它只把不含 Future、Task 或 Queue 的
InteractionRequest 快照交给 Engine，支持权限和用户输入两种类型；`respond()` 只接受匹配
ID 和匹配响应类型，错误 ID、错误类型、重复响应和终态后的迟到响应均忽略。Broker 为每次
权限或用户输入生成独立、生命周期内不复用的 interaction ID；`InteractionRequest.call_id`
仍保存原工具调用 ID。ask 前端使用 Broker ID 响应，Broker 返回给既有 ask handler 的
`UserInputResponse` 时重新关联为原始 call_id，因此不修改 runtime_tools.py 的协议。取消、
外部 Task 取消、消费者提前关闭和 Run 终态都会清理 broker 等待；已使用 ID 即使 clear、
取消或完成后也不会再次接受旧响应。Engine 将权限请求映射为 PermissionRequested，将通过
既有 UserInputRequester 协议产生的 ask 等待映射为 UserInputRequested。Engine 不读取 stdin、
CLI、App、配置、HOME 或会话文件。

一次带工具的 Run 保持一组严格递增 seq：RunStarted 后，模型文本立即产生 TextDelta；工具
调用批次先追加 assistant Message（包含本步完整文本和 ToolCall 元组），再按模型返回顺序
逐个通过 ToolExecutor。允许调用才产生 ToolStarted，handler 返回后产生匹配的 ToolFinished；
权限请求一定在 ToolStarted 之前。每个 ToolResult 转换为确定性的紧凑 JSON tool Message，
字段顺序固定为 status、message、data，并使用 `to_mutable_json()` 和 `allow_nan=False`。
所有工具结果（包括未知、参数错误、权限拒绝和执行失败）都能进入下一次 ProviderRequest，
成功或失败均不会绕过模型继续请求边界。只有没有工具调用的 Finish 才产生 RunCompleted。

每个模型步骤都在 `contextlib.aclosing(self._provider_events(...))` 边界内消费 Provider
流。收到普通 Finish、tool_calls Finish、ProviderError、未知事件、取消或 Engine 提前结束
时，退出该边界后才继续产生终态、执行工具或发起下一次模型请求；底层 Provider 流的
`aclose()` 在该边界的 finally 中同步等待完成，不依赖 GC、事件循环退出或 `sleep(0)`。

ProviderRequest.tools 在每一轮保持原始不可变 ToolSpec 顺序；Engine 只维护本次 Run 的内存
Message，不修改 Provider、会话格式或 MicaApp。默认限制为最多 8 次模型请求和 32 次工具
调用，限制值必须是构造阶段验证过的正整数；工具批次超过剩余总量时在执行任何 handler
前产生 ErrorCategory.LIMIT，模型步数耗尽后不再请求 Provider。模型错误继续沿用现有
Provider 错误分类，内部异常转换为 UNKNOWN，取消只产生一个 RunCancelled。

验证覆盖单工具往返、多个工具严格顺序、模型第二轮开始前第一条流已关闭、普通 Finish
同步关闭、ProviderError 失败关闭、混合文本与工具调用、失败结果回传模型、权限和 ask
等待、独立 interaction ID、重复 call_id 的迟到响应、Engine 终态后的迟到响应、取消、
提前关闭、模型/工具上限、纯文本旧事件序列、公共懒加载导出和安全错误边界。目标组、
严格警告组、分发组和安装态稳定性复跑均通过：CLI 生命周期与 P5-06 严格组 `122 passed`，
P5-06 目标组 `343 passed`，分发组 `12 passed`，安装态 Unicode session 目标用例两次均为
`1 passed`；全量测试为 `796 passed, 1 skipped`。显式关闭测试替身 TemporaryDirectory
后，严格警告测试没有 `ResourceWarning`、pending task 或 `async_generator_athrow`。
P5-06 不实现会话 v2、MicaApp 工具装配、CLI/TUI 或会话提交；这些属于 P5-07/P5-08，尚未开始。

# P5-07 会话 v2 与完整提交

会话当前写入版本升级为 v2，reader 继续严格接受固定旧 wire 的 v1。v1 turn 保留
`user_message` 与 `assistant_message`；v2 turn 改用单个 `messages` 数组，按 USER、零个或多个
assistant tool-calls/对应 TOOL 批次、最终普通 ASSISTANT 的顺序表达完整轮次。`TurnEntry` 保留
原五参数构造，在末尾增加不可变 `intermediate_messages`，并通过 `context_messages` 提供稳定
组合视图。角色字段、tool_call 字段、JSON 类型、记录版本和跨记录版本均严格校验；版本读取先
稳定检查 `format_version` 存在性，跨记录比较不哈希不可信版本容器；不可信内容只产生不回显
记录值的 `SessionFormatError`。

Store 的正式创建、追加和重写统一生成 v2。`append_turn` 在既有单 session lock 内读取 v1、
校验、合并本次业务变更并通过同目录临时文件、flush、fsync 和 `os.replace` 一次替换；新增的
事务更新边界使 rename、rewind 和 clear 也在一次锁持有期间完成读改写。替换失败保留 v1 原
字节并清理临时文件，恢复后可重试。branch 读取源快照后直接创建 v2 分支，不改写 v1 源文件，
消息和时间戳保持不变。scan 同时列出健康 v1/v2，并把混合版本文件归入安全诊断。

Engine 增加可选同步完成转录 sink。纯文本成功传入最终 assistant；工具成功按 assistant
工具调用、匹配 TOOL 结果、后续批次和最终 assistant 传入不可变 tuple。sink 仅在
RunCompleted 前调用一次；失败、取消和消费者提前关闭不调用，sink 异常转换为唯一安全失败
终态。公共工具事件仍不携带参数或完整输出。ToolExecutor 仅通过 `tool_specs` 暴露确定性
ToolSpec tuple，MicaApp 注入 executor 后将该快照交给每轮 ProviderRequest，并通过公开
`respond_interaction()` 转交当前 Broker，不直接执行 handler 或读取终端。

工具消息清洗集中在纯函数边界：递归替换 `api_key`、`apikey`、`token`、`access_token`、
`secret`、`password`、`authorization`，并识别常见 `sk-` 与 Bearer 样式，固定替换为
`[REDACTED]`。清洗复制不可变 ToolCall/Message，不修改原对象，不处理 user 或最终普通
assistant 文本。同一份清洗后消息同时进入下一次 ProviderRequest、Engine 完成转录和
TurnEntry；直接通过 Service/Store 构造工具轮次也复用该边界。

MicaApp 不再从 TextDelta 推测持久化消息。它只在收到 Engine 完整转录且观察到 RunCompleted
后构造一个 TurnEntry，并通过一次 `commit_turn` 提交；Provider 失败、取消、等待中断、消费
者提前关闭、转录失败和存储失败均不形成半轮。未知工具、参数错误、权限拒绝或普通工具失败
若被模型恢复并最终完成，仍保存完整历史。write、edit 或 bash 已开始后若后续 Provider、
模型步数或存储失败，安全终态会提醒工具修改可能已经发生并要求检查工作区，不回显路径、
命令、工具输出或原始异常；工具副作用不承诺回滚。

目标组、严格异步组、分发回归和全量测试均通过，分别为 `289 passed`、`73 passed`、
`12 passed`、`823 passed, 1 skipped`。原子替换故障注入证明旧 v1 字节不变、无临时文件、
仍可按 v1 加载且恢复后可一次升级；跨层退出重载测试证明完整已清洗工具顺序进入下一次模型
请求和 v2 JSONL。新增公共回归证明缺失版本、数组或对象版本容器均不会泄漏 `KeyError` 或
`TypeError`，scan 能隔离损坏文件并保留健康 v1/v2 会话。剩余风险是清洗器按本阶段明确凭据键
和常见样式工作，不是通用 DLP；工具副作用仍需用户在失败提示后自行检查。P5-08 CLI 工具装配、
权限提示和安装态真实工具闭环尚未开始。

# P5-08 基础 CLI 集成与阶段收口

基础文字 CLI 在 Context/AGENTS 预检后、Provider 构造前创建单个 InteractionBroker 和
ToolRegistry，注册 read、ls、write、edit、bash、ask，并以同一 Broker 连接 ask、
ToolExecutor 和 MicaApp。Provider 只收到按名称确定排序的六个 ToolSpec。工具初始化失败时
使用固定诊断退出，不构造 Provider。

`--permission-mode` 支持 default、accept-edits 和 solo，可与新建、`--continue`、`--resume`
组合；非法值由参数解析阶段拒绝。default 直接允许 read/ls，write/edit/bash 询问；后两种
模式直接允许文件修改，bash 仍询问。权限只接受 ASCII `y`/`yes`，`n`/`no`/空输入拒绝，其他
输入固定提示后重试。ask 问题显示在标准错误，空回答也是合法工具回答，不进入普通 USER 消息。

ToolStarted 和 ToolFinished 仅输出 `tool <name>: started` 与 `tool <name>: <status>`，不展示
参数、调用 ID、完整结果或路径。权限或 ask 等待时的 EOF、Ctrl+C 和外部取消会关闭事件流、
清理交互等待并关闭 Provider，不提交半轮。消费者在 PermissionRequested 或 UserInputRequested
等待点主动 `aclose()` 的两个直接公共行为测试也已覆盖：不提交响应即可同步清空 Broker 等待
与内部异步任务；write handler 不开始、不创建目标文件，ask 等待被取消，两者均不保存半轮，
最终关闭 App 时 Provider 总计只关闭一次。Engine 对工具信号子流使用显式 `aclosing()`，保证
公共 App 流关闭返回前完成清理，不依赖事件循环关闭或异步生成器终结器。Provider 流关闭和
子进程树回收边界继续由 Engine 和 runtime tool 回归覆盖。成功工具 Run 保存完整 v2 工具转录，
后续请求和 `--continue` 恢复 USER、ASSISTANT(tool_calls)、TOOL、最终 ASSISTANT 的顺序；工具
副作用后的后续失败不保存半轮，并提醒用户检查工作区。

仓库外安装 wheel 在隔离 HOME、Unicode 和空格工作区中完成真实 loopback 闭环：default 模式
授权 write 后创建唯一目标文件、保存一条完整 v2 TurnEntry，第二次 `--continue` 恢复工具历史
并保存第二轮，未重新执行 write，JSONL 仅位于临时 HOME。输出和 JSONL 不包含测试 API Key、
工具参数敏感哨兵或工作区绝对路径。Windows 安装态由真实自动发现的 Git Bash 执行无副作用、
有限输出命令，确认 Unicode/空格 cwd、权限询问、合并输出、最终回答和进程退出均正常；未使用
PowerShell 代替 Bash。

最终自动化结果为：P5-08 集成目标文件 `23 passed`，CLI/P5-08 严格警告组 `64 passed`，工具、
Policy、Executor、App 与 Engine 严格组 `200 passed`，分发组 `14 passed`，全量
`850 passed, 1 skipped`；Ruff、格式、离线构建
和 diff 检查均通过。受控真实 OpenAI-compatible Provider 冒烟也已完成：首轮模型请求 write，
default 模式询问并授权后创建预期临时文件、返回最终回答并退出；第二次 `--continue` 恢复完整
历史，模型请求无需授权的 read 并返回最终回答。隔离 HOME 中恰好保存两个完整 v2 工具 Turn，
真实 Key 未进入 CLI 输出或会话 JSONL，临时凭据环境、HOME 和工作区均已清理。P5-08 的任务单
验收证据至此齐全。剩余风险包括其他 OpenAI-compatible 服务对 tools 的实际支持差异、POSIX
Bash 未做当前原机验证，以及工具副作用不自动回滚。本任务未实现 Textual TUI、配置文件、
斜杠命令、扩展系统、npm 启动器或正式发布。
