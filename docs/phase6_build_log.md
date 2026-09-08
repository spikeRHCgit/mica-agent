# P6-01A 启动资源快照契约

## 任务目标

为 `ContextBuildResult` 增加前端无关的 `loaded_sources` 快照，使后续界面能够读取本次上下文构建实际加载的 AGENTS 来源，而不重新读取文件或解析指令消息正文。

## 稳定语义

- `loaded_sources` 是只包含 `AgentFileSource` 的不可变元组，顺序固定为 `GLOBAL`、`WORKSPACE`。
- 只有本次 `ContextBuilder.build()` 确实为某来源追加 AGENTS 系统消息时，该来源才进入快照。
- 缺失、空文件和产生安全诊断的来源不进入快照；只含换行等非空内容按既有逻辑生成消息并进入快照。
- 每次显式构建都重新读取文件；已经返回的结果不受后续文件变化影响。
- session messages 不属于 AGENTS 来源，不进入快照。

## 兼容性决定

新增字段定义为 `loaded_sources: tuple[AgentFileSource, ...] = ()`，位于原有 `messages`、`diagnostics` 之后。原有位置参数和关键字构造继续有效，`frozen=True` 与 `slots=True` 保持不变。

## 安全边界

快照只包含稳定枚举，不携带绝对路径、工作区或 HOME、文件内容与大小、系统提示词正文、原始异常、traceback 或运行时文件对象。现有诊断文案与指令封装保持不变，也不增加文件读取、目录创建、会话写入或后台任务。

## 验收结论

目标、兼容、分发、全量测试及静态质量、格式和离线构建检查均通过。wheel 继续包含更新后的 `mica_agent/context_builder.py`，普通导入保持无文件系统和运行时副作用。

## 后续状态

P6-01B 尚未开始。

# P6-01B Textual 外壳与主题

## 任务目标

加入 Textual 正式依赖和独立 `mica_agent.tui` 包，建立不连接会话、Run 或其他业务运行时的最小可测试前端外壳。

## 最终三段布局

外壳按稳定顺序挂载可滚动的 `#transcript`、唯一的 `#work-status` 和固定在底部的 `#composer`。三个区域互为 Screen 的直接子节点；状态区不进入 transcript，composer 在宽、窄终端中都保持可见。本切片不显示 Logo、启动资源、消息、运行状态或输入功能。

## 依赖决定

产品依赖声明为 `textual>=8.2.8`，不增加 Rich 直接依赖、不设置最高版本上限，并保持 Python 3.11 最低版本。锁文件固定本阶段使用的 Textual 版本及其传递依赖。

## 主题和降级边界

主题模块集中维护背景、表面、正文、次要文字、浅蓝/标准蓝/深蓝品牌色、成功、错误和边界等语义令牌。区域同时由固定位置和边界区分，不依赖颜色作为唯一信息；颜色能力交由 Textual/Rich 的内置协商、ANSI 和 `NO_COLOR` 降级机制处理，不增加平台探测或原始 ANSI 控制。

## 测试与安装态结论

Textual headless/Pilot 测试覆盖外壳挂载退出、区域唯一性与顺序、滚动类型、父子边界、宽窄尺寸、底部 composer、无颜色/有限颜色条件和退出后无残留任务。普通顶层导入保持不加载 TUI/Textual；wheel 包含三个 TUI 模块、声明 Textual 依赖，并可在隔离安装态显式导入 `MicaTuiApp`。

## 后续边界

本切片未接入 CLI、会话、MicaApp、Engine、Provider、工具、权限、ask 或真实输入。P6-01C 尚未开始。

# P6-01C Logo 与启动资源区

## P6-01C1 Logo 组件边界

`MicaLogo` 是只属于 TUI 的静态只读组件，不依赖会话、应用、Engine、Provider、工具执行器、工作区或配置，也不读取文件、创建目录、启动任务、Timer、Worker、动画或子进程。

## 响应式规则

Logo 使用组件可用宽度选择三个稳定档位：宽度不小于 54 时显示六行完整折叠几何 M，宽度不小于 28 且小于 54 时显示四行紧凑 M，宽度小于 28 时只显示精确文本 `Mica`。完整与紧凑形态使用块字符和留白形成高低错落的切面轮廓，不使用三角字符。组件监听 Textual 尺寸事件，在同一生命周期内更新现有视图，不重复挂载。

## 颜色与降级

完整和紧凑形态复用主题现有的浅蓝、标准蓝与深蓝令牌分层渲染，不引入紫色、原始 ANSI、图片、外部字体或平台探测。几何字符和 `Mica` 文本独立表达品牌，禁用颜色或使用有限 ANSI 颜色时仍可读取，能力协商继续交给 Textual/Rich。

## 挂载与安装态

唯一的 `#mica-logo` 是可滚动 `#transcript` 的第一个子节点，不是 Screen 的直接子节点；Logo 过高时由 transcript 的滚动区域容纳，不改变 Screen 直属的 work-status 和底部 composer。wheel 包含 `mica_agent/tui/logo.py`，安装态显式导入 `MicaTuiApp` 的入口保持不变。

## P6-01C2 启动资源快照

