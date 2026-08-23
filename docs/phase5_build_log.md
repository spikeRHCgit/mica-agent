# P5-01 公共工具、交互与取消契约

## 目标

建立后续工具系统共享的供应商无关公共对象、事件和取消信号，同时保持现有纯文本消息与会话 v1 行为兼容。

## 公共契约

- 工具：`ToolSpec`、`ToolCall`、`ToolRequest`、`ToolResult`、`ToolResultStatus`。
- 权限与交互：`PermissionMode`、`PolicyDecision`、`PermissionAnswer`、`PermissionResponse`、`UserInputResponse`。
- 取消：`CancellationToken`、`CancellationRequested`。
- 事件：`ToolStarted`、`ToolFinished`、`PermissionRequested`、`UserInputRequested`，以及 `ErrorCategory.LIMIT`。
- 消息：保留 `Message(role, content)`，新增 assistant `tool_calls` 与 tool `tool_call_id`。

## 关键决定

JSON object 输入在构造时递归复制为只读 mapping，并把数组复制为 tuple，形成不受原始 dict/list 后续修改影响的深层不可变快照。校验只报告字段规则，不回显原始输入。

`CancellationToken` 使用一次性异步事件；重复取消幂等，先取消后等待立即完成，所有活动等待者同时唤醒，取消异常使用固定安全文案。

## 验证与边界

目标测试、旧 session/provider/engine/app 兼容性测试及全量质量检查均通过。P5-01 未实现注册表、Schema 参数验证、权限算法、真实工具、Provider tool calling、Engine 工具循环、会话 v2 或 CLI 集成，这些仍属于 P5-02 及后续任务。

剩余风险：公共事件只约束可携带字段，后续事件生产者仍必须在进入事件前生成固定或已清洗的安全说明。
