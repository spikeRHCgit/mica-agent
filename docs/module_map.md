# Mica 源码地图

> 用来定位现有文件；文件里的具体行为以源码、测试和领域规格为准。
> 新增、删除、重命名文件，或文件职责改变时，同一任务同步更新本文。

地图里的**分组就是功能模块**——拆任务卡、判断体量时说的「模块」，以这里的分组为准。

## 索引

| 组（模块） | 管什么 |
|---|---|
| 应用入口 | CLI、应用装配和取消生命周期 |
| 配置与选择 | JSON 配置、来源、能力、凭据只读保护 |
| 公共契约 | 消息、事件、工具、会话和校验边界 |
| 会话与工作区 | 工作区身份、上下文、JSONL 和会话服务 |
| 工具与权限 | 工具注册、执行、权限、文件和命令 |
| Provider | Provider 协议及 OpenAI-compatible 实现 |
| 终端前端 | 主缓冲区输入、渲染、交互和运行控制 |
| 资源与包 | 系统提示词和包入口 |

测试：各组列出对应测试，通常按文件名对应源码行为；跨模块装配测试放在应用或会话组，共用的 SSE 测试辅助在 Provider 组。

## 应用入口

- `src/mica_agent/app.py`：连接装配、模型目录/空闲切换/默认保存、每轮冻结和运行持久化边界。
- `src/mica_agent/cli.py`：命令行覆盖、配置启动、会话选择和前端路由。
- `src/mica_agent/__main__.py`：`python -m mica_agent` 入口。
- `src/mica_agent/cancellation.py`：Provider、Engine、工具和交互共用的取消令牌。
- `tests/test_app.py`：应用装配、运行提交和失败边界。
- `tests/test_cli.py`：CLI 参数、输出和退出码。
- `tests/test_cli_interactive.py`：基础文字交互入口。
- `tests/test_cli_startup.py`：启动诊断和资源初始化。
- `tests/test_cli_tool_lifecycle.py`：CLI 工具运行生命周期。
- `tests/test_cancellation.py`：取消令牌和取消传播。

## 配置与选择

规格：`docs/phase6_plan.md`。

- `src/mica_agent/config_contracts.py`：不可变设置、能力、来源与独立连接绑定。
- `src/mica_agent/config_store.py`：配置 JSON 读取及默认目标项锁内原子保存。
- `src/mica_agent/config_service.py`：配置校验合并、启动选择及每轮有效能力。
- `src/mica_agent/_credential_permissions.py`：凭据文件及父目录的实际权限保护与只读访问。
- `tests/test_config_contracts.py`：配置类型、预算及映射不可变性。
- `tests/test_config_store.py`：严格 JSON、并发保存及失败字节保护。
- `tests/test_config_service.py`：来源选择、能力降级与敏感诊断。
- `tests/test_credential_permissions.py`：隔离文件/目录真实权限与查询失败验证。

## 公共契约

- `src/mica_agent/models.py`：供应商无关的消息和角色。
- `src/mica_agent/events.py`：Engine 向前端公开的有序事件。
- `src/mica_agent/reasoning.py`：思考强度与显示模式的稳定枚举。
- `src/mica_agent/tool_contracts.py`：工具定义、调用、请求、结果和状态。
- `src/mica_agent/checkpoint_contracts.py`：中断运行的进度检查点。
- `src/mica_agent/session_contracts.py`：会话元数据、轮次和 JSON wire 契约。
- `src/mica_agent/continuation_contracts.py`：模型步骤续接项、严格 wire、关联校验与单轮体量上限。
- `src/mica_agent/schema_validation.py`：ToolSpec Schema 和参数校验。
- `src/mica_agent/message_sanitizer.py`：工具消息、展示和诊断的敏感信息清洗。
- `tests/test_events.py`：事件构造和顺序契约。
- `tests/test_models.py`：消息和模型契约。
- `tests/test_reasoning.py`：思考强度契约。
- `tests/test_reasoning_integration.py`：思考事件跨模块集成。
- `tests/test_tool_contracts.py`：工具数据契约。
- `tests/test_session_contracts.py`：会话 wire 契约。
- `tests/test_continuation_contracts.py`：续接 wire、关联、体量与清洗回放判定契约。
- `tests/test_schema_validation.py`：Schema 校验。
- `tests/test_system_prompt.py`：提示词装配和清洗边界。