`StartupResourceSnapshot` 是 TUI 专用的 frozen、slots 不可变视图状态，保存系统提示词稳定名称、按 global/workspace 固定顺序排列的 `AgentFileSource`，以及保持注入顺序的工具、Skill 和扩展名称。列表输入在构造时复制为元组；名称只接受安全的单行非空文本。校验错误使用固定字段说明，不回显输入值、路径、内容、对象表示或原始异常。未来装配层直接把 `ContextBuildResult.loaded_sources` 注入 `agent_sources`，TUI 不调用 ContextBuilder，也不重新读取文件。

## P6-01C2 只读资源区

`MicaTuiApp` 新增可选 `startup_resources` 关键字入口，`None` 规范化为空快照，既有无参和 `ansi_color` 构造保持有效。`#startup-resources` 是 `#transcript` 中紧随唯一 Logo 的第二个子节点，稳定显示系统提示词名称、AGENTS 来源、工具、Skill 和扩展；空值明确显示 `none`，名称按纯文本渲染。资源区与 Logo 一起由 transcript 负责滚动，不覆盖唯一 work-status 或底部 composer。

## P6-01C 安全与集成边界

快照和视图不携带或显示系统提示词正文、AGENTS 正文、绝对路径、诊断对象、traceback 或运行时对象，也不创建任务、Timer、Worker、线程或子进程。P6-01C2 仍未连接 CLI、真实 ContextBuilder 调用、会话、MicaApp、Engine、Provider、工具执行器或 Run；P6-02A 尚未开始。

# P6-02A1 Composer 输入与提交契约

`MicaComposer` 基于 Textual 的多行编辑组件，保持固定基础高度和显示层软换行；普通 Enter 对非空白原文发出一个只读 `Submitted.text` 快照并清空输入，Shift+Enter 在光标处插入真实换行。有效提交不裁剪空格、不重排文本或规范化 Unicode，空白输入只清空，不产生提交。

Composer 仍是 Screen 的第三个直接子节点并保持底部焦点；本切片只产生 TUI 内部消息，不连接 CLI、MicaApp、会话、Engine、Provider 或 Run，也未实现粘贴摘要、用户消息卡片、自动增长或内部溢出提示。P6-02A2 与 P6-02B 尚未开始。

# P6-02A2a Composer 动态高度与内部滚动

`MicaComposer` 复用 Textual 文本区域自身的显示排版结果：`content_region.height` 等于其公开的显示文档行数，`height: auto` 让组件高度随内容行数增长；组件保留主题既有的一行边界，因此空输入时内容一行、组件三行。真实换行、软换行及中文/Unicode 宽字符都按 Textual 实际显示宽度计入高度，不向原文插入真实换行。

高度上限为八个内容行（组件十行），超出后在组件内部垂直滚动，光标保持在可见区域；用 Backspace/Delete、清空或提交后按新的显示行数逐步缩小，清空或提交后回到一行内容与顶部滚动。组件根据终端剩余高度动态设置 `max-height`，保留工作状态条和至少一行 transcript，机身保持在屏幕底部；在极矮终端中降低实际可见内容高度、由内部滚动承载溢出，但不丢失或截断原始文本。

对同一输入状态连续多次刷新后高度保持稳定，不会 1/2 行振荡；终端 resize 时更新布局而不重建组件、不清空文本、不改变焦点或移动父节点。

# P6-02B 完整输入体验与用户消息

本节完成输入溢出提示、长粘贴折叠原子片段、原文还原提交与用户消息卡片，闭环了 Composer 的完整输入体验：

- **输入溢出提示**：当显示行数超过内容区域高度时，在顶部/底部可见行以 `↑ N more` / `↓ N more` 提示被滚动隐藏或未显示的内容行；提示随滚动与高度实时变化，且不作为提交文本的一部分。
- **长粘贴折叠**：利用 Terminal 的 bracketed paste 事件，达到阈值行（9 行）的粘贴折叠为不可展开的 `[paste #N +M lines]` 摘要；短粘贴继续作为普通文本。片段作为原子单元保存：方向键整体跨过、Backspace/Delete 接触时删除整个片段、光标不能停在片段内部逐字编辑。
- **原文还原**：在 TUI 提交边界恢复所有片段的完整真实原文，供后续业务运行链路消费；不把摘要文字当成真实消息，同时保留一份紧凑显示快照用于用户消息卡片。
- **用户消息卡片**：提交后向 transcript 追加只读用户消息卡片，展示紧凑显示快照并继续保留摘要，不主动解释为 Markdown。

本切片只产生 TUI 内部状态与消息，仍不连接 CLI、MicaApp、Engine、Provider、会话、Run 或真实业务运行；未升级任何依赖。P6-03 尚未开始。

# P6-03A+B 模型回复与思考内容投影

