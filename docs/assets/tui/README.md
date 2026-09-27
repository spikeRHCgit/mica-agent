# TUI 视觉参考资源

本目录保存已经由用户确认、并需要在终端前端施工和视觉验收中长期使用的截图。它们是
设计与验收输入，不是运行时资源；产品代码不得读取或在终端中直接显示这些 PNG。

本文的「使用规则」是终端视觉验收流程的唯一事实源；角色实践手册和任务卡只引用这里，不另写一套。

## 文件说明

- `startup-reference-wide.png`：正向目标。用于确认新会话启动首屏的整体构图、纵向顺序、
  资源分区、输入框边界和底部信息层级。
- `startup-current-bottom-aligned.png`：负向问题。记录 P5-09 冒烟时启动内容被推到下半屏、
  大面积深蓝背景、资源区过于简化和底部信息缺失的问题。
- `composer-current-incomplete-border.png`：负向问题局部。记录输入框聚焦后边界不稳定、缺少
  提示文字和快捷键信息的问题。
- `startup-remove-logo-wordmark.png`：删除目标。只删除 full/compact 图形 Logo 右侧的小写
  `mica`，极窄终端的纯文本 `Mica` 降级仍保留。
- `startup-remove-simulated-greeting.png`：删除目标。移除看似模型已经回复的静态欢迎行，保留
  真正的应用就绪说明。
- `startup-remove-unimplemented-key-hints.png`：删除目标。移除尚未全部实现的底部快捷键栏，
  后续只在真实绑定完成后重新设计。
- `startup-facts-current-layout.png`：负向问题局部。记录 `W:`、`P:def`、耗时和工具数造成的
  信息噪声。
- `startup-facts-pi-layout-reference.png`：正向布局参考。只借鉴“工作区居左、模型居右、会话另行”
  的低干扰信息层级，不照抄 Git、token、成本或进程数据。
- `startup-context-paths-current-problem.png`：负向问题局部。记录已加载 AGENTS 只显示 `yes`
  而无法定位来源的问题。
- `runtime-tool-flow-reference.png`：运行中工具流的整体参考。用于确认工具调用嵌入对话区、
  长内容折叠和命令输出层级，不复刻截图所属产品的背景或外围控件。
- `runtime-write-card-reference.png`：`write` 卡片正向参考。用于确认工具名、路径、纯文本正文
  和折叠摘要的结构。
- `runtime-edit-card-reference.png`：`edit` 卡片正向参考。用于确认上下文、删除、新增内容及
  新旧行号的同卡片差异布局。
- `runtime-ask-panel-reference.png`：结构化 `ask` 面板正向参考。用于确认标题、问题、选项、
  推荐标记、说明文字和底部操作提示的层级。
- `runtime-permission-panel-layout-reference.png`：权限面板的布局参考，只借鉴命令预览、问题、
  纵向选项和底部提示的空间关系；当前产品契约仍固定为 `Yes`/`No` 与 `Esc` 取消，不照抄
  截图中的永久授权或修改命令选项。
- `runtime-timeline-order-current.png`：负向问题。记录同一 Run 的多次模型文字被追加到首个
  回复组件、后续工具卡片堆积在下方，导致视觉顺序偏离真实 Engine 事件顺序。
- `runtime-timeline-order-reference.png`：正向目标。只借鉴“模型步骤、工具步骤、下一模型步骤”
  沿单一时间线交错出现的顺序，不复刻背景图片、字体或外围产品信息。
- `runtime-ask-keyboard-instability-current.png`：负向问题。Ask 正文已经可见，但真实 Windows
  终端中上下键选择会因焦点所有权不稳定而失效；用于验证修复后的交互而非复刻布局缺陷。
- `runtime-interaction-overlay-reference.png`：正向目标。Ask 和权限等待时使用底部阻塞式交互层，
  保留上方 transcript 可见并覆盖普通底部控制区；不复刻截图中的背景或额外选项。
