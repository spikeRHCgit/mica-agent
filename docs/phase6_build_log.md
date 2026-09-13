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

# P6-10C 对话时间线与模型步骤分段

## 目标与实现

- 同一 Run 内的思考/正式回复不再按整个 `run_id` 复用一个组件。活动映射仍按 `run_id` 指向当前模型步骤；首个被接受的 `ToolPrepared`（含 ASK）结束该步骤：稳定 Markdown、移除流式光标，并解除活动映射，DOM 位置保持不变。
- 同批后续工具不会再生成空回复。工具完成后的新 Delta 在上一批工具卡片之后挂载新的思考/回复视图，transcript 顺序与 Engine 事件一致。
- 无效、重复、乱序或迟到的 `ToolPrepared` 不收口当前步骤。Run 终态只结束当前活动回复。

## 边界与验证

- 不改 Engine/事件契约、Ask/权限公共回调、一次响应或取消语义，也不开始 P6-10D/E。
- 聚焦测试覆盖两步两工具 DOM 顺序、拒绝边界、多工具无中段空回复、ASK 分段、失败/取消与迟到 Delta、真实 `MicaTuiRuntime` 两轮假 Provider、历史/跨 Run 隔离和离底 `↓ new output`。
- 临时 110×36 截图已与时间线正负向参考对照：模型步骤、工具卡片、下一模型步骤沿单一时间线交错出现。自动化验收通过后，视觉完成仍由使用者在真实 Windows 终端确认。

# P6-10D 全局 Run 中断与两次 Ctrl+C 退出

## 目标与实现

- `MicaTuiApp` 用高优先级 `escape`/`ctrl+c` 绑定接管全局控制，不再依赖 Ask、权限、composer 或 transcript 保有焦点。
- `Esc` 只取消当前已投影且未终止的真实 `run_id`：调用既有同步 `run_cancel_requester`，不伪造 `RunCancelled`、空回答或 `PermissionResponse(DENY)`。空闲 `Esc` 是 no-op。
- 第一次 `Ctrl+C` 永远不退出；活动 Run 同时请求取消并进入约两秒确认窗口，空闲时只布防。窗口内第二次才 `exit()`，超时后重新作为第一次。时间比较使用注入的 `_clock`，不创建 Timer/Worker/后台任务。
- 取消成功后立即收口活动 Ask 记录并卸下权限面板，避免 App 级绑定抢先消费按键后局部 `on_key` 再触发第二次回调。清理仍走 `MicaTuiRuntime.cancel()`、CancellationToken、Engine、Broker、Provider 与 Bash 既有链路。

## 边界与验证

- 不改 Engine/事件契约、Ask/权限组件、CLI 或 P6-10C 时间线分段。未开始 P6-10E 交互覆盖层与失焦恢复。
- 聚焦测试覆盖任意焦点下的一次 `Esc`、空闲 no-op、Provider 流式取消、Bash 进程回收、Ask/权限不合成回答、两次 `Ctrl+C` 与超时重置。
- `runtime.py` 与 `cli.py` 未修改；第二次退出仍由 `_RuntimeTuiApp.exit()` 进入 `request_shutdown()`/`aclose()`。

# P6-10E 统一交互覆盖层与焦点恢复

## 目标与实现

- 新增唯一 `InteractionOverlay`：空闲时不进入 `compose()`，Ask/权限等待时懒创建并 `display` 显隐。活动交互独占底部，隐藏 work-status/composer/facts；transcript 与工具卡片继续可见。composer 身份与草稿保留。
- Ask 使用 overlay 活动视图加 transcript 惰性副本：Textual 不能把同一 widget 从 overlay 再挂到 transcript。Permission 关闭后不进入历史，既有工具卡片原地更新。
- App 级高优先级绑定路由 Ask/权限的上下、Enter、数字、`n` 和滚动键；`check_action()` 只在存在活动交互且 `owns_key()` 为真时启用。空闲数字/Enter 仍归 composer。Ask 输入/备注模式不抢数字和 `n`。
- 按键所有权按活动 Ask/权限状态判定，不依赖 overlay 子节点或挂载时 `focus()`。PageUp/PageDown 在权限面板上滚动 overlay 内容，不移动 transcript。
- `AppFocus` 与交互挂载/收口按 Ask、权限、运行中 transcript、空闲 composer 恢复焦点。chrome 同步默认不抢焦点，避免 Bash 卡片内部滚动被打断。
- 保留 P6-10C 时间线分段和 P6-10D `Esc`/`Ctrl+C`。不改 Engine/事件、runtime、CLI，也不伪造 Ask 回答或 Permission DENY。

## 边界与验证

- 新测试覆盖 overlay 替换底部控制区、无本地焦点的 Ask/权限按键、Ask 终态记录位置、权限不进 transcript、极端终端仍保留一行 transcript、`AppBlur → AppFocus` 恢复以及空闲数字键仍属 composer。
- 自动化验收通过后，真实 Windows 终端的键盘与失焦行为仍由使用者确认。未开始 Phase 7/8，不宣布 Phase 6 完成。

# P6-10F Windows 终端输入与焦点可靠性

## 目标与实现