- **公共事件投影 seam**：`await MicaTuiApp.project_engine_event(event)` 只消费现有 `EngineEvent` 公共对象；返回 `True` 表示该事件已被当前 TUI Run 投影接受，工具、权限和 ask 事件安全返回 `False`。
- **Run/seq 隔离**：`RunStarted` 建立不可复用的 Run 投影；每个已识别事件按 Run 维护严格递增的 `seq`，重复、倒序、未知 Run、终态后的事件均被拒绝。非本切片事件只消耗有效序号，不创建占位视图。
- **正式回复边界**：首个 `TextDelta` 创建唯一的 `AssistantResponseView`，后续增量原样追加到同一对象及其 Textual `Markdown` 源；流式蓝色光标定位在正文末尾区域，不占独立正文行，`RunCompleted` 以完整源执行稳定 Markdown 更新，失败/取消只保留已有内容并完全移除光标。
- **思考显示模式**：`ReasoningDelta` 进入独立的 `ReasoningView`，默认 `COLLAPSED`，并支持 `FULL`、`COLLAPSED`、`HIDDEN`。折叠预览按真实显示宽度生成，窄终端仍在最后可见行保留省略提示；隐藏模式保留隔离状态但不挂载正文或空白占位。
- **焦点规则**：折叠思考视图聚焦后，`Ctrl+O` 只切换当前组件的折叠/完整显示；完整和隐藏模式不被该快捷键改变。
- **无颜色降级**：思考使用主题 `MUTED` 语义色、细左线和额外缩进；布局分隔与纯文本仍在禁用颜色时保持可读，未依赖颜色作为唯一区分。
- **范围边界**：本切片不接入 CLI、MicaApp、真实 Engine 流或 Provider，不持久化界面状态，不实现工具卡片或唯一工作状态条；**P6-03C 尚未开始**。

# P6-03C 唯一工作状态条

- **公共事件投影**：`MicaTuiApp.project_engine_event()` 延续 P6-03A+B 的 Run/seq 校验；只将当前活动 Run 的合法事件投影到唯一 `WorkStatusView`，工具和交互事件更新阶段但保留既有 `False` 返回兼容语义。
- **稳定状态**：状态条公开 `WorkStatus`（`READY`、`WORKING`、`DONE`、`CANCEL`、`INTERRUPT`）与 `WorkPhase`（`NONE`、`WAITING`、`THINKING`、`RESPONDING`），固定在输入区上方，不进入 transcript。
- **计时与进展**：使用可注入单调时钟；RunStarted 建立 waiting 起点，独立 ReasoningDelta 进入 thinking，TextDelta 进入 responding，工具/权限/用户输入事件进入 waiting。阶段增量不重置阶段起点，但所有可观察进展重置颜色计时；thought 只累计 thinking，waited 统计首个正式文本前等待，无正式文本时使用总时长。
- **视觉与资源边界**：点号通过同一 `advance_animation()` 循环推进，生产环境最多一个 Textual Timer；主体颜色从 `BRAND_LIGHT`、`BRAND`、`BRAND_DEEP` 插值，颜色不是状态唯一表达。ready、done、cancel、interrupt 停止动画；取消前无进展显示 cancel，否则显示 interrupt，失败统一 interrupt。
- **验收结论**：公开行为测试覆盖状态生命周期、阶段与耗时、颜色边界、时钟倒退、Run 隔离、唯一性、窄终端、无颜色、resize 和安全 repr；新增 `status.py` 纳入 wheel 分发。P6-04 尚未开始。

# P6-04 历史恢复与滚动

- **历史注入边界**：`MicaTuiApp(stored_session=...)` 只接收由外部加载完成的 `StoredSession`，非法类型在构造期抛固定 `TypeError`；TUI 不读取 Store、不访问会话文件路径、不重放 Run。
- **只读历史投影**：新增 `tui/history.py`，按 `StoredSession.turns` 原始顺序产出「历史用户卡片 → 该轮工具记录 → 最终回复视图」，多轮保持持久化顺序；工具记录顺序取自 `intermediate_messages` 中真实 `tool_calls` 序列。
- **历史工具记录**：v3 快照存在时显示 `tool <name> · <终态>`，缺快照的旧版本降级为 `tool <name> · recorded`，只表示存在该调用而不猜测结果；组件持有不可变快照供 P6-05 替换为正式卡片，`repr` 与渲染都不泄漏 `call_id`、参数、命令或结果正文。
- **历史与实时边界**：历史恢复不建立 Run、不污染 seq 与终态集合，启动后状态条仍是 `Mica · ready`；不恢复或伪造历史思考内容，历史回复以非流式 Markdown 呈现且不挂载流式光标。实时 `ReasoningDelta`/`TextDelta` 与新提交继续追加在历史之后。
- **滚动与跟随**：`ConversationTranscript` 在底部时自动跟随新增内容，真实 PageUp 等用户滚动离底后停止强制跟随；离底期间新增可见内容显示唯一 `↓ new output` 提示，返回底部后隐藏提示并恢复跟随。提示是 transcript 内的覆盖层，不进入滚动历史，也不改变工作状态条与 composer 的位置和高度。
- **验收结论**：公开行为测试通过真实 Textual Pilot 覆盖历史顺序、工具记录降级、空会话、底部跟随、离底提示、被拒事件隔离、隐藏思考、resize、无颜色降级与安全 `repr`；新增 `history.py`、`transcript.py` 纳入 wheel 分发校验。P6-05A 尚未开始。

# P6-05A 工具卡片骨架与 read