## 会话与工作区

- `src/mica_agent/workspace.py`：工作区根目录、身份和会话数据根。
- `src/mica_agent/workspace_paths.py`：工作区相对路径及敏感路径分类。
- `src/mica_agent/context_builder.py`：AGENTS 上下文读取、快照和安全诊断。
- `src/mica_agent/jsonl_store.py`：JSONL 会话保存、读取、锁和版本迁移。
- `src/mica_agent/session_service.py`：会话选择、修改和检查点业务；一次读取统一投影通用消息与逐 assistant 对齐的续接，完整步骤保留原记录，不完整批次、部分输出及中断状态作为 USER 历史工作记录。
- `tests/test_workspace.py`：工作区身份和根目录。
- `tests/test_workspace_paths.py`：路径解析和越界边界。
- `tests/test_context_builder.py`：AGENTS 读取和上下文快照。
- `tests/test_jsonl_store.py`：JSONL 存储、损坏和原子写入。
- `tests/test_session_service.py`：会话业务操作、统一恢复快照与续接对齐、检查点历史工作记录和只读旧格式恢复。
- `tests/test_session_integration.py`：会话与应用/存储集成。

## 工具与权限

- `src/mica_agent/tool_registry.py`：工具注册、解析和确定性列表。
- `src/mica_agent/tool_executor.py`：校验、授权、执行、取消和安全结果转换。
- `src/mica_agent/tool_presentation.py`：工具展示安全快照和视图数据。
- `src/mica_agent/file_tools.py`：工作区文件读写编辑工具。
- `src/mica_agent/runtime_tools.py`：Bash、ask、输出限制和进程清理。
- `src/mica_agent/permissions.py`：权限模式和决定枚举。
- `src/mica_agent/policy.py`：权限策略决策。
- `src/mica_agent/command_risk.py`：命令风险词法分类。
- `src/mica_agent/interactions.py`：权限和 ask 的交互 Broker。
- `src/mica_agent/_file_lock.py`：跨平台文件锁。
- `tests/test_tool_registry.py`：工具注册。
- `tests/test_tool_executor.py`：工具执行边界。
- `tests/test_tool_presentation.py`：工具展示快照。
- `tests/test_file_tools.py`：文件工具。
- `tests/test_runtime_tools.py`：运行时工具和进程。
- `tests/test_permissions.py`：权限模式和策略基础。
- `tests/test_policy.py`：权限决定。
- `tests/test_command_risk.py`：命令风险分类。
- `tests/test_interactions.py`：交互 Broker。
- `tests/test_permission_integration.py`：权限跨模块集成。
- `tests/test_tool_session_integration.py`：工具与会话集成。

## Provider

- `src/mica_agent/providers/base.py`：Provider 请求、事件、错误和生命周期协议。
- `src/mica_agent/providers/openai_compatible.py`：OpenAI-compatible Chat Completions SSE 适配器。
- `src/mica_agent/providers/openai_responses.py`：API Key Responses 请求编码、SSE 输出项关联、真实终态和有界续接。
- `src/mica_agent/providers/anthropic_messages.py`：Messages 历史和工具结果编码、SSE 内容块校验、真实终态及有界签名续接。
- `src/mica_agent/providers/__init__.py`：Provider 公共出口。
- `src/mica_agent/engine.py`：模型、工具、交互、取消和终态的单一 Agent Loop。
- `tests/test_provider_contract.py`：Provider 公共协议。
- `tests/test_openai_provider.py`：OpenAI-compatible Provider。
- `tests/test_openai_responses_provider.py`：Responses 请求、续接回放、工具往返、SSE 终态及资源边界。
- `tests/test_anthropic_messages_provider.py`：Messages 请求、思考和签名回放、Engine 工具往返、SSE 终态、预算及资源边界。
- `tests/test_engine.py`：Engine 运行循环和终态。
- `tests/sse_transport_helpers.py`：Provider 测试共用的 SSE 传输辅助。