- 真实 Windows 终端冒烟暴露三组输入问题：长时间失焦后 Ask/权限导航与 transcript 滚动失效、单次 `Ctrl+C` 仍可能直接退出、composer 可见光标与编辑位置不可靠。
- 本地根因证据：Textual 8.2.8 的 win32 `EventMonitor` 只处理 `KEY_EVENT` 与 `WINDOW_BUFFER_SIZE_EVENT`，声明的 `FOCUS_EVENT_RECORD` 从不读取；Windows Terminal 会通过 DECSET 1004 的 `ESC[I`/`ESC[O` 汇报焦点，但该路径依赖终端版本与设置。因此恢复不能只绑定 `AppFocus`。
- `MicaTuiApp` 在 Key、鼠标滚轮和 Resize 分派前执行幂等的输入所有权校正：只修复无效、已脱离 DOM 或不属于当前活动交互的焦点；活动 Ask、活动权限优先，其次运行中 transcript，最后空闲 composer。首个按键或滚轮本身直接生效，不吞掉用户操作。
- 无阻塞交互时 PageUp/PageDown 由 App 级路由滚动 transcript；聚焦 Bash 卡片时保留其内部浏览与边界冒泡。`app_focus` 在 App 入口恢复，覆盖被 App 拦截消费的按键路径。
- `Ctrl+C` 只有一个退出状态机：第一次布防（活动 Run 同时调用一次既有取消接缝），约两秒内第二次独立按键才退出。新增基于可注入时钟的同按键去重窗口，合并同一输入批次内的近同步重复投递（按键与原生信号、或重复分发）。
- 平台限制（如实收窄，非已解决）：Textual 8.2.8 的 win32 事件转换丢弃 key-up 记录、忽略 `wRepeatCount`，其 `Key` 事件也不携带按下/重复/释放标记；Kitty 扩展序列里本版本同样丢弃事件类型字段，且 Windows 控制台路径不产生该序列。因此“去重窗口之后到达的自动重复”与“新的独立按下”在产品可观察层无法区分，持续按住 Ctrl+C 超过系统重复延迟仍可能被计为第二次按键。该边界不通过自动测试声称已识别，而由真实 Windows Terminal 的手工复验（一次短按不退出、两次明确短按才退出）确认。
- `cli.py` 在 TUI 期间安装最小 SIGINT 接缝：把原生 Ctrl+C 转入 App 的同一状态机，自身不执行清理；App 退出或 runner 异常后恢复原处理器，非主线程场景跳过；`--plain` 行为保持不变。
- composer 删除会丢弃基类 `_on_mouse_down` 协程的同步覆写（`coroutine was never awaited`），鼠标定位继续由基类处理器与 `watch_selection` 完成。渲染、`cursor_screen_offset` 与 `get_target_document_location()` 现在共用同一提示符偏移：`› ` 只在首个可见行占两格，非首行的硬件/IME 光标与鼠标命中都不再额外偏移两格。

## 边界与验证

- 不改 Engine/事件契约、会话格式、Provider、权限策略或中断轮次持久化；未开始 P6-10G，未宣布 Phase 6 完成。
- 聚焦测试覆盖：无 `AppFocus` 时 Ask/权限第一发 Down、transcript PageUp 与鼠标滚轮、resize 自愈、Run 终态 composer 恢复、Screen 卸载安全 no-op、Bash 卡片消费与边界冒泡不回归；composer 中间插入/删除、多行与 Unicode、软换行、点击定位和无未 await 协程；Ctrl+C 真实时间推进下的近同步重复、确认窗口内第二次、超时重置与关闭幂等，以及去重窗口之外事件按平台限制计为第二次的边界。
- composer 坐标统一：`render_line()`、`cursor_screen_offset` 与重写的 `get_target_document_location()` 共用同一提示符偏移规则（`› ` 只在首个可见行占两格）。聚焦测试覆盖第二/第三逻辑行、软换行后的第二视觉行、内部滚动后的首个与后续可见行、Unicode 与宽字符、点击后插入/Backspace/Delete 的实际位置，以及长粘贴片段点击后仍保持原子。
- 真实 Windows Terminal 的长时间失焦、两秒双击与 composer 编辑仍由使用者手工复验后关闭；自动化不能替代该确认。持续按住 Ctrl+C 的自动重复边界属于上述平台限制，同样由手工复验确认（作为已知风险，不作为已完全解决）。

## P6-10F 返工：自动跟随竞争与交互所有权

### 根因一：滚动失效不是焦点事件缺失，而是自动跟随竞争

- 准确链路：PageUp 只安排一次延迟滚动，`scroll_y` 与 `watch_scroll_y` 尚未生效时，流式 `TextDelta`/`ToolOutputDelta` 已经看到 `_following=True`，于是排下强制 `scroll_end()`，把用户刚刚的 PageUp 覆盖。多次失焦只是放大器（失焦期间输出更多、回调更多）。
- 另一层冲突是双重跟随状态：Textual `anchor()` 与 Mica `_following` 同时想控制“是否跟随底部”，没有单一所有者。
- 修复：`ConversationTranscript` 独占跟随状态并移除 `anchor()`；PageUp/方向键/滚轮上滚/滚动条点击拖动/Home 先在入口同步置 `_following=False`，再委托滚动；自动跟随请求合并为一个待执行回调，并在执行前复查跟随状态，用户一旦离底旧回调自动失效；落底改由布局变化（`_scroll_update`）同步完成，不再依赖 `call_after_refresh` 时序；`End` 立即落底并恢复跟随，`_following` 只在到底或显式 End 时恢复；全新会话的启动首屏保持顶部对齐。`scroll_visible` 等程序性揭示不再被误判为用户意图。

### 根因二：权限面板是多重所有权，不是 Down 选择算法

- `apply_key("down")` 本身正确；问题是 owner 依赖 `_active_permission_key` 指针，与 `_permission_views`、overlay 已挂载子组件和 `screen.focused` 可能分叉。指针一旦失效，App 级路由找不到 owner，失焦后的第一发按键就落不到可见面板上。
- 结构问题还包括：`PermissionInteractionView` 自己继承 `VerticalScroll`，与外层 overlay 形成嵌套滚动；页面键既可能被 App 路由又被面板本地 `on_key` 处理。
- 修复：overlay 是唯一滚动容器，`PermissionInteractionView` 改为普通可聚焦组件，只保存选择状态与渲染；owner 事实源改为 `InteractionOverlay.active_view`（已挂载、仍活动、且 overlay 可见），`_active_permission_key` 只作为业务关联索引（响应接受与取消请求仍由它把关）；页键一律滚动 overlay；删除面板本地 `on_key`，App 级交互控制器是唯一按键入口。

### 返工验证

- 新增回归顺序：PageUp 后同一事件循环内 `content_added()`，刷新后仍离底且只有一个 `↓ new output`；先排队的跟随回调在用户接管后失效；连续失焦/聚焦加流式输出保持离底；`End` 后恢复跟随；业务索引与焦点同时失效时第一发 Down 仍生效；页键滚动 overlay 且 transcript 不动。
- 权限面板的滚动断言随架构一起迁移到 overlay（面板 `max_scroll_y == 0`）；Bash 卡片测试的聚焦改为 `scroll_visible=False`，与产品聚焦约定一致。
- 真实 Windows Terminal 的滚动抢占、失焦恢复与页键行为仍由使用者手工复验；自动化不能替代该确认。

# P6-11A 主缓冲区核心纵向闭环

## 目标

