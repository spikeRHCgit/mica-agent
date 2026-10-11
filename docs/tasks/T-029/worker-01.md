# 第 1 轮汇报 · 2026-10-11

依据：[T-029.md](T-029.md)。

- 结果：完成 —— Provider/Finish 契约、Engine 安全续接携带及 DeepSeek/OpenRouter 回传已实现，局部与全量验证通过。
- 改动：`src/mica_agent/providers/base.py`、`src/mica_agent/providers/openai_compatible.py`、`src/mica_agent/engine.py`、`tests/test_provider_contract.py`、`tests/test_openai_provider.py`、`tests/test_engine.py`，以及本汇报。
- 验证：局部 pytest `337 passed in 5.86s`；全量 pytest `2171 passed, 2 skipped in 148.50s (0:02:28)`；全仓 Ruff `All checks passed!`；格式检查 `112 files already formatted`；所有命令退出码为 0。
- 偏离与疑点：无。
- 建议：无。

### 明细

- [x] ProviderRequest 默认兼容、槽位数量与类型、关联、来源过滤及 Finish 安全 repr：`test_request_continuation_slots_and_finish_are_compatible_and_private`、`test_request_continuation_validation_is_safe`、`test_request_checks_ordered_message_association`；新增字段均在末尾且有默认值。
- [x] 两次工具往返和最终回答的槽位增长、完成回调及历史隔离：`test_continuations_grow_across_tools_and_completion_excludes_history` 断言请求为 `(old,)` → `(old, first)` → `(old, first, None)`，完成回调只收到 `(first, None, last)`，成功终态前恰好一次。
- [x] 原始工具参数与思考敏感内容判定：`test_original_message_continuation_safety_reaches_request_commit_and_cancel` 分别覆盖假凭据参数与本地路径思考。续接判定在工具消息清洗前执行；原始参数交给 ToolExecutor，下一请求、完成回调、取消快照均得到无 items 的不可回放标记。
- [x] 工具执行取消、后续流失败、纯思考完成、状态更新保留续接：`test_tool_cancellation_preserves_finished_continuation` 对 not_started/in_progress 使用实际取消信号；`test_finished_step_continuation_survives_tool_status_and_second_stream_failure` 覆盖 success/failed 与第二步部分正文；`test_enabled_channel_pure_reasoning_finishes_only_with_known_source` 验证纯思考正常 Finish，来源未知保持旧空响应错误。
- [x] 单轮 1 MiB 精确边界、超限全降级且不恢复、历史多轮独立：`test_turn_continuation_budget_is_exact_sticky_and_updates_progress` 同时覆盖正常完成与工具取消、恰好上限与超出 1 字节，核对下一请求/已有 progress/完成回调；`test_multiple_history_turns_do_not_share_current_turn_budget` 使用两个各 600000 字符的历史记录，不把历史多轮合并限额。
- [x] DeepSeek 字符串累积与带/不带 tools 的完整历史回传：`test_deepseek_accumulates_and_replays_every_history_assistant` 对完整请求字面量作等值断言，包含此前普通回答和最近工具步骤两个 assistant 的思考，不只回传最近步骤。
- [x] OpenRouter 字符串、三个详情类型、同/不同 index 顺序、完整字面量及思考去重：`test_openrouter_preserves_detail_arrival_order_and_exact_replay_fields` 保存 index 顺序 `8,8,2,0`，签名/密文原值不变；回传仅编码适用且非 None 字段，保留空 summary；`test_openrouter_reasoning_display_uses_one_source` 验证字符串优先和仅详情明文兜底，空字符串与 None 区别保留。
- [x] 未知字段/类型降级、流式有界累积、协议损坏/断流/取消真实终态：`test_unknown_or_unrepresentable_details_discard_step_but_keep_tool_facts`、`test_openrouter_invalid_details_container_marks_step_unreplayable` 保留正文/工具事实；`test_stream_continuation_budget_uses_wire_bytes_and_keeps_visible_stream` 在流未结束时检查已释放累计内容，覆盖 UTF-8 与转义字节；`test_openrouter_over_budget_details_release_opaque_data_and_do_not_restore` 检查密文累计释放；`test_continuation_does_not_hide_protocol_errors_or_trigger_retry` 和 `test_cancelled_reasoning_stream_never_produces_finish_continuation` 检查真实错误/取消、单次请求与资源关闭。
- [x] 来源/模型/接口不匹配、不可回放、关闭渠道的过滤及既有回归：Engine 和 Chat Completions 对应参数化过滤测试保留所有通用消息；默认关闭不产生/发送专属字段。只读核对 App.run 的 ProviderRequest/Engine.run 调用及 CLI 默认工厂，无需改动；Go 继续使用关闭默认值，实际渠道装配和历史恢复留给第 6 步。
- [x] 回调失败单终态和诊断、repr 安全：`test_continuation_sink_failure_has_one_safe_terminal_and_validates_callable` 验证 callable 边界、固定错误文字及唯一失败终态；`test_invalid_finish_continuation_stops_before_tools` 验证绑定错误和关联错误在工具准备/执行前失败，敏感参数导致不可回放也不能掩盖原记录的关联损坏。
- [x] 局部与全量 pytest、Ruff 检查和格式检查：最终产品/测试版本均通过，下表列明命令及平台跳过项。全量包含现有安装态验证，未删改或跳过本卡测试。
- [x] 通读产品与测试 diff，`git diff --check` 通过；`git status --short` 只显示上述六个允许文件和本汇报。初始工作区干净，保持 main，未建分支、暂存、提交、合并或推送。

