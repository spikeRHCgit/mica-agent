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