在现有脏工作树上建立不进入 alternate screen 的主缓冲区终端前端，通过临时 `mica cli --main-buffer-preview` 跑通「启动首屏 → composer 编辑 → 提交 → 真实 `MicaApp.Run` → 流式文本 → Ask/Permission 阻塞 → Esc/Ctrl+C → 工作状态 → 输入区恢复 → 正常关闭并保留主缓冲区历史」的完整纵向闭环。默认 Textual 入口与 `--plain` 语义不变，旧前端不删除，不开始 P6-11B/C 与 P6-10G。

## 包边界与 IO 所有权

- 新代码只放在 `src/mica_agent/tui/main_buffer/`，不导入 Textual：`terminal.py`（唯一 IO 所有者）、`sanitize.py`（不可信正文的唯一清洗接缝）、`renderer.py`（语义文档与差分帧）、`components.py`（纯文本组件）、`editor.py`（草稿、软换行与粘贴片段）、`interactions.py`（ask/权限独占模式）、`runtime.py`（单 Run）、`screen.py`（模式机与编排）。
- 除 `terminal.py` 外任何模块都不写 stdout、不读原始输入；渲染器与组件只返回字符串或语义块，使测试能直接断言逻辑帧而不依赖终端。
- `FORBIDDEN_SEQUENCES` 只作为文档化样例；真正的防线是 `find_forbidden_sequences()`：它按 ANSI 序列语法解析**实际发出的完整字节流**，因此未知私有模式、整屏擦除、窗口标题与剪贴板操作都会被检出，而不是只匹配固定字符串。

## 输入与渲染的关键决定

- 依赖：新增直接依赖 `prompt-toolkit>=3.0.52`（锁定 `3.0.53`），只使用 `create_input`、`raw_mode`、`attach`、`read_keys` 与 `Buffer`/`Document`，不引入其 Application 与全屏渲染，采用自有渲染与自有按键分派。
- Windows 低层语义：`Win32Input.read_keys()` 是非阻塞轮询，`attach(callback)` 才是输入就绪信号源，因此模式机使用「就绪回调内排空 `read_keys()` + 线程安全入队」，而不是阻塞式读取。
- 差分渲染（物理模型）：渲染器记录上一帧的真实物理状态：动态区行数、帧内光标行、可重写尾部块的起始行，以及已经作为永久行写出的 pending 前缀行。首帧只从当前光标位置向下写出，不先发 Cursor Up、不擦除启动前内容；后续帧**先**按上一帧状态回到「本次仍需重绘的第一个块」的起点并擦除其下的旧尾部，**然后**才写出内容，因此正文绝不会从 composer 光标处开始写。超出屏幕预算的行在同一个擦除点之后作为一次性永久输出向下写出（自然进入 scrollback），随后写出不超过屏高的可重绘帧并把光标放回 composer 单元格；已经被永久写出的前缀行不会重复写出，也不会重复重绘。
- 不可信正文边界：用户输入与历史消息、模型正文、工具可见字段与输出、`RunFailed` 可见信息、Ask 问题/选项/备注、权限工具名与预览、以及 session/model/workspace/branch 等动态事实，都先过 `sanitize.py`；它整段删除 CSI/OSC/DCS/C1 与 C0 控制内容、归一 CRLF、保留正常 Unicode，而且从不插入新的 `ESC`，因此清洗结果无法再组成控制序列。`Terminal.write()` **不**做无差别清洗：渲染器自己生成的光标与擦除序列必须原样写出，可信控制与不可信正文只在语义正文边界区分。
- 预着色行：带 ANSI 的 Logo 与资源面板行标记为 `styled`（先按纯文本测量/截断、后着色，且宽度不超过终端），不参与换行、宽度测量与二次着色，避免按列切分 ANSI 序列。
- composer：按终端单元格软换行（CJK、Emoji、组合字符不拆错），显示最多八行并保持光标行可见，上下隐藏行数标在边框上；达到阈值的 bracketed paste 折叠为 `[paste #N +M lines]` 原子片段（整体跨越、一接触就整体删除、提交还原原文、transcript 只显示摘要，手打同名摘要不会冒充真实片段）。草稿正文不做 `strip`：前导/尾随空格与空白行保留，只清控制内容；制表符在显示层按 `TAB_WIDTH` 确定性展开为空格，软换行、显示文本与物理光标单元格共用同一套宽度计算，`submit_text()` 仍返回带真实 `\t` 的原文。提交顺序是「先交给运行时，返回 True 才清空草稿并进入 running」：被拒绝时草稿、光标、粘贴片段与编号全部保持原样，不追加用户消息、不伪造 Run、不启动工作动画，也不暴露内部原因。界面不再显示当前输入层无法交付的 `Shift+Enter` 提示，临时入口保留 `Ctrl+J` 换行。
- 生命周期：`run()` 把输入获取、raw mode、autowrap 关闭、attach 与首帧都纳入同一清理边界；raw mode、autowrap、attach、`Input.close()` 与 App 关闭各自独立记账且相互隔离，单项失败不会阻断其他清理，也不以异常形式打扰使用者（包括 `clear_chrome` 自身的 write 失败：仍会 detach 输入、恢复 autowrap、离开 raw mode、关闭输入对象、调用一次 `aclose` 并标记 `closed`）。CLI 在失败路径兜底关闭业务资源，正常关闭后不重复关闭 Provider。
- resize 与状态刷新：终端尺寸在每次绘制前重新读取，只重排可重绘区、chrome、composer 与交互面板，不改变草稿文本、逻辑光标、交互状态或事件顺序；工作状态由**唯一**刷新任务推进 `working.` 点动画，交互面板活动时隐藏并在回到 running 后恢复，`done` 显示真实耗时约两秒后淡出、`stop`/`error` 持久到下一轮，关闭时取消并等待该任务。run 恢复 idle 后用公开接缝刷新 Git 分支，并为模型切换/会话重命名保留 `update_session_snapshot()` 入口，不用定时器轮询。
- 降级：颜色与交互能力分离；`NO_COLOR`/`TERM=dumb` 只关闭颜色。输出不是交互式终端时前端拒绝启动（`--plain` 仍是基础文字降级入口），不向管道写入光标寻址序列。
- 模式机：唯一活动模式 `IDLE/RUNNING/ASK/PERMISSION/CLOSED`；终端失焦不改变模式。`Esc` 只在活动 Run 时取消（空闲 no-op）；第一次 `Ctrl+C` 永不退出（活动 Run 同时请求取消），两秒窗口内第二次才退出；`Ctrl+D` 采用最小规则（当前没有更细的 CLI 事实源）：只在空闲且草稿为空时退出，有草稿、活动 Run 或交互模式时忽略，输入流自身的 `EOFError` 走同一条规则，都不删字符也不伪造 Run。
- Ask 与权限面板在选择期间替代工作状态、输入框与事实区，回答或取消后恢复 composer；权限仍只提供一次允许与拒绝，`Esc` 的语义是取消 Run 而不是拒绝本次调用。