- **卡片生命周期**：新增稳定的工具卡片基础组件和单行 `read` 卡片；`ToolPrepared` 创建组件，`ToolStarted` 与 `ToolFinished` 原地更新同一对象，实时身份使用 `(run_id, call_id)` 隔离。
- **状态收口**：准备与执行使用深蓝背景，成功使用深绿，失败或取消使用深红；Run 失败或取消只收口该 Run 中尚未完成的卡片，不改变已经成功的卡片。
- **read 展示边界**：只显示工具名和经过展示契约确认的工作区相对路径；失败时可追加固定安全原因，不显示正文、参数、文件大小、耗时、调用标识、绝对路径或原始异常。
- **历史兼容**：v3 中带 `read` 展示快照的历史调用恢复为最终卡片；v1/v2 或缺少快照时继续使用 `recorded` 简化记录，其他工具仍保持中性历史记录。
- **集成与验收**：卡片新增和终态更新保持既有底部跟随与离底新输出提示；窄终端、无颜色、会话兼容、App/CLI 公共链路、安装态 wheel、全量测试和离线构建均通过。P6-05B 尚未开始。

# P6-05B write 卡片

- **展示规则**：新增 `WriteToolCard`，在无外框卡片中展示工具名、工作区相对路径和安全快照中的原始文件内容；内容作为纯文本保留 Markdown 标记、Unicode、缩进与有意义的空白，不显示参数标签、大小、耗时或内部标识。
- **折叠与展开**：按归一化后的逻辑行计算，CRLF/LF 统一处理且忽略单独的结尾换行；超过十行时默认显示前十行与固定折叠提示，聚焦卡片的 `Ctrl+O` 只切换当前卡片，短内容保持不变。终端软换行不改变逻辑行数，展开后的尺寸变化继续使用既有 transcript 跟随机制。
- **生命周期和历史恢复**：实时 `ToolPrepared → ToolStarted → ToolFinished` 始终更新同一 `(run_id, call_id)` 卡片；Run 失败或取消只收口对应 Run 的未完成卡片，已完成卡片不被重染。v3 write 快照恢复为最终卡片，旧版本或缺失快照继续使用中性记录，不重放工具、不读取工作区、不建立实时映射。
- **安全与兼容边界**：卡片只接收 `ToolPresentationSnapshot`，拒绝不匹配的工具/快照类型；失败或取消保留内容并追加固定安全原因，`repr` 不包含路径、内容、Run ID 或 call ID。read 行为、其他工具中性历史记录和既有滚动/颜色降级保持兼容。
- **最终验收结论**：write 生命周期、状态颜色、纯文本内容、逻辑行折叠、焦点展开、窄终端软换行、历史恢复、安全边界和 wheel 分发测试通过；P6-05C 尚未开始。

# P6-05C edit 卡片

- **差异展示**：新增 `EditToolCard`，只消费 EDIT presentation 中已经生成并清洗的 `matched` 与 `lines`，按 snapshot 原始顺序展示 context、removed、added 行，不读取文件、不重新计算 diff、不进行逐字符高亮。工具名加粗，工作区相对路径使用下划线浅青色，正文为纯文本。
- **行号与颜色**：matched 为 true 时，context 使用 `new_line`，removed 使用 `old_line`，added 使用 `new_line`；当前可见行号列统一宽度。上下文使用灰白色，删除使用浅红色 `-`，新增使用黄绿色 `+`。pending 的 `matched=None` 和 unmatched 不显示伪造行号，空白增删行仍保留标记与行号（若快照提供）。
- **matched/unmatched**：matched 为 false 时展示快照提供的无行号尝试差异，并固定显示 `file was not changed`；已有相同安全文案不会重复，其他安全原因与其组合显示。没有最终 presentation 的失败或取消仍保留尝试差异并生成固定安全原因。
- **折叠与展开**：只按 removed/added 变化行计数；不超过 20 条完整显示，超过 20 条保留原始顺序中的前 10 条和后 10 条变化，并允许相邻 context 一并显示，中间使用灰色省略提示。聚焦卡片后 `Ctrl+O` 展开完整快照，再次按下恢复折叠；卡片状态更新不重建组件且保持展开状态，尺寸变化复用 transcript 跟随逻辑。
- **生命周期与历史边界**：实时 `ToolPrepared → ToolStarted → ToolFinished` 原地更新同一 `(run_id, call_id)` 卡片；实时 `CANCELLED` 显示红色取消状态并保留差异。`RunFailed`/`RunCancelled` 只收口对应 Run 的未完成卡片，已完成卡片不重染。v3 历史只恢复合法的 success/failed edit 快照；取消 Run 不提交 TurnEntry，因此不存在合法 cancelled 历史快照。v1/v2 或缺少 presentation 继续显示 `tool edit · recorded`。
- **安全与兼容边界**：渲染和 `repr` 不包含 call ID、Run ID、绝对路径、原始参数、凭据或 traceback；read/write 和既有滚动、离底新输出、窄终端及无颜色降级保持兼容。P6-05D 尚未开始。
- **最终验收结论**：edit 生命周期、实时取消、Run 隔离、差异行样式与行号、空白行、unmatched 降级、二十条变化折叠、焦点展开、历史 success/failed 恢复和安全边界通过验证。