## 终端前端

规格：`docs/tui_design.md`；视觉验收：`docs/assets/tui/README.md`。

- `src/mica_agent/tui/models.py`：终端视图状态和共享枚举。
- `src/mica_agent/tui/__init__.py`：终端前端公共出口。
- `src/mica_agent/tui/main_buffer/terminal.py`：终端输入、尺寸、能力和输出 IO。
- `src/mica_agent/tui/main_buffer/sanitize.py`：不可信正文到安全 ANSI 的清洗。
- `src/mica_agent/tui/main_buffer/renderer.py`：语义块、差分帧和物理行布局。
- `src/mica_agent/tui/main_buffer/components.py`：启动、正文、状态和底部组件。
- `src/mica_agent/tui/main_buffer/markdown.py`：Markdown 清洗渲染和流式正文。
- `src/mica_agent/tui/main_buffer/tool_views.py`：内置工具和通用工具视图。
- `src/mica_agent/tui/main_buffer/editor.py`：草稿、光标、软换行和粘贴片段。
- `src/mica_agent/tui/main_buffer/commands.py`：本地命令登记、匹配和输入框候选。
- `src/mica_agent/tui/main_buffer/menus.py`：模型菜单筛选、导航、容量确认与面板排版。
- `src/mica_agent/tui/main_buffer/interactions.py`：Ask/Permission 输入模式。
- `src/mica_agent/tui/main_buffer/runtime.py`：单 Run 控制器和资源收口。
- `src/mica_agent/tui/main_buffer/screen.py`：主缓冲区编排、模式机和事件投影。
- `src/mica_agent/tui/main_buffer/__init__.py`：主缓冲区公共出口。
- `tests/test_tui_models.py`：终端视图模型。
- `tests/test_tui_main_buffer_cli.py`：主缓冲区 CLI 装配。
- `tests/test_tui_main_buffer_composer.py`：编辑器和 composer。
- `tests/test_tui_main_buffer_commands.py`：命令匹配、候选交互和五行窗口。
- `tests/test_tui_main_buffer_menus.py`：模型菜单输入、独立标记、配色和宽高降级。
- `tests/test_tui_main_buffer_events.py`：事件投影。
- `tests/test_tui_main_buffer_hierarchy.py`：内容层级。
- `tests/test_tui_main_buffer_lifecycle.py`：生命周期。
- `tests/test_tui_main_buffer_markdown.py`：Markdown 展示。
- `tests/test_tui_main_buffer_redraw.py`：差分重绘。
- `tests/test_tui_main_buffer_refresh.py`：刷新调度。
- `tests/test_tui_main_buffer_render.py`：渲染布局。
- `tests/test_tui_main_buffer_sanitize.py`：终端清洗。
- `tests/test_tui_main_buffer_screen.py`：主屏幕状态和输入。
- `tests/test_tui_main_buffer_tool_views.py`：工具视图。
- `tests/test_tui_main_buffer_vt.py`：Windows VT 输入。

## 资源与包

- `src/mica_agent/__init__.py`：顶层公共 API 和惰性导出。
- `src/mica_agent/system_prompt.py`：内置提示词读取及会话系统指引装配。
- `src/mica_agent/prompts/system.md`：随包分发的系统提示词资源。
- `tests/test_public_api.py`：顶层公共 API。
- `tests/test_distribution.py`：构建产物和隔离安装。

## 常见改动

- 新增工具：同步工具契约、注册、执行/权限、展示、相关测试和本地图对应组。
- 新增 Provider：同步 Provider 协议、配置选择、应用装配、错误/取消边界、测试和本地图。
- 改终端输入或渲染：同步终端规格、相关测试和视觉验收记录。
- 改会话格式：同步契约、Store、Service、上下文、兼容测试和历史说明。