## 事件投影

- 同一模型步骤内的连续 Delta 更新同一个可重写尾部块；首个工具事件或交互事件结束该步骤。
- 一次工具调用只产生一条记录，按 `ToolPrepared → ToolStarted → ToolOutputDelta → ToolFinished` 原地更新同一尾部块，收口后提交为永久记录；Ask/权限等待期间该记录保持可重写，因此回答后仍能收到真实终态，不会留下永久 pending 记录。
- `RunCompleted` 显示实际耗时的 `done(Ns)` 约两秒后淡出，`stop`/`error` 保留到下一次 Run；同一界面始终只有一条工作状态条。
- 事件准入：只有当前真实 `run_id`、严格递增 `seq` 与匹配的 `(run_id, call_id, tool_name)` 才会被修改；重复、倒序、未知 Run、终态之后的迟到事件都被拒绝，只记入内部诊断。`ToolFinished` 还要求最终快照的 `call_id`、`tool_name`、`kind` 与 pending 一致、状态与 `ToolFinished.status` 符合公共契约：不一致时整条事件都不投影——pending 卡片保持原状态、不提交、不清除待答工具身份，只记固定安全诊断，后续匹配的合法终态仍能收口同一张卡片。
- 终止分类：用户 Esc/Ctrl+C 取消映射为 `stop`，Provider 或消费链异常、以及无终态结束映射为 `error`；内部标记只携带最小类别（`cancelled`/`runtime_error`/`stream_ended`），真实公共终态事件仍然优先。`RunFailed` 的原始 `message` 不进入 transcript：正文只按公共 `ErrorCategory` 映射固定文案，因此绝对路径、Provider 原始响应、异常正文、Key 与 traceback 都不会到达屏幕或发出的字节流。
- 恢复会话时从已加载 `StoredSession` 只读投影用户消息、已成功/失败的 v3 工具快照与最终回复；缺少快照的旧版本降级为 `recorded`，不重放 Run、不重新执行工具。

## 验证结果

- 测试：`test_tui_main_buffer_render.py`（组件、编辑器、按键归一与交互模式）、`test_tui_main_buffer_vt.py`（虚拟终端回放器及其自检）、`test_tui_main_buffer_redraw.py`（首帧不覆盖 shell sentinel、连续同帧不上移、pending 增长与收回、短帧后 pending 超高不写入 composer、超高继续增长不重复写出、超高缩短不留残影、chrome 同时变高变矮、clear_chrome 保留正文、宽度重排、超高帧滚入 scrollback、全程无禁用效果）、`test_tui_main_buffer_sanitize.py`（22 类对抗载荷注入文档块/事实/交互面板/启动面板，共 114 例）、`test_tui_main_buffer_composer.py`（软换行、八行窗口、长粘贴片段、ask 自定义项、面板排版顺序）、`test_tui_main_buffer_composer_whitespace.py`（前导/尾随空格、空白行、Tab 展开与光标一致、软换行后的 Tab 光标、提交原文保留 `	`）、`test_tui_main_buffer_screen.py`（端到端纵向闭环、提交被拒绝时保留普通草稿与粘贴片段）、`test_tui_main_buffer_lifecycle.py`（初始化/退出路径的清理计数与幂等性、清理阶段 write 失败、`Ctrl+D` 与输入流 EOF 的最小退出规则）、`test_tui_main_buffer_refresh.py`（resize、工作状态动画、done 到期、事实刷新）、`test_tui_main_buffer_events.py`（Run/seq/call/tool_name 隔离、最终快照身份与状态契约、终态与 stop/error 分类、RunFailed 固定文案不泄漏路径与 Key、交互响应被拒绝时保留面板）、`test_tui_main_buffer_cli.py`（路由、失败兜底关闭且不重复关闭、依赖声明）。
- 分发断言同步更新：wheel 必须包含 `mica_agent/tui/main_buffer/` 九个模块（含 `sanitize.py`），`importlib.metadata.requires` 必须包含 `prompt-toolkit>=3.0.52`；既有 P6-09 路由测试的 `_main_cli` 替身增加新关键字断言。
- 统一门槛：聚焦组（含分发与既有 CLI/TUI/runtime 套件）`-W error` **347 通过**；全量 **1842 通过、2 跳过**；`ruff check` 全绿；`ruff format --check`（133 文件）通过；`uv build --offline` 成功；两项 `git diff --check` 干净。
- 视觉证据：110×36 与 40×16 的启动帧（彩色与 NO_COLOR）、resize 前后帧、Ask 与权限面板帧；另用虚拟终端把真实发出的字节流回放，证明 shell sentinel 原样保留、没有清除 scrollback、没有 alternate screen / 鼠标捕获，且退出后物理光标落在干净行。

## 边界与后续

- 本切片不包含：工具专用折叠/展开卡片（`Ctrl+O` 全局展开）、思考显示模式、粘贴片段折叠、Bash 动态输出视口、Markdown 渲染，均为 P6-11B；默认入口切换与旧 Textual 退役为 P6-11C。
- Logo 几何在 Textual 与主缓冲区两侧各有一份实现（新前端不得导入 Textual），P6-11C 删除旧前端后收敛为单份。
- 已知偏差：`Shift+Enter` 换行在当前输入层不可用（prompt-toolkit 3.0.53 没有 `ShiftEnter` 键，Windows 控制台与 Windows Terminal 默认也不交付该修饰符），因此界面不再显示该提示，临时入口保留 `Ctrl+J`；输入协议适配属于 P6-11B/C，本轮不自行发明键序列。
- 已知边界：`import mica_agent.tui.main_buffer` 仍会经由父包 `mica_agent.tui.__init__` 连带加载旧 Textual 前端（子包源码本身不含 Textual 依赖）；把旧前端包改成惰性导出属于 P6-11C 的退役工作，本轮未改。
- 终端 resize 只重新排版重绘区；已经写入 scrollback 的历史行保留写入时的宽度，由终端自身的重排行为决定。
- 多行输入：`Enter` 提交，`Ctrl+J`（LF）插入真实换行。prompt-toolkit 3.0.53 没有 `ShiftEnter` 键，Windows 控制台与 Windows Terminal 默认也不把 Shift 修饰符交付到这一层，因此“Shift+Enter 换行”当前不可用；若某个终端开启 modifyOtherKeys / CSI-u，该序列也不会被解析（可能被当普通文本）。输入协议适配属于 P6-11B/C 的输入层工作，本轮不自行发明键序列。
- 真实 Windows Terminal 的滚轮/scrollbar/scrollback、长时间失焦后的第一发按键、持续按住 `Ctrl+C` 的自动重复边界仍由使用者手工复验；自动化不能替代该确认。

