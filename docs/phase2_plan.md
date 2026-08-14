# Mica 第二阶段：Provider、Engine 与基础 CLI 对话闭环

> 状态：已批准，施工中
> 上位约束：`PROJECT_PLAN.md`
> 前置阶段：`docs/phase1_plan.md`

## 1. 阶段目标

第二阶段打通 Mica 的第一条真实业务链路：用户通过基础 CLI 输入文本，Mica
使用 OpenAI-compatible Provider 请求模型，Provider 将流式响应转换为供应商无关
的结构化事件，Agent Engine 管理一次 Run 的生命周期，CLI 实时显示文本并在
请求结束时给出明确终态。

本阶段的核心产物不是一批空的目录或接口，而是一条可以在无真实网络的自动化测试
中重复验证、并能使用真实 Provider 独立验收的最小闭环。

## 2. 用户可见结果

提供基础交互式入口：

```text
mica cli
```

用户可以在同一进程中连续提交多条文本指令。当前进程内保留供应商无关的消息历史，
因此后续请求可以使用前文上下文；退出时暂不保存会话文件，退出方式为 `Ctrl+C`
或输入流结束（EOF）。

当前阶段只保证文本对话。CLI 不提供最终版 TUI、状态栏、斜杠命令、快捷键注册、
模型选择器或配置弹窗。

## 3. 构建范围

### 3.1 必须交付

- `MicaApp` 的最小装配入口，负责连接 CLI、Engine、Provider 和当前内存上下文。
- 供应商无关的文本消息模型，至少支持 `system`、`user` 和 `assistant` 角色。
- `AgentEngine`，负责创建 Run、提交消息、消费 Provider 事件和报告终态。
- OpenAI-compatible Provider，使用 Chat Completions 的流式接口。
- 流式文本增量、正常结束、服务端错误、鉴权错误、超时和畸形响应的结构化处理。
- Provider 网络请求边界上的有限重试；不得重试已经产生工具副作用的操作（本阶段尚无工具）。
- 统一的 `completed`、`failed`、`cancelled` 终态，以及带运行标识和顺序信息的事件。
- 基础 `mica cli` 交互循环，实时打印文本增量，正确处理 `Ctrl+C` 和 EOF。
- 通过环境变量读取本阶段临时连接参数：
  - `MICA_API_URL`
  - `MICA_API_KEY`
  - `MICA_MODEL`
- 缺少必要连接参数时，在网络请求前输出可操作诊断，不发送空配置请求。
- 使用固定 HTTP 传输响应的自动化测试，不依赖外部网络、真实 Key 或随机模型输出。
- 使用用户提供的真实 Provider 进行一次独立集成验收；Key 只能来自本机环境变量或被 Git 忽略的本地配置。

### 3.2 明确不包含

- Tool Calling、工具注册、工具执行和权限系统。
- JSONL 会话、`--continue`、`--resume`、`/new`、`/rename`、`/branch`、`/rewind`、`/clear`、`/btw` 或 `/compact`。
- `config.json`、全局/项目配置合并、环境变量覆盖的最终配置系统和首次配置向导。
- 模型列表探测、模型切换、上下文上限计算和上下文压缩。
- Textual TUI、状态栏、弹窗、最终快捷键语义和完整 CLI/TUI 共用前端层。
- ExtensionAPI、MCP、Skill、npm 启动器和分发安装链路。
- Anthropic 原生协议或其他非 OpenAI-compatible 协议实现。
- FakeProvider 产品实现；测试替身只存在于 HTTP 传输边界。
- GenericAgent 旧代码、旧配置、旧会话和旧 API 的兼容迁移。

## 4. 阶段契约

本阶段只定稿支撑当前闭环所需的最小字段，后续阶段可以向后兼容地扩展。

### 4.1 Standard Message

消息必须是供应商无关的数据对象，至少包含：

- `role`：`system`、`user` 或 `assistant`。
- `content`：文本内容。

Provider 可以将其转换为 OpenAI-compatible 请求格式，但 Engine 和 CLI 不得依赖
OpenAI 私有消息对象。当前不保存工具消息、图像内容或供应商私有字段。

### 4.2 Provider

Provider 至少提供以下能力：

- 接收连接信息、模型 ID 和标准消息历史。
- 以异步流的形式产生标准化文本增量和结束/错误信息。
- 在模型请求边界执行有限重试。
- 不保存会话真相，不执行工具，不决定权限，不修改 CLI 状态。

OpenAI-compatible 是协议实现，不等同于某一家供应商。后续可以增加供应商预设
目录，将供应商名称、默认 API URL、模型建议和请求头映射到同一个兼容协议 Provider；
只有协议真正不同的供应商才需要独立 Provider 或扩展。本阶段不实现预设选择界面，
但 Provider 入口不得设计成只能硬编码单一供应商。

