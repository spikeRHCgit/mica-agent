# T-026 第 1 轮审查 · 2026-10-11

依据：[`T-026.md`](T-026.md)、[`worker-01.md`](worker-01.md)

## 审查范围

- 改动对照：`git status --short` 只有 `docs/tasks/T-026/worker-01.md`，任务卡与 HEAD 一致；没有源码、测试、依赖或其他文档改动。
- 判断审查：13 个问题逐条核对覆盖面、来源类型标注和未核实项的写法；第 13 条只给候选和代价，没有替 master 选方案或规定 wire。
- 补充验证：只读抽查两条直接决定 B 设计的事实：
  - DeepSeek Thinking Mode 页：请求带 tools 时，要回传所有之前轮次的 `reasoning_content`，包括没有调用工具的轮次；不带 tools 时回传会被忽略。与汇报第 9 条一致。
  - OpenCode Go 页：逐行核对 endpoint 表，`gpt-6-luna`、`gpt-5.6-luna` 走 `/responses`，另有 `x-opencode-session` 与自有 User-Agent 要求。与汇报第 11 条一致。第一次摘要式抓取误把两款 Luna 归到 Chat Completions，逐行原文核对后排除。

## 发现

- 无范围偏离。来源类型与未核实边界写得清楚，没有把示例或 SDK 类型声明写成服务端保证。
- 第 9 条的 DeepSeek 规则意味着「续接」不只是同轮工具往返：带 tools 的后续请求要携带此前所有轮次的思考内容。这会影响续接数据的持久化范围和体量，需要在设计讨论里明确。
- 第 7 条的 Messages 前缀绑定意味着单存签名不足以保证合法回放；跨接口或改动历史前缀后应判为不可回放，这和计划中「不能合法转换的组合降级为普通历史」一致。
- 第 10 条确认现有 Chat Completions 适配器只做思考展示，没有续接回传；OpenRouter 的 `reasoning`/`reasoning_details` 未解析，Go 的会话头未装配。这些都属于计划已预留的「渠道明确要求回传的续接数据在本项卡片中核实后接入」。

## 结论

通过。汇报可以作为 B 设计讨论的事实依据。