# P6-05D bash 卡片

- **卡片结构**：新增 `BashToolCard`，只接收已清洗的 Bash `ToolPresentationSnapshot` 与 `ToolOutputDelta.text`。命令区以 `$ ` 加安全命令纯文本显示，输出区保存安全动态缓冲；卡片复用基础生命周期、状态颜色和 `(run_id, call_id)` 稳定身份，不保存原始请求、工具结果、进程或管道对象。
- **动态输出投影**：`ToolPrepared` 创建 pending 卡片，`ToolStarted` 原地切换 running，匹配 running Bash 卡片按 Engine seq 顺序追加 `ToolOutputDelta`。非 Bash、未知卡片、错误 Run、迟到事件和终态卡片均被拒绝；不重新清洗、排序、持久化或创建后台输出任务。`ToolFinished` 用最终安全 output 覆盖动态缓冲，最终快照因此不会重复显示。
- **命令与输出折叠**：命令按当前卡片宽度计算真实硬换行和软换行，默认保留前三条显示行，超出部分显示固定剩余数量；详情 `Ctrl+O` 展开完整命令。运行期间输出视口最多显示最近八条逻辑行；终态默认保留最后八条，更早内容显示固定 earlier lines 提示，展开后显示完整最终输出。LF/CRLF、空白逻辑行、Unicode、尾部无换行文本和 `[REDACTED]` 均保留可读语义。
- **输出跟随与内部浏览**：运行期间卡片自动跟随最新八条输出，但不会改变 transcript 自身的离底状态。卡片获得焦点后，Up/Down/PageUp/PageDown 在已保留逻辑行范围内移动内部视口；离开最新位置后停止卡片跟随，新增内容追加到缓冲并在卡片内唯一显示 `↓ N new lines`。End 返回最新视口、清除未读数量并恢复跟随；不使用 Timer、Worker、线程或后台 Task。
- **终态显示**：成功使用绿色状态，空输出只显示一次 `(no output)`；非零退出使用红色状态并在输出后显示 `exit N`；取消、启动失败、超时、输出限制或 Run 失败不伪造退出码，保留已接收安全输出并显示固定安全原因。失败、取消和中断同时依靠文字与颜色表达。
- **Run 与历史边界**：`RunFailed`/`RunCancelled` 只收口对应 Run 中 pending/running 的 Bash 卡片，已完成卡片不重染。v3 合法 success/failed Bash presentation 恢复为默认折叠的最终卡片，不播放动态输出、不重跑命令、不读取工作区；取消 Run 不提交 TurnEntry，不构造 cancelled 历史。v1/v2 或缺少 Bash presentation 的历史继续显示 `tool bash · recorded`，read/write/edit 正式卡片保持不变。
- **安全与兼容边界**：render 与固定 `repr` 不包含 call ID、Run ID、绝对路径、Shell 可执行文件路径、原始异常、参数或凭据；命令和输出只来自安全 presentation/事件。现有 transcript 跟随、离底提示、唯一工作状态条、模型输出、read/write/edit 卡片、窄终端和无颜色文字识别保持兼容。P6-05E、P6-06 尚未开始。
- **最终验收结论**：Bash 生命周期、动态输出顺序与最终校准、命令三显示行折叠、八行输出视口、内部浏览与未读提示、CRLF/空白行、成功无输出、非零退出、无退出码失败/取消、Run 收口、v3/旧历史恢复、安全边界及 wheel 导入覆盖通过验证。P6-05E、P6-06 尚未开始。

# P6-05E 通用工具卡片

- **目标与结构**：新增 `GenericToolCard` 作为没有专用展示实现的扩展/未知工具安全兜底。卡片继承统一 `ToolCard` 状态、语义背景和稳定组件身份，固定为两行紧凑布局；第一行只显示 snapshot 的工具名，第二行显示生命周期状态，不创建参数区、结果区或空详情面板。
- **状态与安全文案**：pending 显示 `waiting`，running 显示 `running…`，success 显示 `completed`，失败显示 `tool execution failed`，取消显示 `operation cancelled`。实时失败状态额外映射为 `permission denied` 或 `tool arguments are invalid`；历史 GENERIC failed 无具体结果状态时统一使用 `tool execution failed`。卡片不显示 `tool details are unavailable`、原始 message、参数、结果、异常或 traceback，`repr` 固定为 `<GenericToolCard>`。
- **统一解析接缝**：`create_tool_card()` 同时服务实时 `ToolPrepared` 和 v3 历史恢复，顺序为内置专用 kind、匹配 GENERIC 工厂、Generic 兜底；ASK 返回 `None` 留给 P6-06。`ToolCardFactory` 仅接收 `ToolPresentationSnapshot`，`MicaTuiApp` 构造时校验并复制 `Mapping[str, ToolCardFactory]`，调用方后续修改不会影响 App。非 Mapping、非法键和非 callable 值使用固定安全诊断。
- **工厂回退**：内置 read/write/edit/bash 路由优先且不可覆盖。自定义工厂异常、返回非 `ToolCard` 或返回 call/tool/kind 身份不一致的卡片时，静默回退 Generic，不捕获 `BaseException`，不向用户或历史写入错误信息；实时与历史使用同一解析规则。
- **生命周期与兼容**：GENERIC 按 `(run_id, call_id)` 创建并原地处理 `ToolPrepared → ToolStarted → ToolFinished`；RunFailed/RunCancelled 只收口对应 Run 的 pending/running 卡片。Generic 不接收 `ToolOutputDelta`。合法 v3 success/failed 快照恢复为终态卡片，不重放 Run；v1/v2 或缺失快照继续显示 `tool <name> · recorded`。既有 read、write、edit、bash 卡片和滚动、窄终端、无颜色降级保持不变。
- **验收结论**：新增代表性测试覆盖五种可见状态、原地生命周期、Run 隔离与收口、动态输出拒绝、v3/旧历史、工厂优先级/快照复制/异常回退、ASK 边界和安全不泄漏；P6-06 及后续任务未开始。