## 用户真实终端验收

- 使用者已在 Windows Terminal 通过临时 `--main-buffer-preview` 完成冒烟：主缓冲区、终端原生滚动与 scrollback、composer 编辑、Ask/Permission 选择、Run 中断及退出恢复等主要功能运行正常，P6-11A 验收通过。
- 本次验收同时确认了下一切片的视觉基线：当前用户消息、模型思考、工具记录和正式回复层级仍然拥挤，正式回复尚未渲染 Markdown。该差异属于 P6-11B，不回退 P6-11A 的主缓冲区架构结论。
- 负向现状与正向层级参考分别保存为 `runtime-main-buffer-current-p6-11a.png` 和 `runtime-content-hierarchy-pi-reference.png`；后续任务必须实际打开并成对对照，不照抄参考产品的文案、数据或外围功能。

# P6-11B 历史与工具呈现迁移

## 目标

在 P6-11A 的主缓冲区架构上把近似的纯文本日志升级为有明确语义层级的终端界面：区分用户消息、模型思考、正式回复与工具记录；对正式回复做安全、宽度感知的 Markdown 渲染；恢复 read/write/edit/bash/ask/generic 的专用工具呈现与大面积状态色块；活动工具显示有界实时输出；实现全局 `Ctrl+O`；历史恢复与实时事件复用同一组视图。仍通过临时 `--main-buffer-preview` 验收，不切换默认入口、不删除旧前端、不开始 P6-11C/P6-10G。

## 语义块与模块边界

- 语义块只保存安全源数据与展示状态，并按当前宽度/调色板/全局展开状态生成物理行（`Block` 纯文本、`RenderedBlock` 冻结物理行、`UserBlock`/`ReasoningBlock`/`Markdown*`/工具视图）。业务层不再提前拼接整段不可信 ANSI；不可信正文仍在清洗接缝之后才进入排版。
- 新增 `markdown.py` 与 `tool_views.py` 两个职责单一的模块：前者只管安全边界、Rich 主题映射与物理行集成，后者只管把安全展示快照变成工具色块；编排层（`screen.py`）继续只做投影与模式机，不再堆放 Markdown 与工具解析。
- `ReasoningDisplayMode` 移到不依赖 Textual 的 `tui/models.py`，旧 `output.py` 继续导入并重导出，旧前端行为与公开导出不变。

## 物理重绘模型

- 渲染器改为**保留式屏幕映射**：为每个已写入的物理行保存逻辑身份（同一消息/工具记录被切分后的片段共享标识与连续逻辑行号）。映射行数不超过终端高度，因此映射内的每一行都仍在屏幕上，回到任意一行的上移量都不会越过屏幕顶部。
- 重绘时按身份+文本求公共前缀：普通增量只回到变化尾部；较早块高度变化（全局 Ctrl+O、宽度变化）时从该块起点受控重排；完全相同的帧不再产生任何字节。首帧仍只从当前光标位置向下写出，擦除始终从可寻址行开始，不触碰启动前的 shell 内容；超过屏幕预算的行仍然只写一次并进入 scrollback。
- 唯一可重写块高于屏幕预算时按物理行切分：头部成为永久片段、尾部继续可重写，两个片段共享逻辑标识与连续行号，因此已写出的行不会被重复写出，也不会改变屏幕映射中的身份。

## 内容层级

- 用户消息：原文按纯文本显示（正文里的 `#`、`**`、围栏都不会生效），每一条物理行填满当前可用内容宽度的低对比度深灰背景，首行左侧短蓝色身份标识、后续行稳定缩进；块自身不附加空行。
- 模型思考：只有 `ReasoningDelta` 进入思考块；muted + dim 灰色次要文字 + 左侧细灰线；默认 `collapsed`（最多两行，省略号计入第二行宽度预算），`hidden` 不产生任何行、也不留下空白占位，`full` 展示全部；显示模式由 `MainBufferScreen` 注入，不响应 `Ctrl+O`。历史没有保存思考正文时不伪造历史思考。
- 正式回复：直接位于终端背景，不使用聊天气泡；标题/列表标记/链接使用克制的蓝青色，行内代码只用蓝灰色前景、不加背景，代码块深灰背景并填满可用宽度；完成收口后用完整累计源文本做一次稳定渲染并移除流式光标。
- 语义间距唯一事实源在渲染器 `_layout_document`：相邻且不同逻辑流的高层记录之间恰好一条无背景空白行；同一流切分、流式增量与工具原地更新不重复插入；chrome/composer/facts/status 不参与。

## Markdown 安全边界

- 使用锁文件中已有的 Rich 15.0.0（本轮声明为直接依赖，不升级任何依赖）；Mica 只做清洗、主题映射与物理行集成，不自行实现 CommonMark 解析器。
- 行样式由 Mica 从 Rich 解析出的 `Style` 重新生成，只允许加粗/暗淡/斜体/下划线与前景/背景色，**从不生成 OSC 8 超链接**；链接显示为可读文本加地址。不渲染 HTML、不加载图片、不访问 URL、不执行代码，不改变光标、标题栏、剪贴板、终端模式或 scrollback。
- NO_COLOR 下保留标题（加粗）、缩进、列表标记、引用边界（`▌`）与代码块等宽边界（`│`），结构不依赖颜色；未闭合围栏、未闭合强调与拆开的 Unicode 增量都不会抛异常。
- 流式累计源文本始终不超过 `MAX_SOURCE_CHARS`：多次追加后完整 source 被截到上限，到达上限后继续追加不改变内容（确定且幂等），清洗、Unicode、流式光标、`finish()` 与切分/冻结行为不变。
- Rich 解析器构造与渲染处于同一个普通异常边界：任一阶段抛普通异常都降级为已清洗纯文本（先按逻辑换行拆分，再按当前终端列宽硬换行，每个字符串都是一条独立物理行且不超宽度）；制表符展开为空格，不保留任何 C0/C1 控制内容，也不泄露异常文本，不新增超链接、备用屏幕或鼠标捕获序列；只捕获普通异常，`BaseException` 继续上抛。
- 流式光标计入最终物理行宽度：末行按实际可见宽度还有空余列时直接追加，已占满 `width` 时另起一条物理行；不删不截正文，光标只出现一次，`finish()` 后完全消失，正常路径与降级路径都保证含光标的完整物理行不超宽。

