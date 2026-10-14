# 第 1 轮审查 · 2026-10-14

依据：[`T-031.md`](T-031.md)、[`worker-01.md`](worker-01.md)。

## 审查范围与证据

- 工作区仅包含卡片允许的 `anthropic_messages.py`、对应测试、`docs/module_map.md` 登记和本轮汇报；没有修改既有 Provider、Engine、公共契约、依赖或其他管理文件。
- 核对实现的请求编码、system/messages 分离、工具 schema、adaptive/off 思考配置、Messages 历史工具结果配对、兼容续接槽位消费、非法 ID 的请求内映射及签名回放限制。
- 核对 SSE 生命周期：多行 data、event/type、ping、内容块起止、正文/思考/签名/工具参数碎片、message_delta 与 message_stop 的终态关系，以及失败、截断、暂停、拒绝、断流和取消路径。
- 核对 `_MessagesStep` 的续接状态：正常无专属内容返回 `None`；有效 thinking、redacted thinking、正文引用和工具引用返回 `REPLAYABLE`；未知语义、缺失签名、多正文和超限保持 `UNREPLAYABLE`，超限释放后不可恢复。
- 采信 worker 验证：专项测试 171 passed；全量（含离线构建、隔离 wheel 安装和安装态行为）2518 passed、2 skipped；Ruff、格式和 `git diff --check` 通过。两项跳过均为既有 Windows 目录符号链接权限不足（WinError 1314），没有新增 skip 或放宽断言。
- master 补充关键回归抽验：`uv run pytest tests/test_anthropic_messages_provider.py -q -W error -k 'request_literal or complete_finish_shapes or real_engine_two_step_multi_tool_roundtrip or malformed_lifecycle or exact_wire_budget or incremental_overflow or stream_errors or cancellation or replay_binding or compatible_replay_order'` → `66 passed, 105 deselected in 0.41s`，exit 0。

## 发现

未发现阻止验收的问题。适配器保持单次请求职责，Engine 仍是工具和循环的唯一所有者；服务端工具、未完成工具、签名拒绝和不支持的模型配置均未被伪装为成功。worker 标出的协议边界与卡片方案一致，本卡不宣称真实渠道或用户终端验收。

## 结论

验收通过，T-031 完成，B 第 5 步完成。下一步为 B 第 6 步应用装配；B 交付项全部完成后仍需用户进行三类接口和真实渠道验收。
