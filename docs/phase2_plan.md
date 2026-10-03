# Mica 第二阶段：Provider、Engine 与基础 CLI 对话闭环

> 状态：已完成
> 上位约束：`docs/PROJECT_PLAN.md`
> 前置阶段：`docs/phase1_plan.md`

## 1. 目标

打通第一条真实对话链路：基础 CLI 接收用户文本，Engine 管理一次运行，OpenAI-compatible Provider 返回流式响应，前端显示文本增量和明确终态。

## 2. 已交付

- 提供供应商无关的标准文本消息、Provider 响应和带 Run 标识及顺序信息的 Engine 事件。
- 实现 OpenAI-compatible Chat Completions 流式 Provider，包括 SSE 解析、错误分类、超时和请求边界上的有限重试。
- 建立 AgentEngine 与 MicaApp 装配，使 CLI 消费公开事件；Provider、Engine 与 CLI 各自承担协议、运行编排和输入输出职责。
- 提供 `mica cli` 基础文字对话入口，支持连续输入、流式显示、运行失败诊断与取消。
- 通过 `MICA_API_URL`、`MICA_API_KEY` 和 `MICA_MODEL` 环境变量提供当时的连接入口，并在缺少必需参数时于发起请求前给出诊断。

当前 CLI 用法以 `README.md` 为准；现行模块职责和公共入口以 `docs/module_map.md` 及源码为准。

## 3. 实现概况

以下记录第二阶段建立、现行代码仍沿用的基础对话链路；涉及后续扩展的地方标明当前实现与阶段交付的区别。

- **跨模块数据契约**：`src/mica_agent/models.py` 用不可变的 `Message` 和 `MessageRole` 表示供应商无关的消息；`providers/base.py` 用 `ProviderRequest`、`TextDelta`、`Finish`、`ProviderErrorEvent` 和 `Provider` 异步流协议隔离模型协议。`events.py` 定义前端消费的事件，使用同一 `run_id` 和递增 `seq` 关联一轮运行的增量与终态。
- **OpenAI-compatible 适配**：`providers/openai_compatible.py` 将 base URL 解析为 Chat Completions 端点，用 `httpx.AsyncClient.stream()` 发起请求，逐条读取 SSE 的 `data:`，把响应片段转换为标准增量、完成或错误事件。HTTP 状态和传输异常被归入鉴权、限流、超时、临时故障或畸形响应；有限重试只发生在允许重试且尚未产生输出的请求边界，不重放已经开始的流。
- **单次 Run 编排**：`AgentEngine.run()` 接收 `ProviderRequest`，先发 `RunStarted`，将 Provider 文本增量转换为 Engine `TextDelta`，再以 `RunCompleted`、`RunFailed` 或 `RunCancelled` 中的一种结束。Engine 负责运行顺序和错误映射，不读取连接环境变量，也不把 HTTP 响应对象暴露给前端。
- **应用与基础文字前端**：`MicaApp.run()` 将本次用户消息与已有上下文组装成请求，消费 Engine 事件并管理当前运行；现行上下文来源已由后续阶段扩展为 `SessionService`。`cli.py` 在装配 Provider 前检查三个必需环境变量；`run_interactive()` 支持注入 Provider、输入和输出接缝，基础文字循环 `_interact()` 连续读取用户输入、实时写出 `TextDelta`，并把失败、取消与 EOF 转为可见的终态或退出行为。现行默认终端前端由后续阶段交付，基础文字前端仍可通过 `--plain` 使用。

## 4. 验收结论

第二阶段已通过标准消息、事件顺序、Provider 流与错误路径、Engine 终态和 CLI 连续对话的自动化验证，并完成构建、仓库外运行及真实 Provider 集成验收。阶段成果为基础文本对话闭环；后续阶段在此基础上扩展了会话持久化、工具、权限与终端前端。