## 工具视图

- 一次调用仍只有一条记录，按 `ToolPrepared → ToolStarted → ToolOutputDelta → ToolFinished` 原地更新；身份继续按 `run_id`/单调 `seq`/`(run_id, call_id, tool_name)` 与最终快照完整契约校验，名称与 presentation kind 不一致时拒绝投影，不回退通用视图。只有通过完整 presentation 准入的 `ToolPrepared` 才能建立工具步骤边界；被拒绝的 `ToolPrepared` 不改变当前 reasoning/assistant 流。
- 状态用大面积低饱和色块表达并填满可用内容宽度：待执行/执行深蓝、成功深绿、失败或取消深红；不显示 `running`/`success` 等冗余状态词，NO_COLOR 改用 `○ ● ✓ × ■` 与固定安全原因；后续 Run 失败不会把已完成的成功工具重新染红。
- read 单行只显示工具名与安全相对路径；write 按纯文本显示十行并给出准确剩余/总行数；edit 使用快照中的真实旧/新行号与逐行差异、超过二十条变化时保留前十与后十条；bash 命令行三行折叠、活动期只显示最近八条逻辑行与准确更早行数（不建立卡片内滚动、不抢翻页键），终态用最终快照收口并显示 `exit N`/`(no output)`/固定原因；ask 显示问题与真实答案；generic 只显示工具名与必要原因，不伪造可展开内容。
- Bash 动态输出按逻辑行累积：空白行、连续空行、CRLF 与尾部无换行的片段都保留原语义，跨事件到达的片段拼回同一条逻辑行而不被拆成额外行，进行中行也计入逻辑行数与“更早行数”；动态缓冲只保留最近八条逻辑行（其中至多一条进行中行），`ToolFinished` 后一律以最终安全快照为唯一终态正文，不把动态缓冲拼到最终输出。
- `ToolViewFactory` 是唯一解析接缝：构造时复制外部映射、内置工具名不可被覆盖，只接收公共安全快照；解析顺序与既有 Textual seam 一致：保留名只接受对应 kind，内置 kind（含 ASK）只给名称正确的内置视图，外部 builder 只处理非保留名的 `GENERIC`。扩展视图的接受条件是对调用方快照做完整值比较（call_id、tool_name、kind、status、payload、message 任一字段不同即拒绝），因此字段等值的独立副本会被接受；构建调用、返回类型与快照比较都在普通异常边界内，出错时安全回退通用视图（只累计一个固定计数，异常文本与 traceback 不进入卡片或诊断）。

## 全局 Ctrl+O 与物理边界

- `Ctrl+O` 由屏幕路由、由渲染器持有唯一全局状态，一次展开当前会话中所有仍在可寻址范围内、具有合法安全快照的可展开终态工具，再次折叠；不影响用户消息、思考、正式回复、Ask、Permission、composer、status 或 facts，也不改变工具的业务状态、snapshot、call_id 与事件顺序；新完成的工具遵循当前全局状态，活动工具始终保持有界预览。
- 物理上限：终端 scrollback 中已经滚出屏幕的行无法原地改变高度（不清屏或不重复写出就无法重排）。可寻址范围等于渲染器的屏幕映射：最近一屏、且首行仍在屏幕上的块；屏幕外的历史工具保持写入时的形态。虚拟终端回放证实在展开/折叠过程中只使用光标上移 + `ESC[J` 擦除，没有 alternate screen、鼠标捕获、`CSI 3J` 或 shell sentinel 丢失。

## 历史恢复

- `StoredSession` 继续按 Turn 顺序显示用户消息、该轮工具调用与最终回复；用户消息与最终回复使用与实时内容相同的块与渲染器，v3 `ToolPresentationSnapshot` 走同一套专用工具视图，v1/v2 或缺少快照时退化为中性 `tool <name> · recorded`。
- 不重放 Run、不重新执行工具、不重读工作区、不重算 write/edit/bash 内容、不建立历史与实时 call_id 映射，也不补造未保存的思考；历史正文与实时正文走同一条清洗接缝。

## 验证结果

- 新增测试：`test_tui_main_buffer_markdown.py`（结构要素与 CJK 宽度、代码块满宽背景、NO_COLOR 结构、resize 重排、流式未闭合增量、冻结前缀切分、链接无 OSC 8、对抗载荷注入、跨多个 delta 的累计源上限与上限幂等、Rich 异常降级的多行/超宽/Unicode/宽度约束、`BaseException` 不被吞掉）、`test_tui_main_buffer_tool_views.py`（六类视图规则、满宽状态色块、NO_COLOR 符号与安全原因、bash 有界输出与终态覆盖、write/edit 折叠、工厂复制与安全回退、宽度上限、完整快照值比较的等值接受与逐字段回退）、`test_tui_main_buffer_hierarchy.py`（用户消息纯文本、层级顺序与留白、思考三模式与注入接缝、Ctrl+O 展开/折叠与活动 bash 有界、历史同视图与 recorded 降级、repr 不泄漏正文）。
- 既有套件更新：`test_tui_main_buffer_render.py`（read 视图替代旧工具行断言）、`test_tui_main_buffer_sanitize.py`（工具视图与 Markdown 管线加入对抗载荷）、`test_tui_main_buffer_events.py`（名称/kind 不一致拒绝、新工具遵循全局展开、活动 bash 忽略翻页键改为等待输入队列而不固定 sleep、Bash 动态空白行/CRLF/跨事件片段/八行窗口与更早行数/终态快照覆盖）、`test_tui_main_buffer_redraw.py`（变化行起点、完全相同帧零字节、Ctrl+O 受控重排）、`test_tui_main_buffer_screen.py`（用户消息带身份标识的行内断言）、`test_distribution.py`（wheel 收录 `markdown.py`/`tool_views.py`，直接依赖含 `rich>=15.0.0`）。
- 统一门槛（P6-11B-MR3 视觉层级返工后）：P6-11B/main-buffer 兼容组（`tests/test_tui_main_buffer_*.py` + 分发 + P6-09）`-W error` **410 通过**；全量 `pytest -q -W error` **1955 通过、2 跳过**；`ruff check` 全绿；`ruff format --check`（157 文件）通过；`uv build --offline` 成功；两项 `git diff --check` 干净。
- 视觉证据：110×36 彩色、80×24 彩色、80×24 NO_COLOR、完整纵向序列（用户消息 → 思考 → Markdown 回复 → read → write → edit → bash → 后续回复）、默认折叠与 Ctrl+O 展开、Bash 活动输出、恢复历史共 8 张截图，与 `runtime-main-buffer-current-p6-11a.png`、`runtime-content-hierarchy-pi-reference.png`、`runtime-tool-blocks-pi-reference.png`、`runtime-write-card-reference.png`、`runtime-edit-card-reference.png` 并排比对；截图带真实样式序列，代码块背景与工具色块均覆盖可用宽度。

