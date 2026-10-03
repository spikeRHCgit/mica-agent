# Mica 第五阶段：终端交互前端与中断恢复

> 状态：已完成
> 上位约束：`docs/PROJECT_PLAN.md`
> 前置阶段：`docs/phase4_plan.md`
> 视觉与交互细则：`docs/tui_design.md`

## 1. 目标

把基础文字 CLI 升级为可持续使用、在真实 Windows Terminal 中稳定工作的终端交互前端，同时让用户取消或可恢复 Provider 失败后的真实运行进度可以保存和继续。最终前端使用终端主缓冲区及原生 scrollback；界面只消费 MicaApp、SessionService 和 Engine 的公共契约。

## 2. 已交付

- `mica cli` 默认进入主缓冲区前端，不进入 alternate screen、不捕获鼠标滚轮；输出留在终端 scrollback，`--plain` 保留基础文字界面。旧 Textual 全屏前端、专属测试和依赖已退役。
- 提供启动资源区、持久化历史、按真实事件顺序交错的用户/思考/正式回复/工具记录，以及动态工作状态、输入框下的工作区/分支/模型/会话事实。
- 输入区支持多行与软换行、普通位置光标移动、插入和删除、长粘贴片段折叠；模型正式回复安全渲染 Markdown，思考内容独立显示。
- 五种内置工具及未知工具有对应的安全展示；执行中有界更新、终态原位收口，颜色块与无颜色符号区分状态。`Ctrl+O` 统一展开或折叠当前会话的工具详情，不改变思考内容。
- `ask` 与权限确认在等待时独占底部交互区，支持方向键、数字键、自定义答案或一次授权/拒绝；交互结束后恢复原有草稿与底部事实。
- `Esc` 可取消模型输出、工具和交互等待；`Ctrl+C` 使用短时二次确认退出。Windows 输入路径包含方向键后备、单独 `Esc` flush、终端失焦后的显式输入模式，以及 composer 光标即时定位。
- 完整成功轮次仍保存为 `TurnEntry`；取消或可恢复失败另存 `RunCheckpoint`，恢复时保留原始用户指令和真实已完成进度，不伪造未执行工具或最终模型回答。

## 3. 实现概况

### 3.1 主缓冲区输入与差分输出

`tui/main_buffer/terminal.py` 统一拥有原始按键、尺寸、终端能力与实际输出字节；Windows VT 输入遇到零字符导航记录时使用等效序列后备，`flush_keys()` 使单独 `Esc` 成为可处理按键，同时避免把完整方向键序列拆开。`MainBufferScreen` 用 `IDLE/RUNNING/ASK/PERMISSION` 等显式模式分配按键，不依赖终端焦点事件决定交互归属。

`MainBufferRenderer` 保存安全语义块及已写出的物理行身份，只对变化尾部产生差分帧；宽度变化、历史工具展开或较早块高度变化时受控重排。普通正文不发送 alternate-screen、整屏擦除或鼠标捕获序列；超出屏幕预算的旧输出交给终端 scrollback。只有编辑光标变动而文本不变时发送 cursor-only 定位帧，不重写正文。`sanitize.py` 在不可信文本进入排版前去除控制序列，终端层再检查实际发出的字节。

### 3.2 对话时间线与内容呈现

`MainBufferScreen.handle_event()` 按 `run_id`、递增 `seq` 和工具身份准入公共事件，将每个模型步骤的 `ReasoningDelta`、`TextDelta` 与随后的工具记录依次投影到同一语义文档；接受的 `ToolPrepared` 才结束当前模型步骤，后续回复因此出现在该批工具下方。`components.py` 为用户条、次要思考、状态和底部事实提供独立层级；`markdown.py` 用受控 Rich 渲染正式回复及流式预览，解析失败退回已清洗纯文本。相邻高层记录的间距由渲染器统一决定。

`tool_views.py` 按合法 `ToolPresentationSnapshot` 生成 read/write/edit/bash/ask 或通用视图，同一工具调用在活动期更新、终态原位完成；活动 Bash 只显示有界输出，历史中的最终安全快照才参与全局展开。恢复旧会话时只投影已有消息和展示快照，不重新执行工具或读取当前工作区补造历史。工作状态在运行时动态显示 working；完成后短时显示耗时，主动中断和其他失败分别保留 stop、error 到下一轮开始。

### 3.3 编辑、阻塞交互与生命周期

`editor.py` 基于 prompt_toolkit 的 `Buffer`/`Document` 保存真实草稿与光标位置，软换行仅改变显示；折叠的大段粘贴在编辑时作为不可拆分片段，提交时还原原文。`interactions.py` 维护 ask 与权限选项、自定义回答、备注、拒绝后可重试提示，并把用户动作转换为公共 `UserInputResponse` 或 `PermissionResponse`。交互面板替代 working、composer 与 facts，但不丢失草稿；超高面板按选中行锚点裁排，保证当前选项仍可见。

本阶段也把权限后端收敛为 `default` 与 `solo`：前者对受保护文件和命令风险请求一次确认，普通合法文件操作与低风险命令直接继续；后者跳过策略审批，但不跳过注册、Schema 校验、文件工具能力限制、超时和取消。`command_risk.py` 只分析命令文本，不执行或静态证明任意 Shell 命令安全；前端仅响应后端产生的 `PermissionRequested`，不自行决定风险。

`MainBufferRuntime` 拥有唯一活动 Run、事件消费任务、取消令牌和资源关闭顺序；`MainBufferScreen` 只投影被接受的事件并管理显示状态。取消、EOF 或关闭时依次收敛交互等待、Engine 事件流、工具进程与 Provider，迟到事件不能改写已结束的 Run。`cli.py` 默认装配此终端前端，`--plain` 走保留的基础文字循环。

### 3.4 中断检查点与恢复

`checkpoint_contracts.py` 定义不同于成功轮次的 `RunCheckpoint`：按模型步骤保存部分助手文本、工具调用及 success/failed/in_progress/not_started 状态，禁止为未完成工具伪造结果。`AgentEngine` 在取消或可恢复失败前向内部 `progress_sink` 发出真实进度；`MicaApp` 在公开终态前经 `SessionService` 提交一次检查点，完整成功仍只提交 `TurnEntry`。工具执行使用原始参数，持久化副本经集中清洗，不把绝对路径或凭据误带入恢复上下文。

`session_contracts.py` 与 `jsonl_store.py` 将会话格式扩展为 v4 的有序 `TurnEntry/RunCheckpoint` 时间线，旧 v1/v2/v3 在首次业务写入时锁内原子升级；`SessionService.context_snapshot()` 按该时间线只投影已完成工具的真实结果，对部分输出和未完成状态使用 Mica 自有的明确提示。前端恢复历史时显示稳定的 incomplete 标记，不把中断记录伪装成完整回答。

## 4. 验收结论

第五阶段的主缓冲区入口、输入编辑、时间线、Markdown、工具与交互展示、取消清理和中断恢复均通过自动化、分发/离线构建及用户真实 Windows Terminal 冒烟验收。旧全屏前端已移除，`mica cli` 与 `--plain` 的入口边界已确认。视觉细则以 `docs/tui_design.md` 为准；施工和返工证据保留在 `docs/log/phase5_build_log.md`。