# P6-06 结构化 ask 面板

## 稳定设计

- `AskInteractionView` 是 transcript 中唯一的 ask 生命周期组件，接收公开 `AskPrompt`、run/interaction 身份和前端无关的同步响应/取消回调；不持有 Broker、Engine、Provider、Store、ToolRequest 或 ToolResult。
- 结构化选项从第一项开始选择，支持边界内 Up/Down、ASCII 数字定位、明确的 recommended/description、`allow_custom` 自定义回答和 `n` 备注编辑。无选项时直接使用面板内单行自由文本输入，所有 ask 回答都不进入普通 composer 或 UserMessageCard。
- responder 返回精确 `True` 后面板锁定等待真实 ToolFinished；Esc 使用相同的安全回调边界请求当前 run 取消，只有回调接受后锁定。回调异常安全吞掉且不泄漏细节，失败返回保持可交互。
- ToolFinished、RunFailed 和 RunCancelled 都在原组件上收口；成功为绿色只读答案记录，失败/取消为固定安全文案的红色记录。终态组件继续支持 `Ctrl+O`，展开内容只读，尺寸变化复用 transcript 的跟随通知。

## 历史与展示兼容

- v3 ASK presentation 恢复为只读历史 ask 记录，仅显示持久化的安全 question/answer 和终态，不伪造缺失的 title、options、recommended、selected index 或 notes；旧版本或缺少快照继续显示 `tool ask · recorded`。
- ASK prepared presentation 允许结构化参数存在但只保存经过边界处理的 question；final presentation 同时兼容裸字符串和 runtime 三字段 Mapping 中的 answer，payload 仍只包含 question/answer，原始请求与 ToolResult 不被修改。

## 验收结论

实现保持既有 Run/seq、工具卡片、工作状态、滚动、composer 和安全边界；聚焦 TUI/工具展示兼容测试通过。未连接真实 CLI、Broker、Provider 或 Engine 私有状态，未实现权限窗口和 P6-07。

# P6-07A 两模式权限后端与基础 CLI

## 目标与实现

- 删除 ACCEPT_EDITS；公开枚举与 CLI 只保留 default/solo，旧连字符及下划线参数均拒绝，不保留别名。
- default 的普通文件操作和未命中风险规则的 Bash 自动允许；受保护文件、危险命令、无法理解的 Shell 和自定义工具通过现有 Broker 一次确认。拒绝不调用 handler，批准仅作用于当前调用。
- solo 在注册查询、Schema 校验之后跳过事实工厂、风险规则、allow_rules 与 hard_denied；保留取消、工具失败与 ask 的需求交互，不新增 PermissionResponse。
- 文件 handler 只负责执行能力，不再永久拒绝敏感路径；集中敏感分类同时提供工作区路径与纯词法入口，默认 Executor 即使未注入 workspace 也会识别敏感文件名。相对路径、链接、设备、UTF-8、大小与唯一匹配限制不变。

## 取舍与边界

- 独立 command_risk 只分析文本，不执行探测或读取脚本。支持引号、转义、组合命令、常见 env/command 包装及危险命令 basename/.exe；任一命中只对原始调用询问一次，不改写命令。
- 动态扩展、复杂结构、文件描述符语法、不支持的包装选项和 Git 工作目录/配置选项保守询问；普通 Git status/diff/log/show/add/commit、脚本和网络命令未命中规则即可执行。此处未建立完整 Shell 解释器。
- 没有 OS 文件/网络隔离；脚本、未知程序与间接副作用不作安全证明。`PolicyOperation.SAFE_READ_ONLY_COMMAND` 仅作为历史兼容枚举保留，Bash 默认事实始终使用 `COMMAND`，普通命令是否自动执行由风险事实决定。
- 事件、会话版本、展示过滤、五工具顺序和 CLI 生命周期未修改；未实施 P6-07B TUI 权限面板。

## 验证与学习入口

