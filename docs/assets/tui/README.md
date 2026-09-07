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

## 使用规则

- Worker 开始启动首屏相关任务前必须实际打开上述全部相关图片，不得只读文字转述。
- 正向目标图中的布局和视觉层级需要复现；负向问题图只用于确认必须消除的偏差。
- 删除目标图中红框标注的元素不得以改文案、隐藏透明度或换位置的方式保留；应从对应视图
  结构中移除。Pi 截图只约束底部信息布局，不是整体视觉目标。
- 目标图包含旧版示例数据。`ls`、`Permissions: ask`、Context token 数、`/help` 等不得被
  当作当前产品事实照抄；实际文字必须来自当前已存在的公共契约和真实能力。
- 自动化测试、构建成功和 DOM 断言不能代替视觉验收。应在相同或可比的终端尺寸与初始状态
  下生成实现截图，与目标图并排检查后再交由用户手工确认。
