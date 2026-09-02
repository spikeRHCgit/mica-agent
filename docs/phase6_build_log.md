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