## 用户真实终端验收

- 使用者已在真实 Windows Terminal 完成 P6-11B 冒烟并确认通过；仍有不影响本切片核心目标的细节问题，后续按独立问题处理，不阻塞 P6-11B 收口。

## 边界与后续

- 本切片不切换默认 `mica cli`、不删除旧 Textual、不开始 P6-11C 与 P6-10G；会话格式、Engine、Provider、权限后端与工具执行未改动。
- 屏幕外历史的高度重排受终端物理限制（见上）；若未来需要改变已滚出屏幕的旧记录高度，只能选择清屏重绘或另起一段重复内容，两者都违反当前主缓冲区原则，因此保持现状并等待产品决策。
- 思考显示模式的配置入口与持久化仍属 Phase 7；本轮只提供注入接缝与默认折叠。

# P6-11C 默认切换与旧前端退役

## 任务目标

将 `mica cli` 默认入口切到主缓冲区终端前端，删除临时 `--main-buffer-preview`，退役无调用方的旧全屏实现、专属测试和 Textual 依赖，并保留 `--plain` 基础文字降级。

## 入口与装配

- 正式 `mica cli` 与旧预览路径共用同一套会话选择、启动快照、工具、Provider 与 `MicaApp` 装配；非 `--plain` 直接构造 `MainBufferScreen`。
- `run_interactive(..., plain=True)` 的测试默认值保持不变，避免既有基础 CLI 集成测试误入终端前端。
- 默认路径不安装 SIGINT 接缝；原始模式下 Ctrl+C 仍由前端模式机处理。构造或运行失败只给出固定诊断 `mica: unable to start terminal interface`，并在未自行收敛时兜底关闭 Provider。

## 退役边界

- 删除 16 个旧全屏模块及其 20 个专属测试；`mica_agent.tui` 只导出 `MainBufferScreen`、`StartupResourceSnapshot`、`TuiSessionSnapshot` 和 `ReasoningDisplayMode`。
- 产品依赖移除 `textual>=8.2.8`，保留 `prompt-toolkit` 与 `rich`；安装态 wheel 成员与公开导入不再包含 `MicaTuiApp` / Pilot。
- 共享快照与枚举契约迁到 `tests/test_tui_models.py`，不依赖任何前端实现。

## 验证结果

- 聚焦组（CLI / 主缓冲区 / 公开导入 / 分发 / P6-09 / 共享模型）`-W error` **477 通过**。
- 全量 `pytest -q -W error` **1665 通过、2 跳过**。
- `ruff check` 全绿；`ruff format --check`（102 文件）通过；`uv lock --check` 通过；`uv build --offline` 成功。wheel 只收录 `tui/__init__.py`、`tui/models.py` 与 `tui/main_buffer/**`，依赖为 `httpx` / `jsonschema` / `prompt-toolkit` / `rich`。
- 虚拟终端截图输出到系统临时目录 `mica-p6-11c/`；后续输入可靠性返工完成后，使用者已在真实 Windows Terminal 中复验通过。

## 验收口径

自动化、静态检查、离线构建、安装态验证和后续真实 Windows Terminal 复验均已通过，P6-11C 已完成。Phase 6 仍等待 P6-10G1～G2，不因本切片完成而关闭。Engine、Provider、权限后端、会话格式与主缓冲区视觉规则未改。

# P6-11C-MR2 输入可靠性与 Ask/Permission 闭环

## 根因

- 使用者两次真实 Windows Terminal 探针确认：VT 输入模式下部分物理导航键只给出
  `uChar == "\x00"` 的记录，锁定版 `Vt100ConsoleInputReader` 只转发带字符的记录，因此
  `read_keys()` 返回空、方向键从未进入 Screen；单独 `Esc` 一直停留在未完成的转义序列里，
  因为产品链路只调用 `drain_keys()`、从不调用 `flush_keys()`。
- `AskInteraction` 用 `selected_option_index = len(options)` 提交自定义答案，被 Broker 的
  越界校验拒绝，而 UI 对拒绝没有任何反馈；自定义输入还会吞掉 `↑`/`↓` 且空答案静默失败。
- 渲染器的 chrome 只从顶部保留尾部行：面板高于可视高度时会整行丢掉当前选中行（80×12 +
  17 行面板可复现）。
- 已排除：入口/解释器漂移、Textual、旧安装包、selected 状态机、差分未写出、后续帧覆盖、
  终端失焦、数字键与方向键路由不同。

## 最小实现

- `terminal.py`：`Terminal.flush_keys()` 与 `drain_keys()` 对称；`default_input_factory()`
  就地安装 Windows 导航后备——`windows_record_sequences()` 把零字符的受支持导航记录换成
  等效 VT 序列，`WindowsVtKeyReader` 用锁定版 `Vt100Parser` 解析，混合读取器只接管
  「记录 → 输入串」并复用 prompt_toolkit 的平台 IO；key-up、非按键事件、不支持的 VK 与
  同批已由字符记录覆盖的 VK 都不产出，因此不会重复发键。
- `screen.py`：空 `drain` 时用一次性 `loop.call_later`（`ESCAPE_FLUSH_DELAY = 0.1`，可注入）
  安排 flush，非空 `drain` 取消句柄，flush 结果与 drain 共用同一个归一化入队路径，关闭时
  先取消句柄；面板按 `渲染器高度 - 1` 拿到可用行数与选中行锚点；响应被拒绝时保留面板与
  交互状态并显示固定提示。