- `runtime-main-buffer-pi-reference.png`：主缓冲区整体正向参考。只借鉴终端原生背景、主缓冲区
  scrollback、输出位于底部输入区上方和退出后历史保留；不照抄截图中的模型、token、成本、
  MCP、包更新或其他产品文案。
- `runtime-tool-blocks-pi-reference.png`：动态工具色块正向参考。用于确认工具调用以大面积状态
  背景区别于普通模型正文、运行中安全摘要动态更新、历史工具可统一展开；截图中的代码、命令、
  路径、桌面内容和其他应用界面都不是 Mica 的实现输入。
- `runtime-main-buffer-current-p5-11a.png`：P5-11A 用户真实终端验收后的负向视觉基线。主缓冲区、
  原生 scrollback、输入和交互功能已经通过，但用户消息、模型思考、正式回复与工具记录过于拥挤，
  正式回复仍暴露原始 Markdown 标记；只用于确认不得回退的偏差，不得复刻其视觉层级。
- `runtime-content-hierarchy-pi-reference.png`：正文层级正向参考。只借鉴段落留白、标题/列表/行内代码
  的可读层级、次要过程信息与正式回复的区分，以及工具色块和普通正文的清晰边界；不照抄 Todo、
  token、成本、模型名、产品文案、桌面背景或其他应用控件。
- `runtime-p5-11b-hierarchy-still-crowded.png`：负向问题。P5-11B 真实 Windows Terminal 复验：用户消息
  背景随文字收缩、用户/思考/回复/工具之间零间距、思考不够次要。只用于确认必须消除的偏差，
  不是待复刻目标。
- `runtime-p5-11b-inline-code-overhighlighted.png`：负向问题。P5-11B 真实 Windows Terminal 复验：普通
  段落里的路径、工具名、命令和环境变量被行内代码灰块密集高亮。只用于确认必须消除的偏差，
  不是待复刻目标。

## 使用规则

- 执行界面任务的 agent 开始前必须实际打开相关图片，不得只读文字转述。任务至少打开
  `runtime-main-buffer-pi-reference.png`、`runtime-tool-blocks-pi-reference.png` 及当前任务涉及的
  Ask、权限、工具或启动参考图；任务卡应写明需要打开的图片。
- 有对应正向参考的负向问题图必须成对打开，例如 `runtime-main-buffer-current-p5-11a.png` 与
  `runtime-content-hierarchy-pi-reference.png`：前者只证明需要消除的拥挤和原始 Markdown，后者
  只提供内容层级与阅读节奏目标。
- 正向目标图中的布局和视觉层级需要复现；负向问题图只用于确认必须消除的偏差。
- 删除目标图中红框标注的元素不得以改文案、隐藏透明度或换位置的方式保留；应从对应视图
  结构中移除。各 Pi 截图只约束其条目声明的主缓冲区、底部事实或工具色块特征，不要求照抄
  Pi 的产品文案、功能集合和完整视觉主题。
- 目标图包含旧版示例数据。`ls`、`Permissions: ask`、Context token 数、`/help` 等不得被
  当作当前产品事实照抄；实际文字必须来自当前已存在的公共契约和真实能力。
- 运行中参考图只决定对应组件的构图和信息层级；工具参数、权限选项、状态文字与安全边界
  仍以 `docs/tui_design.md` 和当前公共事件契约为准。
- 时间线问题图与目标图必须成对检查；不得只确认组件数量，而要确认渲染文档顺序与 Engine
  事件顺序一致。键盘可靠性必须通过真实输入字节链路验证，不能用框架合成的焦点事件替代。
- 自动化测试、构建成功和组件顺序断言不能代替视觉验收。应在相同或可比的终端尺寸与初始状态
  下生成实现截图，与目标图并排检查，在报告中提供对照证据，再交由用户在真实 Windows Terminal
  手工确认；截图对照通过也不能表述为真实终端已经验收。
