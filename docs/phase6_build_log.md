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

对同一输入状态连续多次刷新后高度保持稳定，不会 1/2 行振荡；终端 resize 时更新布局而不重建组件、不清空文本、不改变焦点或移动父节点。P6-02A2b 与 P6-02B 尚未开始。
