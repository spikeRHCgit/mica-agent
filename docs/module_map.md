# Mica 源码技术地图

## 使用方式

本文用于按文件快速定位 Mica 当前源码。每个词条只说明一个实际存在的源码或包资源文件；
阶段目标和进度以 `PROJECT_PLAN.md`、对应 `docs/phaseN_plan.md` 为准，具体行为仍以代码和测试
为准。新增、删除、重命名源码文件时，同一任务必须同步更新本文。

`__pycache__/`、wheel、sdist、测试缓存和虚拟环境属于生成物，不进入技术地图。

## 文件词条

| 文件 | 具体作用 |
|---|---|
| `src/mica_agent/__init__.py` | Mica 顶层公共 API 出口；集中导出稳定契约，并通过懒加载避免普通导入触发 CLI、Provider、文件系统或运行时装配。 |
| `src/mica_agent/__main__.py` | `python -m mica_agent` 的最小入口；把执行直接交给 `cli.main()`。 |
| `src/mica_agent/_file_lock.py` | JSONL Store 使用的跨平台独占文件锁；组合进程内线程锁与 Windows/POSIX 文件锁，提供超时和上下文管理。 |
| `src/mica_agent/app.py` | 应用层装配边界；绑定 SessionService、启动上下文、AgentEngine 和工具执行器，只在完整成功后提交包含最终工具展示快照的会话轮次。 |
| `src/mica_agent/cancellation.py` | Provider、Engine、工具和交互等待共享的协作式取消令牌与固定取消异常。 |
| `src/mica_agent/cli.py` | 正式 CLI 装配与前端路由边界；负责参数解析、环境配置检查、工作区与会话选择、启动上下文快照、一次性工具/Provider/App 装配，默认进入 Textual TUI，`--plain` 进入基础文字界面，并提供安全启动诊断与资源关闭。TUI 会话期间安装最小 SIGINT 接缝，把原生 Ctrl+C 路由进 App 的两次确认退出状态机，App 退出或 runner 异常后恢复原处理器；`--plain` 保持原 KeyboardInterrupt 行为。P6-11A 增加临时 `--main-buffer-preview` 路由：与 Textual 路径复用同一会话、工具、Provider 与快照装配，只替换前端，且 `--plain` 优先（该参数不是稳定公共契约，不写入 README）；主缓冲区路径不安装 SIGINT 接缝，原始模式下 Ctrl+C 由前端模式机处理。前端构造或运行失败时，CLI 在未自行收敛的失败路径兜底关闭业务资源并给出固定诊断，正常关闭后不重复关闭 Provider。 |
| `src/mica_agent/context_builder.py` | 会话激活时读取 global/workspace `AGENTS.md`，生成独立指令消息、安全诊断和实际加载来源的不可变快照；不负责系统提示词正文。 |
| `src/mica_agent/engine.py` | 单次 Run 的核心循环；按序推进 Provider 请求、文本事件、工具调用、交互等待、限制、取消和唯一终态。 |
| `src/mica_agent/events.py` | Engine 对前端公开的有序事件契约，包括 Run、思考与文本增量、工具准备/开始/完成、工具展示快照、动态输出、权限、用户输入和终态事件。 |
| `src/mica_agent/file_tools.py` | 当前文件工具实现与注册入口；提供 `read`、`write`、`edit`，并执行大小限制、UTF-8、原子写入和安全结果转换。 |
| `src/mica_agent/interactions.py` | Engine 与前端之间的交互 Broker；分配不复用的交互 ID，等待权限/ask 回答，并清理取消、迟到或失效响应。 |
| `src/mica_agent/jsonl_store.py` | 一个会话一个 JSONL 文件的存储层；负责严格解析、锁内创建/读取/重写/删除、原子替换、扫描损坏和 v1/v2 到 v3 升级。 |
| `src/mica_agent/message_sanitizer.py` | 工具消息和工具展示进入 Provider、会话或诊断前的集中凭据清洗；隐藏敏感键、常见 Key 和 Bearer 凭据。 |
| `src/mica_agent/models.py` | 供应商无关的标准 `Message` 与角色定义，包含普通文本、工具调用和工具结果关联字段。 |
| `src/mica_agent/permissions.py` | 权限模式和策略决定的基础枚举：`default`、`solo` 以及 `ALLOW`、`ASK`、`DENY`。 |
| `src/mica_agent/policy.py` | 两模式内存权限决策：default 使用受保护资源/命令风险事实、一次授权与规则；solo 跳过权限策略。 |
| `src/mica_agent/command_risk.py` | 纯词法命令风险分类；分析组合命令、包装、危险命令和受保护路径，不执行命令或读取脚本，不提供安全证明。 |
| `src/mica_agent/runtime_tools.py` | 运行时工具实现与注册入口；提供 Windows 优先的受限 `bash`，以及自由文本或结构化选择的 `ask`，管理环境清理、输出上限、超时、进程树与等待取消。 |
| `src/mica_agent/schema_validation.py` | 使用固定 JSON Schema Draft 2020-12 校验 ToolSpec Schema 和实际工具参数，拒绝远程引用并返回安全错误。 |
| `src/mica_agent/session_contracts.py` | 会话公共数据和 JSON wire 契约；定义 SessionMeta、带工具展示的 v3 TurnEntry、v1/v2 严格兼容读取、消息角色顺序和格式错误边界。 |
| `src/mica_agent/session_service.py` | 会话业务层；在 Store 之上提供创建、加载、列表、重命名、分支、回退、清空、提交、最后活跃选择和过期清理。 |
| `src/mica_agent/system_prompt.py` | 通过包资源加载内置提示词，取得一次性环境快照并组装冻结的会话系统提示词；不负责读取 `AGENTS.md`。 |
| `src/mica_agent/tool_contracts.py` | 供应商无关的工具数据契约；定义 ToolSpec、ToolCall、ToolRequest、ToolResult、状态枚举和深层不可变 JSON 快照。 |
| `src/mica_agent/tool_executor.py` | 工具统一执行入口；依次完成注册查询、参数校验、权限判定、交互等待、handler 调用、取消和安全 ToolResult 转换。 |
| `src/mica_agent/tool_presentation.py` | 工具展示快照和生成边界；提供五个内置工具与未知工具的安全 payload、编辑逐行差异、状态映射和深层不可变快照。 |
| `src/mica_agent/tool_registry.py` | 进程内工具注册表；保存 ToolSpec 与异步 handler，提供原子注册、确定性列表、重复拒绝和按名解析。 |
| `src/mica_agent/tui/__init__.py` | Textual TUI 包的显式导入入口；公开 `MicaTuiApp`、Composer、启动资源与会话事实快照、模型输出视图以及 transcript 滚动与历史记录类型，不扩展 `mica_agent` 顶层公共 API。 |
| `src/mica_agent/tui/app.py` | Textual 外壳与 Engine 公共事件投影边界；建立 transcript、唯一 work-status、可编辑 composer 与两行运行事实的稳定布局，接收已加载的 `StoredSession` 恢复历史，按 Run/seq 隔离事件，并把同一 Run 内每个模型步骤映射到独立的思考/回复视图。首个有效 `ToolPrepared`（含 ASK）结束当前步骤并保持 DOM 位置，后续 Delta 追加到上一批工具记录之后；同时驱动唯一工作状态条和工具卡片生命周期及动态输出投影。Ask/权限等待时懒创建唯一底部 overlay，隐藏普通控制区；键盘所有权来自 overlay 当前已挂载的活动子组件（唯一事实源），业务关联索引只映射交互、不决定路由。Key、鼠标滚轮和 Resize 分派前执行幂等的输入所有权校正，只修复无效或不属于当前所有者的焦点，不假设终端一定上报 `AppFocus`。无阻塞交互时 PageUp/PageDown 浏览 transcript；有交互时页键一律滚动 overlay，聚焦 Bash 卡片时保留其内部滚动与边界冒泡。App 级高优先级 `Esc`/`Ctrl+C` 接管取消与两次确认退出，不依赖局部焦点；同一输入批次内近同步重复投递（按键与原生信号、或重复分发）不会计为第二次按键。Textual 8.2.8 的 Windows 控制台输入丢弃 key-up 记录并忽略重复计数，延迟到达的自动重复与本层可见的“新的独立按下”无法区分，该平台限制由真实终端手工复验。不含独立快捷键栏。 |
| `src/mica_agent/tui/bottom.py` | composer 下方的两行只读运行事实；仅渲染 CLI 注入的安全 workspace、model 和 session label，并使用终端 cell width 做显示层省略与右对齐。 |
| `src/mica_agent/tui/composer.py` | TUI 专用的可编辑输入组件；负责品牌提示符、完整固定边框、空草稿发送提示、软换行、真实换行、按显示行数 1～8 行自动增高与内部滚动、溢出提示、长粘贴折叠原子片段、原文还原提交消息与安全展示快照。屏幕光标按提示符行的实际偏移对齐渲染出的光标单元，鼠标定位与删除选区的片段原子性不变。 |
| `src/mica_agent/tui/logo.py` | transcript 顶部的只读响应式 Mica Logo；按可用宽度切换无伴随字标的完整、紧凑与纯文本形态。 |
| `src/mica_agent/tui/interaction_layer.py` | 唯一底部阻塞交互覆盖层；懒创建后用 `display` 显隐，承载活动 Ask 或权限视图，不进入空闲布局，也不保存会话或回调状态。它同时是**唯一滚动容器**，并通过 `active_view` 暴露“已挂载且仍活动”的交互子组件，作为键盘所有权、chrome 显示与焦点恢复的共同事实源。 |
| `src/mica_agent/tui/interactions.py` | ask 结构化交互面板与只读历史 ask 记录；消费公开 AskPrompt，提供可注入的响应/取消接缝、选项导航、自定义回答、备注、终态原地收口和安全展开。活动面板由 overlay 承载，完成后复制为 transcript 记录；选项正文由独立内容子节点承担自动高度，不连接 Broker、Engine、Provider 或 Store。 |
| `src/mica_agent/tui/permissions.py` | 权限请求底部确认面板；消费 ToolPrepared 的安全展示快照与公共 PermissionResponse 接缝，提供一次性 Yes/No、严格同步回调和无敏感信息的纯文本降级展示。面板自身不是滚动容器，页键由 App 路由给共享 overlay；没有本地 `on_key`，App 级交互控制器是唯一按键入口。宽终端在提示行与状态条之间保留一行空隙，短终端去掉纵向 padding。 |
| `src/mica_agent/tui/runtime.py` | TUI 运行适配控制器；集中拥有唯一活动 Run 的 CancellationToken、消费任务、真实 run_id 和 `MicaApp.run()` 事件流，顺序投影事件、转发公共交互响应，并在 TUI 退出或 `aclose()` 时按取消、等待流、清理 UI、关闭 MicaApp 的顺序释放资源。 |
| `src/mica_agent/tui/paste.py` | TUI 内长粘贴折叠片段的模型与源文本判定；按逻辑行阈值决定是否折叠，并提供不可展开的紧凑摘要与真实原文快照。 |
| `src/mica_agent/tui/history.py` | `StoredSession` 到 TUI 历史视图的只读投影；按持久化顺序产出历史用户卡片、ask、read/write/edit/bash 正式卡片和紧凑工具记录，不重放 Run、不重新执行工具、不读取会话存储。 |
| `src/mica_agent/tui/transcript.py` | transcript 容器、用户消息卡片与历史工具记录；新会话从顶部展示启动层级，恢复历史时定位最新内容，并提供底部自动跟随、用户上滚离底检测、唯一 `↓ new output` 提示与返回底部动作；展示提交时保存的紧凑显示快照，不把用户原始文本解释为 Markdown，也不展开工具细节。自动跟随由本组件独占（不使用 Textual `anchor`）：用户滚动入口先同步释放跟随，流式追加只在仍跟随时排队一次回调并在执行前复查，落底由布局变化驱动，显式 End 才恢复跟随。 |
| `src/mica_agent/tui/output.py` | TUI 模型正式回复与独立思考视图；使用 Textual Markdown、流式光标、完整/折叠/隐藏思考模式及当前焦点的 `Ctrl+O` 切换，支持从历史正文构造非流式回复，不持久化事件内容。`ReasoningDisplayMode` 已移到不依赖 Textual 的 `tui/models.py`，本模块继续导入并重导出，保持既有公开导出与行为兼容。 |
| `src/mica_agent/tui/models.py` | TUI 专用的不可变视图状态与共享枚举；启动资源快照保存系统提示词名称、AGENTS 来源及其安全显示路径、工具/Skill/扩展名称，会话事实快照保存显示用工作区、会话标签和模型；另提供不依赖 Textual 的 `ReasoningDisplayMode`（full/collapsed/hidden），供旧 Textual 视图与主缓冲区前端共同使用。 |
| `src/mica_agent/tui/startup.py` | transcript 内的只读启动首屏组件；依次渲染定位文案、Context/Skills/Extensions/Tools 分区资源框和真实就绪说明，不自行读取或推断业务状态，也不模拟模型欢迎消息。 |
| `src/mica_agent/tui/status.py` | 唯一工作状态条视图；公开 Run 工作状态和阶段类型，消费已校验事件投影提供的时钟，负责阶段/总耗时、进展颜色和确定性点号动画，不显示敏感运行标识或正文。 |
| `src/mica_agent/tui/theme.py` | TUI 外壳的深色语义主题令牌与基础布局样式；依靠 Textual/Rich 完成颜色能力协商和外壳级降级。 |
| `src/mica_agent/tui/tool_cards.py` | 工具卡片基础组件、`read`、`write`、`edit`、`bash` 和 Generic 兜底卡片；按 `(run_id, call_id)` 由外壳维护稳定组件身份，消费安全展示快照并原地更新生命周期状态，write 支持纯文本十行折叠与焦点展开，edit 支持逐行差异、行号与二十条变化折叠，bash 支持三行命令折叠、动态输出、八行视口、内部浏览与终态输出校准，并提供统一的安全卡片解析及可注入专用工厂接缝。 |
| `src/mica_agent/tui/main_buffer/__init__.py` | 主缓冲区终端前端的显式导入入口；只导出终端、清洗接缝、渲染、组件、编辑器、交互模式、单 Run 控制器与编排层，不依赖 Textual，也不扩展 `mica_agent` 顶层公共 API。 |
| `src/mica_agent/tui/main_buffer/terminal.py` | 主缓冲区**唯一 IO 所有者**；负责原始输入注册与轮询、终端尺寸、交互与颜色能力判断、autowrap 管理、主缓冲区输出，以及 `find_forbidden_sequences()` 对**实际发出的字节流**做按语法解析的安全扫描（含未知私有模式、整屏擦除、标题与剪贴板）。输入与输出的获取与释放互相隔离、可重复调用，单项清理失败只记账不外抛。 |
| `src/mica_agent/tui/main_buffer/sanitize.py` | 不可信正文到可信 ANSI 的**唯一清洗接缝**：整段删除 CSI/OSC/DCS/C1 与 C0 控制内容、归一 CRLF、保留正常 Unicode（含组合字符与 ZWJ 序列），且从不插入新的 `ESC`，因此清洗结果无法再组成控制序列。 |
| `src/mica_agent/tui/main_buffer/renderer.py` | 语义块协议与保留式差分帧渲染；块只保存安全源数据与展示状态，并按当前宽度/调色板/全局展开状态生成物理行（`Block` 纯文本、`RenderedBlock` 冻结物理行）。渲染器是相邻高层对话记录（用户/思考/正式回复/工具）之间**唯一间距事实源**：不同逻辑流之间恰好插入一条无背景空白行，同一流的切分片段、流式增量与工具原地更新不重复插入。渲染器维护**屏幕映射**：上一帧写出的每个物理行及其逻辑身份（同一消息/工具记录被切分后的片段共享标识与连续逻辑行号），行数不超过终端高度，因此映射内每行都在屏幕上、可安全寻址。重绘时按身份与文本求公共前缀，**先**回到第一个真正变化的行并擦除下方，**再**写出新行，普通增量只重绘变化尾部，较早块高度变化（全局 Ctrl+O、宽度变化）从该块起点受控重排；首帧只向下写出，擦除从不涉及启动前的 shell 内容；帧高不超过屏幕高，超预算行作为一次性永久输出进入 scrollback，已永久写出的前缀不重复写出。另提供 `cell_len`/`expand_tabs`（共用 `TAB_WIDTH`）、`wrap_line` 与 `strip_ansi`。 |
| `src/mica_agent/tui/main_buffer/components.py` | 内容层级与底部组件；生成 Logo（与 Textual 侧同一几何，P6-11C 再收敛）、启动资源面板、工作状态条、输入框边框与底部事实行，并只读地探测当前工作区 Git 分支。`UserBlock` 把用户原文当纯文本显示（每一条物理行填满当前内容宽度的低对比度深灰背景、首行短蓝色身份标识、后续行稳定缩进，正文里的 Markdown 标记不会生效，块自身不附加空行）；`ReasoningBlock` 只消费 `ReasoningDelta`，使用 muted+dim 灰色次要文字与左侧细灰线，支持 full/collapsed（默认，最多两行且省略号计入第二行宽度）/hidden（不产生任何行、不影响相邻间距）三种模式且不响应 Ctrl+O。面板与边框一律先按纯文本测量/截断再着色，动态事实与正文经清洗接缝后再进入排版；块的 `repr` 只报告类别与字符数。 |
| `src/mica_agent/tui/main_buffer/markdown.py` | 模型正式回复的 Markdown 渲染边界；把清洗后的源文本交给锁文件内已有的 Rich 解析与排版，再把每行 Segment 的样式重新生成受控 ANSI（只允许加粗/暗淡/斜体/下划线与前/背景色，**从不生成 OSC 8 超链接**）。标题左对齐、行内代码只用蓝灰色前景（无 bgcolor）、代码块深灰背景并填满可用宽度；NO_COLOR 用 `│ ` 等宽边界与加粗属性保留结构，不依赖颜色。`MarkdownBlock` 保存源文本按需重排，`StreamingMarkdownBlock` 渐进渲染并自带已写出前缀计数，累计源文本始终不超过 `MAX_SOURCE_CHARS`（到上限后追加幂等），`finish()` 用完整累计源文本做一次稳定渲染并移除流式光标；流式光标计入物理行宽度（末行满宽时另起一条物理行）。不加载图片、不访问 URL、不执行代码；解析器构造或渲染抛普通异常时降级为已清洗纯文本，按逻辑换行与终端宽度拆成互不超宽的物理行，不带任何控制内容。 |
| `src/mica_agent/tui/main_buffer/tool_views.py` | 专用工具视图与唯一解析接缝；`read`（单行紧凑）、`write`（纯文本正文、十行折叠）、`edit`（真实旧/新行号、逐行差异、二十条变化折叠、未匹配时显示「文件未改变」）、`bash`（命令行三行折叠、活动期八行有界输出、终态用最终快照收口）、`ask`（问题与真实答案）与 `generic` 兜底。状态用深蓝/深绿/深红大面积背景填满可用宽度表达，NO_COLOR 改用 `○ ● ✓ × ■` 与固定安全原因。`ToolViewFactory` 在构造时复制外部映射、不允许覆盖内置名，只接收公共安全快照；解析顺序为内置 kind 优先（保留名只接受对应 kind，内置 kind 含 ASK 只给名称正确的内置视图，外部 builder 只处理非保留名的 `GENERIC`），扩展视图需与调用方快照逐字段等值（call_id、tool_name、kind、status、payload、message），否则安全回退通用视图，异常文本与 traceback 不进入卡片或诊断。 |
| `src/mica_agent/tui/main_buffer/editor.py` | composer 草稿、软换行与粘贴片段；基于 prompt_toolkit `Buffer`/`Document` 提供插入、删除、移动与按终端单元格的软换行视图（制表符先按 `expand_tabs` 展开，使软换行、显示与光标列共用同一套宽度），并把终端按键归一为稳定 `Key`。达到阈值的长粘贴折叠为不可拆分的 `[paste #N +M lines]` 片段：整体跨越、整体删除，提交时还原原始正文。 |
| `src/mica_agent/tui/main_buffer/interactions.py` | ask 与权限的独占输入模式；提供选项导航（含 `Type something.` 自定义行）、数字定位编号选项、自定义回答、备注、一次允许/拒绝与取消意图，只产生公共 `UserInputResponse`/`PermissionResponse`，不直连 Broker、Engine、Provider 或 Store。 |
| `src/mica_agent/tui/main_buffer/runtime.py` | 主缓冲区单 Run 控制器；集中拥有唯一活动 Run 的取消令牌、消费任务、真实 `run_id` 与事件流，转发公共交互响应，并在未收到公共终态时补一条只携带最小终止类别（`cancelled`/`runtime_error`/`stream_ended`）的内部标记；退出时按取消、等待、关闭 `MicaApp` 的顺序释放资源且幂等。 |
| `src/mica_agent/tui/main_buffer/screen.py` | 主缓冲区前端编排与模式机；维护 `IDLE/RUNNING/ASK/PERMISSION/CLOSED` 唯一活动模式，按 `run_id`／单调 `seq`／`(run_id, call_id, tool_name)` 与最终快照完整身份准入公共事件（名称与 presentation kind 不一致时拒绝投影），把事件投影到语义文档与唯一工作状态：文本增量按模型步骤创建独立片段（`TextDelta` 用流式 Markdown 块、`ReasoningDelta` 只进思考块、工具事件经 `ToolViewFactory` 生成专用视图并原地收口）。`Ctrl+O` 是全局工具详情开关（只影响工具记录，不改变思考、Ask、composer 与 facts），Esc 取消、`Ctrl+D` 最小退出、两次 `Ctrl+C` 退出、resize 重排、唯一刷新任务与 Run 结束后输入区恢复不变；历史恢复按 Turn 顺序复用同一组视图与 Markdown 渲染（v1/v2 或缺少快照时显示中性 `tool <name> · recorded`），不重放 Run、不恢复未保存的思考。提交顺序是先交运行时、成功才清空草稿；关闭时各步互相隔离并只记固定标签；`RunFailed` 只按 `ErrorCategory` 映射固定文案。 |
| `src/mica_agent/workspace.py` | 工作区身份与数据根边界；优先识别 Git 根目录，否则使用启动目录，并生成规范路径、slug、SHA-256 ID 和会话目录。 |
| `src/mica_agent/workspace_paths.py` | 文件工具共享的工作区路径解析器；拒绝绝对/越界路径、重解析点、Windows 别名和 ADS；另提供文件授权与命令分析共用的敏感路径分类，不自行授权。 |
| `src/mica_agent/providers/__init__.py` | Provider 子包的公共出口；只重导出供应商无关契约和错误，不执行网络请求。 |
| `src/mica_agent/providers/base.py` | Provider 公共协议；定义 ProviderRequest、思考/文本/结束/错误事件、错误分类和 Provider 生命周期接口。 |
| `src/mica_agent/providers/openai_compatible.py` | OpenAI-compatible Chat Completions 适配器；构造含可选思考强度的 HTTP 请求、解析 SSE 思考/文本增量、组装工具调用、分类错误并执行有限网络重试。 |
| `src/mica_agent/prompts/system.md` | 随 wheel 分发的 UTF-8 内置系统提示词模板；只保存稳定正文和环境上下文占位符，不含会话动态状态或 `AGENTS.md` 内容。 |
| `src/mica_agent/reasoning.py` | 供应商无关的 `ReasoningEffort` 思考强度枚举；定义稳定的小写 wire value，不读取配置或执行运行时装配。 |