- 新测试覆盖纯分类、组合命令一次授权/拒绝、solo 策略跳过、受保护文件实际读写编辑、普通网络假 handler、参数/能力限制和取消清理。真实文件只使用临时目录与虚构内容，危险命令仅分类或交给记录调用的假 handler。
- 旧授权测试改为显式询问操作或受保护路径，保留生命周期和副作用断言；动态 Bash 输出测试区分默认自动执行与保守询问。安装态覆盖两模式的 Unicode 工作区写入与 continue 历史恢复。
- 学习入口：command_risk → ToolExecutor → InteractionBroker；文件执行能力与授权策略分离；Schema 校验、权限策略和 ask 需求交互的不同职责。
- 验收状态：返工聚焦组 192 通过；最终全量 1457 通过、2 跳过，Ruff、格式检查、离线构建和两项 diff check 通过。待 master 审查；未提交或推送 Git。

# P6-07B 权限确认面板

## 目标与实现

- 新增独立 `PermissionInteractionView`，把权限请求作为 transcript 外的底部临时面板展示；安全详情只读取匹配 `ToolPrepared` 的 `ToolPresentationSnapshot`，Bash、文件工具和 Generic 工具分别使用固定的纯文本降级路径。
- TUI 通过公共 `PermissionResponder` 接收一次性的 `PermissionResponse`，初始选择 Yes，ASCII 数字只定位、Enter 才提交，Esc 只调用既有 `RunCancelRequester`。严格同步回调、意外 awaitable、异常和缺失回调均在固定安全边界内处理。
- `MicaTuiApp` 按 `(run_id, call_id)` 与 `interaction_id` 维护准备记录、活动面板和完成去重；重复、倒序、迟到、错误工具身份、错误 Run 和没有合法准备记录的权限事件不会创建可批准面板。ToolStarted、ToolFinished 和 Run 终态只收口匹配权限状态，不伪造工具生命周期。
- 面板动态挂在唯一 work-status 与 composer 之间，composer 草稿保留并在活动 ask 或权限交互仍有效时继续禁用；面板退出后按既有焦点规则恢复，不进入 transcript、历史或会话格式。

## 取舍与边界

- 复用 ask 面板已有的同步回调检测、严格 `True` 判断和 awaitable 清理逻辑，不引入第二套权限策略，也不读取原始工具参数、ToolResult、工作区或 Shell 状态。
- 权限请求的公共 `message` 仅作为事件语义，不直接渲染；异常、repr、interaction ID、call ID 和 Run ID 不进入面板文本。没有安全展示细节时使用固定说明，并保留有界滚动和纯文本换行能力。
- 未连接真实 Provider、Broker、Engine 或 CLI，未修改事件、会话和后端权限策略；P6-08 尚未开始。

# P6-08 运行控制器与取消收口

## 目标与实现

- 新增 `MicaTuiRuntime`，公开用法为先用已装配的 `MicaApp` 构造 `runtime = MicaTuiRuntime(app)`，再运行 `await runtime.tui.run_async()` 或在测试中使用 `async with runtime.tui.run_test()`；外层也可直接调用 `await runtime.aclose()`。
- 控制器集中拥有唯一活动 Run 的 `CancellationToken`、消费任务、真实 `run_id` 和 `MicaApp.run()` 流引用，使用 `async with aclosing(...)` 顺序投影 `EngineEvent`，并通过 `MicaApp.respond_interaction()` 转发权限与 ask 响应。
- `MicaApp.run()` 增加向后兼容的 `cancellation` 关键字接缝；原有调用保持有效，合法 token 原样传入 Engine，成功提交路径继续只由 MicaApp 写入一个完整 `TurnEntry`。
- TUI 增加同步 `RunRequester` 提交接缝；运行期间锁定 composer，第二次提交、迟到取消和关闭后的交互均被拒绝。TUI 退出与 runtime `aclose()` 共用停止输入、取消活动 token、等待消费任务、清理临时交互、关闭 MicaApp 的生命周期核心。

## 边界与验证

- 正常完成、真实取消、消费者关闭、重复关闭和 Provider 未暴露内部信息的代表测试使用临时会话目录与假 Provider；不连接外部网络、不使用真实凭据、不执行危险命令。
- 取消和关闭不伪造 `RunCancelled`、工具结果或完成事件；pending 交互交给既有 Broker/Engine 清理，未完成 Run 不写入会话，Provider 由 MicaApp 保持幂等关闭。
- P6-09 默认 CLI 路由、安装态完整对话和真实 Provider 接入尚未开始。

# P6-09 CLI 路由与安装态闭环

## 目标

将已完成的 Textual TUI 接入正式 `mica cli` 装配链路：默认启动 TUI，`--plain` 保留基础文字界面，并让两种前端共享同一套会话选择、上下文、系统提示词、工具、权限模式、Provider 与 `MicaApp`。

## 关键装配与路由

- CLI 先完成工作区、SessionService、会话选择、过期清理、一次性 ContextBuilder 构建、系统提示词构建、InteractionBroker、固定顺序工具注册、ToolExecutor、Provider 和 MicaApp 装配，再按 `--plain` 选择前端。
- TUI 路径使用 `MicaTuiRuntime`，注入本次选中的 `StoredSession`、`ContextBuildResult.loaded_sources` 和实际 `ToolExecutor.tool_specs` 生成的 `StartupResourceSnapshot`；Skill/扩展保持空集合。
- 直接调用 `run_interactive()` 的既有测试/调用方继续默认使用 plain，正式 `mica cli` 明确传入默认 TUI 路由。

