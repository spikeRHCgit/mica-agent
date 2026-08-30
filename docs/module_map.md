# Mica 源码技术地图

## 使用方式

本文用于按文件快速定位 Mica 当前源码。每个词条只说明一个实际存在的源码或包资源文件；
阶段目标和进度以 `PROJECT_PLAN.md`、对应 `docs/phaseN_plan.md` 为准，具体行为仍以代码和测试
为准。新增、删除、重命名源码文件时，同一任务必须同步更新本文。

`__pycache__/`、wheel、sdist、测试缓存和虚拟环境属于生成物，不进入技术地图。

## 文件词条

| 文件 | 具体作用 |
|---|---|
| `src/mica_agent/__init__.py` | Mica 顶层公共 API 出口；集中导出稳定契约，并通过懒加载避免普通导入触发 CLI、Provider、文件系统或运行时装配。 |
| `src/mica_agent/__main__.py` | `python -m mica_agent` 的最小入口；把执行直接交给 `cli.main()`。 |
| `src/mica_agent/_file_lock.py` | JSONL Store 使用的跨平台独占文件锁；组合进程内线程锁与 Windows/POSIX 文件锁，提供超时和上下文管理。 |
| `src/mica_agent/app.py` | 应用层装配边界；绑定 SessionService、启动上下文、AgentEngine 和工具执行器，只在完整成功后提交会话轮次。 |
| `src/mica_agent/cancellation.py` | Provider、Engine、工具和交互等待共享的协作式取消令牌与固定取消异常。 |
| `src/mica_agent/cli.py` | 当前基础终端入口；负责参数解析、环境配置检查、工作区与会话选择、工具装配、权限/ask 输入、事件消费和资源关闭。 |
| `src/mica_agent/context_builder.py` | 会话激活时读取 global/workspace `AGENTS.md`，生成独立指令消息和安全诊断；不负责系统提示词正文。 |
| `src/mica_agent/engine.py` | 单次 Run 的核心循环；按序推进 Provider 请求、文本事件、工具调用、交互等待、限制、取消和唯一终态。 |
| `src/mica_agent/events.py` | Engine 对前端公开的有序事件契约，包括 Run、文本、工具、权限、用户输入和终态事件。 |
| `src/mica_agent/file_tools.py` | 当前文件工具实现与注册入口；提供 `read`、`write`、`edit` 和待移除的 `ls`，并执行大小限制、UTF-8、原子写入和安全结果转换。 |
| `src/mica_agent/interactions.py` | Engine 与前端之间的交互 Broker；分配不复用的交互 ID，等待权限/ask 回答，并清理取消、迟到或失效响应。 |
| `src/mica_agent/jsonl_store.py` | 一个会话一个 JSONL 文件的存储层；负责严格解析、锁内创建/读取/重写/删除、原子替换、扫描损坏和 v1 到 v2 升级。 |
| `src/mica_agent/message_sanitizer.py` | 工具消息进入 Provider、会话或诊断前的集中凭据清洗；隐藏敏感键、常见 Key 和 Bearer 凭据。 |
| `src/mica_agent/models.py` | 供应商无关的标准 `Message` 与角色定义，包含普通文本、工具调用和工具结果关联字段。 |
| `src/mica_agent/permissions.py` | 权限模式和策略决定的基础枚举：`default`、`accept_edits`、`solo` 以及 `ALLOW`、`ASK`、`DENY`。 |
| `src/mica_agent/policy.py` | 内存权限决策引擎；根据规范化操作、硬性拒绝、一次性授权、允许规则和权限模式给出策略决定。 |
| `src/mica_agent/runtime_tools.py` | 运行时工具实现与注册入口；提供 Windows 优先的受限 `bash` 和当前自由文本 `ask`，管理环境清理、输出上限、超时、进程树与等待取消。 |
| `src/mica_agent/schema_validation.py` | 使用固定 JSON Schema Draft 2020-12 校验 ToolSpec Schema 和实际工具参数，拒绝远程引用并返回安全错误。 |
| `src/mica_agent/session_contracts.py` | 会话公共数据和 JSON wire 契约；定义 SessionMeta、TurnEntry、v1/v2 严格读写、消息角色顺序和格式错误边界。 |
| `src/mica_agent/session_service.py` | 会话业务层；在 Store 之上提供创建、加载、列表、重命名、分支、回退、清空、提交、最后活跃选择和过期清理。 |
| `src/mica_agent/system_prompt.py` | 通过包资源加载内置提示词，取得一次性环境快照并组装冻结的会话系统提示词；不负责读取 `AGENTS.md`。 |
| `src/mica_agent/tool_contracts.py` | 供应商无关的工具数据契约；定义 ToolSpec、ToolCall、ToolRequest、ToolResult、状态枚举和深层不可变 JSON 快照。 |
| `src/mica_agent/tool_executor.py` | 工具统一执行入口；依次完成注册查询、参数校验、权限判定、交互等待、handler 调用、取消和安全 ToolResult 转换。 |
| `src/mica_agent/tool_registry.py` | 进程内工具注册表；保存 ToolSpec 与异步 handler，提供原子注册、确定性列表、重复拒绝和按名解析。 |
| `src/mica_agent/workspace.py` | 工作区身份与数据根边界；优先识别 Git 根目录，否则使用启动目录，并生成规范路径、slug、SHA-256 ID 和会话目录。 |
| `src/mica_agent/workspace_paths.py` | 文件工具共享的工作区路径解析器；拒绝绝对/越界路径、重解析点、Windows 别名、ADS 和集中定义的敏感文件。 |
| `src/mica_agent/providers/__init__.py` | Provider 子包的公共出口；只重导出供应商无关契约和错误，不执行网络请求。 |
| `src/mica_agent/providers/base.py` | Provider 公共协议；定义 ProviderRequest、流式文本/结束/错误事件、错误分类和 Provider 生命周期接口。 |
| `src/mica_agent/providers/openai_compatible.py` | OpenAI-compatible Chat Completions 适配器；构造 HTTP 请求、解析 SSE、组装工具调用、分类错误并执行有限网络重试。 |
| `src/mica_agent/prompts/system.md` | 随 wheel 分发的 UTF-8 内置系统提示词模板；只保存稳定正文和环境上下文占位符，不含会话动态状态或 `AGENTS.md` 内容。 |