### 4.3 Engine Event

Engine 向前端输出有序事件。事件至少需要表达：

- `run_started`：Run 标识和开始信息。
- `text_delta`：按顺序排列的文本增量。
- `run_completed`：正常完成。
- `run_failed`：错误类别和面向用户的诊断。
- `run_cancelled`：用户取消或运行上下文取消。

事件必须带有可关联当前 Run 的标识；CLI 只消费公开事件，不读取 Provider 私有
状态或拼接底层响应对象。

### 4.4 MicaApp 与 CLI

`MicaApp` 负责装配依赖和维护当前进程内的消息历史；Engine 不读取环境变量，
CLI 不直接发 HTTP 请求。CLI 负责输入、事件渲染和退出信号转换，不能定义模型协议。

## 5. 建议任务拆分

任务编号用于后续派工，具体文件由 worker 在阶段契约内确定；不预先创建空包。

### P2-01：跨模块模型与事件契约

状态：已完成。

建立最小 Standard Message、Provider 响应事件、Engine Event 和终态对象，并用
单元测试固定字段、顺序和错误语义。不得实现网络请求或 CLI 交互。

### P2-02：OpenAI-compatible Provider

状态：已完成。

基于 `httpx` 实现 Chat Completions 流式请求、SSE 增量解析、响应错误分类、超时
和有限重试。使用可注入的 HTTP transport 进行自动化测试；不得添加 FakeProvider。

### P2-03：AgentEngine 与 MicaApp 装配

状态：已完成。

实现单次 Run 的异步编排、当前进程消息历史、取消传播和 Provider 事件到 Engine
事件的转换。Engine 不读取配置、不写会话、不依赖前端。

### P2-04：基础 CLI 闭环

状态：已完成。

将 `mica cli` 接入 MicaApp，支持连续文本输入、实时事件渲染、`Ctrl+C` 中断当前
运行和 EOF 退出。保留已有 `--help`、`--version` 和无效参数公共契约；不注册未来
命令占位符。

### P2-05：集成验证与文档收口

状态：待实施。

补充跨模块自动化测试、构建验证、仓库外 CLI 烟测和真实 Provider 集成验收说明，
更新 README 的当前能力描述，并由 master 审查所有边界和依赖变化。

## 6. 测试与验收安排

### 6.1 自动化测试

- Standard Message 和事件对象的字段、顺序、终态测试。
- SSE 多分片文本拼接、空分片、结束块和畸形块测试。
- HTTP 状态错误、鉴权失败、超时、限流和临时服务端错误分类测试。
- 只对允许重试的错误重试，并验证次数上限和退避行为。
- Engine 在正常完成、Provider 失败、取消和重复输入场景下的结果测试。
- CLI 在无配置、成功响应、失败响应、`Ctrl+C` 和 EOF 下的可观察行为测试。
- 所有默认测试不访问真实网络，不读取用户真实配置，不写入 `~/.mica`。

### 6.2 真实 Provider 集成验收

在本机使用用户提供的真实 API URL、Key 和模型执行最小文本请求，至少确认：

- 请求能够完成并返回文本增量。
- 真实服务的 SSE 格式能被当前解析器正确消费。
- Key 没有出现在终端诊断、测试输出、日志或 Git diff 中。
- 验收结果只记录通过/失败、服务兼容性和必要修正，不保存完整敏感响应。

### 6.3 阶段达标标准

第二阶段只有同时满足以下条件才算完成：

- `mica cli` 可以在配置齐全时完成连续多轮文本对话。
- Engine、Provider、App 和 CLI 之间没有反向依赖或私有状态泄漏。
- 流式文本、失败、取消和重试语义都有自动化测试证据。
- `uv run pytest`、Ruff 检查、格式检查和 `uv build --offline` 全部通过。
- 构建产物在仓库外仍可启动基础 CLI，且不导入 GenericAgent。
- 真实 Provider 集成验收通过，或明确记录为外部服务兼容性阻塞。
- README 和本阶段计划与实际能力一致。

## 7. 风险与后续输入

- OpenAI-compatible 服务对路径、鉴权头、SSE 细节和错误 JSON 的实现可能不同；Provider
  必须把差异限制在协议适配边界，不能污染 Engine。
- 本阶段通过环境变量启动是临时工程入口，不能替代后续 JSON 配置系统，也不应把
  API Key 写进代码、计划或提交历史。
- 供应商预设是后续配置/Provider 阶段的需求：优先复用 OpenAI-compatible 实现，
  不为每家兼容供应商复制一套协议代码。
- 取消语义本阶段只覆盖 Provider 请求和 CLI Run；工具、子进程和统一取消令牌在
  后续工具阶段扩展。