### 协议依据与实现边界

DeepSeek 的带 tools 请求回传此前全部 assistant 思考，依据 [Thinking Mode](https://api-docs.deepseek.com/guides/thinking_mode/)；OpenRouter 保留有序详情和完整回传块，依据 [Reasoning Tokens](https://openrouter.ai/docs/guides/best-practices/reasoning-tokens)。本轮只访问公开协议文档，测试采用 fake Provider、假凭据、HTTP transport 替身和既有安装态本地服务器，不读取真实 `~/.mica` 或访问真实渠道。

开发验证曾发现新增测试误用了不存在的 `ToolResultStatus.FAILED` / `registry.snapshot()`，已按真实契约改为 `EXECUTION_FAILED` / `specs()`；取消测试最初使用立即完成的工具导致观察到 SUCCESS，已改为等待实际取消信号，保持 IN_PROGRESS 断言。未通过放宽断言或跳过测试处理失败。

### 最终验证

| 命令 | 结果 |
|---|---|
| `uv run pytest tests/test_provider_contract.py tests/test_openai_provider.py tests/test_engine.py -q -W error` | `337 passed in 5.86s`，退出码 0 |
| `uv run pytest -q -W error` | `2171 passed, 2 skipped in 148.50s (0:02:28)`，退出码 0，含现有安装态验证 |
| `uv run ruff check src/mica_agent tests` | `All checks passed!`，退出码 0 |
| `uv run ruff format --check src/mica_agent tests` | `112 files already formatted`，退出码 0 |
| `git diff --check` | 无输出，退出码 0 |
| `uv run pytest tests/test_tool_presentation.py::test_workspace_reparse_point_is_not_retained_in_presentation tests/test_workspace_paths.py::test_resolver_rejects_existing_symbolic_link_in_path -q -W error -rs` | `2 skipped in 0.09s`，仅补查全量跳过原因，退出码 0 |

跳过项为 `test_workspace_reparse_point_is_not_retained_in_presentation` 和 `test_resolver_rejects_existing_symbolic_link_in_path`：均由既有测试在创建临时目录符号链接时遇到 `WinError 1314`（当前进程无创建符号链接权限）而跳过。本轮未修改这两个文件或测试条件；对应 Windows 目录 junction 测试随全量执行，未新增人工跳过。

本轮未执行真实渠道或用户真实终端验收；不将本地替身、自动化和安装态结果表述为真实渠道通过。App 连接渠道、历史续接恢复和保存仍由第 6 步装配。