- `interactions.py`：自定义答案改用 `None` 下标提交；自定义输入中 `↑`/`↓` 返回固定选项并
  保留草稿；空答案与拒绝只设置固定短提示、下一次有效输入清除；新增按可用行数降级的面板
  排版（空行 → 说明/详情 → 标题 → footer → 分隔线 → 带「上方/下方还有 N 行」的确定性窗口）。
- `renderer.py`：`set_chrome()` 接受选中行锚点，裁剪时保证锚点可见（未提供锚点时行为不变）。

## 测试边界

- 建模 Windows 记录（含真实 `INPUT_RECORD` 结构）→ 混合读取器 → `Keys.Up/Down`，并断言旧
  读取器对同一批记录返回空；标准 `ESC[A`/`ESC[B` 与后备不合成两次；key-up 不产键。
- 建模控制台输入驱动真实 Screen/Ask/Permission/渲染器，并用仓库自带 `VirtualTerminal`
  回放物理帧确认标记行移动；单独 `Esc` 只在 flush 后取消真实 Run，完整序列不被拆开，关闭
  取消待触发句柄。
- `Type something.` 全链路打到真实 `MainBufferRuntime`/`MicaApp`/`InteractionBroker`/ask
  handler，断言 `selected_option_index is None`、pending 清零、Run 继续；并保留旧越界下标被
  拒绝的回归断言。
- 80×12（及 resize 到 80×8、60×10）下 Ask/Permission 的当前选中行必须出现在物理帧里，
  且不发送 alternate screen、不清 scrollback、不捕获鼠标、`PageUp`/`PageDown` 不滚动。

## 验收口径

自动化、Ruff、lock、离线构建与 diff 检查通过后，本返工仍只报告等待使用者真实 Windows
Terminal 复验（Ask/Permission 方向键、自定义回答、空答案提示、拒绝重试、Esc 取消、超高
面板、失焦恢复、退出后终端恢复），不得自行宣布 P6-11C 或 Phase 6 完成。Engine、Provider、
权限后端、Broker 校验、事件 Schema 与会话格式未改。

# P6-11C-MR2 第二轮极小返工：锚点硬约束与提示清除语义

## 已确认的两个缺陷

- `_window_rows()` 把「命中多少必留行」当作首要评分，锚点 `inside` 只是同级的第二项。
  拒绝提示换成多行后，`PermissionInteraction(width=20, limit=3, selected=1)` + `fail()`
  会选中「不含选中行、但含两条提示行」的窗口：`› 2. No` 从可见行里消失，`selected_row`
  甚至算成 `-1` 或指向计数行。
- `AskInteraction.handle()` / `PermissionInteraction.handle()` 在入口无条件 `self.hint = ""`，
  于是 `PageUp`/`PageDown`/未知功能键/越界数字这类既不消费也不改变状态的按键也会删掉
  拒绝提示。

## 最小实现

- `_window_rows()`：候选窗口先被限制在 `start <= focus < end`，锚点由评分项改成**硬约束**；
  窗口之间再按「覆盖的提示行数 → 是否交代了被隐藏的行（两条计数行放不下时宁可不显示）→
  内容行数 → 更靠上」排序。空间不足时先丢上下计数行，再丢其他内容行，绝不为低优先级内容
  丢掉选中行；上下计数行只在两条都放得下时才显示，避免半个误导性说明。
- `fit_panel_rows()`：`rows` 为空时返回空结果，否则保证 `0 <= selected < len(visible)` 且
  `visible[selected]` 就是原 `anchor` 行。
- 提示清除改为「按消费点清除」：`↑`/`↓` 导航、有效数字定位、`n` 进入备注、备注 `Enter`
  返回、进入自定义、文本编辑（`text`/`paste` 且清洗后非空）、`Backspace`/`Delete`、`Enter`
  重试，以及自定义中 `↑`/`↓` 退出编辑；其余按键保持提示不变。`Esc` 关闭交互，不保留提示。

## 取舍

- 极端预算下的显示优先级按任务书为「选中行 → 与选择相关的核心信息 → 拒绝/空答案提示 →
  上下隐藏计数 → 其他装饰」；本实现把「提示行」排在「隐藏计数」之前，把「内容行数」放在
  两者之后，因此极窄极矮时可能先丢问题主句或计数行，但绝不会丢选中行。若 master 要求把
  核心信息提到提示之前，只需调整 `_window_rows()` 里那一个评分元组。

## 测试边界

- 直接对 `fit_panel_rows()` 断言「锚点行仍在结果中、`selected_row` 合法且精确指向锚点」：
  多行提示、提示离锚点很远、`limit = 1/2/3`。
- Permission/Ask 公共面板在 `fail()` 后（提示在 20 列下确实换行）于 `width = 20/24/30`、
  `limit = 1..5` 下断言 `› 2. No` / 固定选项首项、末项、`Type something.` 始终可见。
- 30×5 极窄极矮终端 + 业务拒绝的物理帧：`_build_chrome()` 的锚点合法且指向当前选择，
  回放帧里能看到 `› 2. No`、`› 1. Yes` 与拒绝提示；`PageUp`/`PageDown`/未知功能键不清提示、
  不移动选择；不出现 alternate screen、scrollback 清除或鼠标捕获序列。
- 把返工前的 `_window_rows()` 与入口无条件清除语义装回进程（临时目录里的 pytest 插件，
  不改仓库）后，上述新增断言全部失败：Permission 出现 `selected_row = -1`、计数行顶替
  `› 1. Yes`、提示被 `PageUp` 清空等。

## 验收口径

只报告自动化结果，等待 master 复审与使用者真实 Windows Terminal 复验；不宣布 P6-11C 或
Phase 6 完成，不启动 P6-10G，不做 Git 收口。

## 最终验收

- master 已复审锚点硬约束、提示清除语义及对应回归测试，指定聚焦组 `-W error` 143 通过；
  Ruff、格式与两项 diff 检查通过。
- 使用者已在真实 Windows Terminal 中完成 Ask 固定选项、自定义回答、Permission、Esc、
  失焦恢复与窄窗口等冒烟测试，确认功能运行正常。
- P6-11C 正式验收通过并完成 Git 收口；P6-10G1～G2 仍未开始，Phase 6 仍保持构建中。
