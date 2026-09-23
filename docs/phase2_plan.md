# Mica 第二阶段：Provider、Engine 与基础 CLI 对话闭环

> 状态：已完成
> 上位约束：`docs/PROJECT_PLAN.md`
> 前置阶段：`docs/phase1_plan.md`

## 1. 阶段目标

打通第一条真实对话链路：基础 CLI 接收用户文本，Engine 管理一次运行，OpenAI-compatible Provider 返回流式响应，前端显示文本增量和明确终态。

## 2. 已交付能力

- 提供供应商无关的标准文本消息、Provider 响应和带 Run 标识及顺序信息的 Engine 事件。
- 实现 OpenAI-compatible Chat Completions 流式 Provider，包括 SSE 解析、错误分类、超时和请求边界上的有限重试。
- 建立 AgentEngine 与 MicaApp 装配，使 CLI 消费公开事件；Provider、Engine 与 CLI 各自承担协议、运行编排和输入输出职责。
- 提供 `mica cli` 基础文字对话入口，支持连续输入、流式显示、运行失败诊断与取消。
- 通过 `MICA_API_URL`、`MICA_API_KEY` 和 `MICA_MODEL` 环境变量提供当时的连接入口，并在缺少必需参数时于发起请求前给出诊断。

当前 CLI 用法以 `README.md` 为准；现行模块职责和公共入口以 `docs/module_map.md` 及源码为准。

## 3. 验收结论

第二阶段已通过标准消息、事件顺序、Provider 流与错误路径、Engine 终态和 CLI 连续对话的自动化验证，并完成构建、仓库外运行及真实 Provider 集成验收。阶段成果为基础文本对话闭环；后续阶段在此基础上扩展了会话持久化、工具、权限与终端前端。