## 安装态闭环

安装态验证使用仓库外、隔离 HOME、包含 Unicode 与空格的工作区、loopback SSE 和虚构凭据；TUI 通过最小 runner 接缝使用 Textual Pilot 验证工具对话、退出和 `--continue` 恢复，不通过源码工作树冒充安装态。

## 安全与生命周期边界

TUI runner 只接收已装配的 `MicaTuiRuntime`，不接收 Provider、Engine、Broker、Store 或凭据。TUI 正常退出、runner 异常、主任务取消和未挂载 Screen 的构造失败路径均最终调用 runtime/App/Provider 清理；启动失败使用固定安全诊断，不回显凭据、URL、绝对路径、原始异常或 traceback。P6-10 尚未开始。

# P6-10A 启动首屏视觉校准

## 目标与实现

- 启动 transcript 现在按无伴随字标的响应式 Logo、定位/ready 文案、Context/Skills/Extensions/Tools 分区资源框和真实就绪说明的顺序从顶部呈现；新会话保持 `scroll_y == 0`，恢复历史仍定位最新内容，启动区不生成模拟模型问候或会话消息。
- 外壳使用中性纯黑画布和近黑表面色；composer 由单个 TextArea 持有完整四边框，gutter 提供品牌色 `› `，空草稿宽屏在同一内容行显示真实 Enter/Shift+Enter 提示，不引入 ANSI 或第二层边框。
- composer 下方保留两行低干扰事实区：第一行 workspace 居左、model 居右，第二行使用 `sessions:` 加真实标题或 Session ID；快照只含显示用 workspace、session label、model，使用 cell width 做显示层省略，不显示权限、耗时、工具数或快捷键栏。
- 用户复审发现的重复小写 `mica`、模拟 `› Mica` 问候、未实现快捷键提示，以及 `W:/P:/M:` 等事实噪声已从对应视图结构中删除。CLI 按 `selected.meta.title.strip() or selected.meta.session_id` 装配会话标签，并为实际加载的 global/workspace `AGENTS.md` 注入安全相对来源路径；未加载或缺少兼容路径时显示 `none`。
- 首轮截图暴露的 24×8 事实不可读、滚动条过重、资源框过宽以及启动层级偏响的问题仍由既有规则解决：事实区始终固定两行，滚动条为单列低对比度样式，资源框收窄为 68 列并将 Context 融入顶边标题。
- 返工后的响应式规则在 110×36、100×30、80×24、40×16 和 24×8 保留可用 transcript、唯一状态条、完整 composer 与事实区；删除快捷键栏释放的高度交还 transcript，极窄尺寸保留 Workspace、Model 和 `sessions:` 的可发现性。

## 边界与验证

- 本切片只调整启动首屏和固定底部控件；消息、推理、工具卡片、ask、权限、运行语义以及 Engine/Provider/会话存储均未改变。
- Textual/Pilot 覆盖顶部初始位置、历史恢复、快照安全、真实注入、完整边框、长 Unicode 事实、固定事实高度、宽屏/约 80 列/40×16/24×8 和 NO_COLOR；真实 Textual 组件树截图已按 110×36、100×30、80×24、40×16、24×8 生成，并与本轮删除目标、负向问题图、Pi 布局参考及正向对照图检查。
- 使用者已在真实 Windows 终端完成视觉复验；延迟 resize/refresh 回调在流式光标终态移除后安全成为 no-op，终态清理保持幂等。统一自动验证、静态检查和离线构建通过，P6-10A 完成。

# P6-10B 运行界面校准

## 目标与实现

- 本切片只校准已确认的运行界面视觉差异，不改变 ask/permission/tool/status 的公共回调、一次响应或取消语义，也不开始 Phase 7/8。
- Ask 面板在选项模式下原先只挂载隐藏 Input；`height: auto` 按子节点而不是父级 `render()` 计算，导致 110×36 只剩青色空框。现在由 `_AskContent` 承担选项/提示正文高度，Input 仍只在 free/custom/notes 模式显示。
- 权限面板在宽终端使用底边距，使 `↑/↓ to navigate` 与唯一 `Mica · working...` 状态条分开；24×8 去掉纵向 padding，使两行 facts 贴在屏幕底部而不溢出。
- 真实 Textual 组件树截图已按对话、工具、ask、权限、历史滚动、24×8 和 NO_COLOR 重新生成；Ask 选项正文、权限提示空隙和 bash 终态 `48 passed` 均可见。
- 首轮截图对照曾通过自动 Design QA；使用者随后在真实 Windows 终端复查中确认仍有会影响后续功能的基础问题，因此本轮只建立可回退检查点，不关闭 P6-10B 或 Phase 6。

## 边界与验证

- 自动化测试覆盖 Ask 挂载后的选项可见高度，权限面板在 40×16 的内部滚动、24×8 的挂载/不重叠/facts 贴底，以及 110×36 的状态条空隙。
- 当前 Ask 与权限布局修复及其自动验证作为阶段内检查点收口；后续继续依据使用者的真实终端反馈修复基础问题，完成复验后再宣布 P6-10B 和 Phase 6 完成。
