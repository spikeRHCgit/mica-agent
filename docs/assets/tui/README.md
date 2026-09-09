# TUI 视觉参考资源

本目录保存已经由用户确认、并需要在 Textual TUI 施工和视觉验收中长期使用的截图。它们是
设计与验收输入，不是运行时资源；产品代码不得读取或在终端中直接显示这些 PNG。

## 文件说明

- `startup-reference-wide.png`：正向目标。用于确认新会话启动首屏的整体构图、纵向顺序、
  资源分区、输入框边界和底部信息层级。
- `startup-current-bottom-aligned.png`：负向问题。记录 P6-09 冒烟时启动内容被推到下半屏、
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

## 使用规则

- Worker 开始启动首屏相关任务前必须实际打开上述全部相关图片，不得只读文字转述。
- 正向目标图中的布局和视觉层级需要复现；负向问题图只用于确认必须消除的偏差。
- 删除目标图中红框标注的元素不得以改文案、隐藏透明度或换位置的方式保留；应从对应视图
  结构中移除。Pi 截图只约束底部信息布局，不是整体视觉目标。
- 目标图包含旧版示例数据。`ls`、`Permissions: ask`、Context token 数、`/help` 等不得被
  当作当前产品事实照抄；实际文字必须来自当前已存在的公共契约和真实能力。
- 运行中参考图只决定对应组件的构图和信息层级；工具参数、权限选项、状态文字与安全边界
  仍以 `docs/tui_design.md` 和当前公共事件契约为准。
- 时间线问题图与目标图必须成对检查；不得只确认组件数量，而要确认真实 DOM 顺序与 Engine
  事件顺序一致。键盘与失焦问题必须在真实按键链路及 `AppBlur → AppFocus` 恢复后验证。
- 自动化测试、构建成功和 DOM 断言不能代替视觉验收。应在相同或可比的终端尺寸与初始状态
  下生成实现截图，与目标图并排检查后再交由用户手工确认。
