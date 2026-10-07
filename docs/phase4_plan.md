# Mica 第四阶段：工具执行、权限控制与统一取消

> 状态：已完成
> 上位约束：`docs/PROJECT_PLAN.md`
> 前置阶段：`docs/phase3_plan.md`

## 1. 目标

把基础对话扩展为可在明确权限下操作工作区的 Agent 闭环：模型提出工具调用，Mica 统一校验、授权、执行和回传结果，再继续请求模型直至得到最终回答或明确终态。工具执行、用户等待和取消必须经过同一套公共边界，而不能由模型或前端绕过。

## 2. 已交付

- 建立供应商无关的 `ToolSpec`、调用、请求、结果和注册契约；未知工具、非法参数、拒绝与执行失败均有结构化结果。
- 最终提供 `read`、`write`、`edit`、`bash`、`ask` 五个内置工具，并对工作区路径、文件大小、原子写入、命令输出、超时和进程树清理设置边界。
- 接通权限决策和一次性用户确认，支持运行中 `ask` 回答；现行权限模式为 `default` 与 `solo`，两模式整理发生在第五阶段。
- OpenAI-compatible Provider 能发送工具定义、组装流式工具调用；Engine 能在同一 Run 中顺序执行多个工具，并将真实结果送回模型继续生成。
- Provider、Engine、交互等待、工具和子进程共享取消语义；一个 Run 只产生一个终态，模型步骤与工具调用具有上限。
- 增加内置系统提示词、独立思考增量、Bash 动态输出和安全工具展示快照；成功工具轮次可保存并恢复，不重新执行历史工具。

## 3. 实现概况

### 3.1 工具契约、注册与授权

`tool_contracts.py` 将工具定义、模型调用、实际请求和执行结果分为不可变对象，对 JSON 参数与结果制作深层快照；`ToolRegistry` 保存 `ToolSpec` 与异步 handler，拒绝重复名称，并把确定性的定义列表提供给 Provider。`schema_validation.py` 以注册的 JSON Schema 校验真实参数。`ToolExecutor.execute()` 固定执行顺序：查找注册项、校验参数、形成权限事实、决定允许/询问/拒绝、调用 handler，再把异常转为安全的 `ToolResult`；Engine 不按工具名分支执行。

`PolicyEngine` 消费规范化 `PolicyInput`，权限等待经 `InteractionBroker` 发出一次请求并接收一次响应。`command_risk.analyze_command()` 只做词法风险分类，不执行命令或把工作区目录当作操作系统沙箱。当前 `default` 对受保护资源和高风险命令询问，`solo` 跳过策略审批；两模式都保留工具注册、Schema、文件能力、输出上限和取消边界。

### 3.2 内置工具与取消

`workspace_paths.py` 集中解析工作区相对路径并拒绝越界、链接和不支持的路径形式。`file_tools.py` 的 `read` 限制 UTF-8 与大小；`write`、`edit` 用同目录临时文件和原子替换，`edit` 只接受唯一旧文本匹配。`runtime_tools.py` 显式寻找 Bash 可执行文件，以工作区为 cwd 启动受限子进程；输出按字节上限收集并通过 reporter 向前端发送有序增量，超时或取消时回收进程树。`ask` 通过 Broker 等待用户输入，而不在工具 handler 内直接读取终端。

`CancellationToken` 贯穿 Provider 流、Engine、Executor、Broker 等待和 Bash 清理；正常工具失败或权限拒绝作为工具结果反馈模型，取消则终止当前 Run。动态输出事件不进入 Provider 消息或会话记录，最终安全结果才成为模型和历史可用的数据。

### 3.3 模型—工具循环与持久化

`OpenAICompatibleProvider` 把 `ToolSpec` 转为请求的 `tools` 字段，在 SSE 结束前按调用索引组装碎片化的 ID、名称和 JSON 参数；缺失、冲突或畸形片段不会交给执行器。`AgentEngine.run()` 在一个 `run_id` 下逐步请求 Provider，按返回顺序生成 `ToolPrepared`、`ToolStarted`、`ToolOutputDelta`、`ToolFinished` 等事件，逐个执行同批工具，把助手调用消息和对应 `TOOL` 结果追加到本 Run 上下文，再发起下一次模型请求。模型步骤和工具调用上限阻止无界循环。

`MicaApp` 在完整 `RunCompleted` 后提交包含用户指令、工具过程和最终回答的 `TurnEntry`。第四阶段将早期纯文本会话扩展到能保存工具消息及最终 `ToolPresentationSnapshot` 的版本，旧会话可以读取并在业务写入时安全升级；当前 writer 已由第五阶段升级为 v4。工具参数和输出进入持久化或展示前经 `message_sanitizer.py` 与 `tool_presentation.py` 清洗，实际 handler 仍使用原始参数，避免把脱敏副本错误地用于执行。

### 3.4 思考、提示词与基础前端

`system_prompt.py` 从随包分发的模板构造启动时的系统提示词；`ContextBuilder` 提供本次成功加载的 AGENTS 来源，`MicaApp` 将头部上下文与后续会话消息分开。`ReasoningEffort` 经 Provider 映射为协议参数，模型思考增量由 Engine 单独发出 `ReasoningDelta`，不混入正式助手消息或成功轮次。基础文字 CLI 消费公共工具、权限和输入事件：状态与 Bash 输出写向 stderr，正式回复写向 stdout；前端通过 `MicaApp.respond_interaction()` 回答等待，不直接碰 Broker 私有状态。

## 4. 验收结论

第四阶段已通过五工具、Schema 与权限、文件和子进程安全边界、SSE 工具调用、多步 Run、取消、旧会话兼容升级与安装态闭环验证；Windows Bash 和真实 Provider 的受控场景完成验收，静态检查及离线构建通过。详细施工证据见 `git show 3829052:docs/log/phase4_build_log.md`；现行调用与源码位置以 `README.md`、`docs/module_map.md` 为准。
