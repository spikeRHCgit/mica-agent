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

# P5-02 工具注册、参数校验与权限策略

## 目标

建立内存 `ToolRegistry`、标准 JSON Schema 参数校验和只接收规范化事实的 `PolicyEngine`，同时补齐 P5 不可变 JSON 快照到普通 JSON 值的集中转换入口。本任务不执行任何真实工具。

## JSON Schema 依赖审计

- `jsonschema` 是成熟的标准实现，支持 Draft 3、4、6、7、2019-09 和 2020-12，提供 Meta-Schema 自检；当前版本支持 Python 3.11，MIT 许可证，Windows CPython 有现成 wheel。它有 `attrs`、`jsonschema-specifications`、`referencing` 和 `rpds-py` 四个合理的运行时依赖；常规 Windows 安装不需要编译环境，但缺少 wheel 的源码安装环境需要为 `rpds-py` 准备 Rust 工具链。
- `fastjsonschema` 更轻，BSD-3-Clause、纯 Python 且无运行时依赖，但只支持 Draft 04、06、07，采用代码生成，默认值行为还可能修改待验证数据，不适合作为 Mica 的长期标准边界。
- 手写 P5 子集不增加依赖，但会重复实现类型、组合和 Meta-Schema 细节，容易形成不完整却被误认为完整标准的校验器，维护和安全风险最高。

最终只增加直接依赖 `jsonschema>=4.25.1`，锁文件解析为 4.26.0，并固定使用 Draft 2020-12；没有 `$schema` 的 ToolSpec 也按该 draft 处理。验证器使用禁止远程获取的空引用注册表，默认测试和参数校验不会访问网络。第三方 Schema/参数异常统一转换为 Mica 固定错误，不向调用方暴露原始异常或数据。

## 公共行为与边界

`ToolRegistry` 在写入前验证 `ToolSpec`、handler callable 和 Schema；重复名称不会覆盖第一次注册。`specs()` 返回仅含 `ToolSpec`、按名称排序的 tuple 快照；`resolve()` 返回未来 `ToolExecutor` 使用的不可变注册项，未知名称使用固定错误且不回显名称。注册和查询都不执行 handler。

`validate_tool_schema()` 区分开发者提供的无效 Schema；`validate_tool_arguments()` 区分可恢复的模型参数错误。required、类型、嵌套 object、array、enum、`additionalProperties` 和 bool/integer 边界由 Draft 2020-12 校验，且验证只消费独立普通 JSON 副本，不修改请求快照。

`to_mutable_json()` 是 P5 不可变 JSON 快照的唯一普通 JSON 转换入口：mapping 复制为 dict、tuple 复制为 list、标量保持 JSON 类型，结果可直接交给标准编码器，且修改结果不会影响原快照。

`PolicyEngine` 的顺序固定为 hard deny、当前调用的一次性授权、注入 allow rule、权限模式矩阵。hard deny 不能被任何后续分支覆盖；规则异常和非 bool 真值均不会放行。DEFAULT 只自动允许 read/list/ask；ACCEPT_EDITS 另允许 write/edit；SOLO 再允许明确标记的安全只读命令，但普通 command 和 other 在所有相应默认分支仍为 ASK。一次性授权不保存、不缓存，也不影响下一次同名调用。

## 验证与未实现范围

公共目标测试和 P5-01/旧核心兼容测试覆盖注册快照、Schema 安全错误、完整权限矩阵、优先级、公共导入与懒加载；全量自动化测试、Ruff、格式检查和离线构建均通过。P5-02 未实现 ToolExecutor、真实文件或命令工具、路径/命令安全分析、Provider tool calling、Engine 工具循环、交互等待、会话 v2、配置读取或 CLI/TUI；这些仍属于 P5-03 及后续任务。

新增运行依赖后，旧分发测试中“只有 httpx”以及依赖机器 uv registry cache 的假设不再成立。经用户明确授权，分发测试改为分别验证 wheel 依赖元数据、无依赖基础 CLI 懒加载，以及 Mica wheel 在当前 uv 锁定依赖环境中的离线运行；自动化测试本身仍不联网。

剩余风险：完整 JSON Schema 支持会带来传递依赖体积；`rpds-py` 在没有对应 wheel 的平台需要编译环境。当前分发测试不再独立证明 uv 能从空 cache 离线安装全部第三方 wheel，该能力依赖发布环境准备 wheelhouse 或预热 cache。未来 Schema 若引入远程引用，当前安全边界会固定拒绝解析，而不会隐式联网。
