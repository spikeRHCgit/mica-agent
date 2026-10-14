# 第 1 轮审查 · 2026-10-14

依据：[`T-032.md`](T-032.md)、[`worker-01.md`](worker-01.md)。

## 审查范围与证据

- Git 核对：当前 main，暂存区为空；实际改动仅为卡片允许的 SessionService、专项测试、module_map 和本轮报告。未修改 App、CLI、Engine、适配器、公共持久化契约或依赖。
- 核对统一快照入口：一次 store.load 同时投影消息和续接；旧入口只委托返回 messages；按 assistant 顺序使用 strict zip 和既有 check_step_association 核对关联，不按目标来源过滤、不改写专属字段、不套用全会话 1 MiB 限额。
- 核对检查点投影：全完成批次恢复全部原调用、真实结果和原续接；无正文但有续接的步骤保留空 assistant；不完整批次整体转为确定性 JSON 的 USER 历史工作记录，不重新发出旧调用或补造结果；partial 和中断状态也使用 USER，删除旧 SYSTEM 和前缀投影路径。
- 核对关键专项测试：一次读取与旧入口委托、敏感 repr、三接口完整工具批次、混排时间线与槽位、生命周期及不完整批次的精确事实断言。旧 read 成功、write 未开始的身份、参数、真实结果和缺失结果保护已迁移，不是简单删除旧断言。
- 采信 worker 验证：专项 55 passed；全量（含安装态）9 failed、2537 passed、2 skipped；Ruff、格式和 diff 检查通过。两项 skip 为既有 Windows 符号链接权限不足（WinError 1314）。全量后仅专项样本补强，已重跑专项和静态检查；本轮没有最终全量通过证据。
- master 定向复现报告所列 9 项：`uv --directory C:/Users/10601/Desktop/learning/mica_agent run pytest tests/test_app.py tests/test_tool_session_integration.py tests/test_tui_main_buffer_screen.py -q -W error --tb=line -k 'reasoning_then_failure_does_not_commit_partial_turn or follow_up_run_sees_checkpoint_context or cancelled_checkpoint_restores_under_a_new_model or running_token_cancellation_restores_partial_without_forged_assistant or permission_wait_cancel_commits_unstarted_tool_checkpoint or ask_wait_cancel_commits_in_progress_tool_checkpoint or failed_run_does_not_break_following_successful_run or tool_batch_cancel_keeps_completed_prefix_and_unstarted_call or escape_cancels_active_run_without_committing_turn'` → `9 failed, 130 deselected in 2.07s`，exit 1；失败位置、旧预期和实际新投影均与报告一致。未重复全量。

## 发现

1. 任务卡已批准 context_snapshot 的公共行为变化，但 master 仅允许修改 SessionService 专项测试，遗漏了直接消费该投影的三个测试文件。worker 按写入边界停下并交回是正确的；不能以恢复旧 SYSTEM 或截取工具前缀来解决此阻塞。
2. `tests/test_app.py` 的七项失败要求旧 SYSTEM 或旧状态文案；相关同一测试中的后续请求断言也需一并迁移，不能只修第一个失败位置。须保留固定系统头、部分输出不成为成功 assistant、无成功 Turn、取消终态、外部 task.cancel 与运行 token 取消、Permission 未执行和 Ask 未完成，以及失败后下一轮正常恢复的保护。
3. `tests/test_tool_session_integration.py` 的一项失败要求已完成前缀 assistant/TOOL。应改为完整 USER 工作记录的精确事实断言，保留真实 A 成功结果、B 未开始且结果为 null、取消批次的持久化状态和未伪造结果的保护；不改工具执行或取消流程。
4. `tests/test_tui_main_buffer_screen.py` 的一项失败要求中断状态为 SYSTEM。应迁移为明确的 USER 历史记录，保留真实 Escape 取消入口、无成功 Turn、CANCELLED 检查点、草稿 x、idle/stop 状态和 frame 的原有保护；不改 TUI 产品代码。

## 结论

第一轮受阻，T-032 不验收、不标记完成。范围内实现暂未发现阻止交付的问题，但全量仍失败；当前不能宣称自动化验收通过、B 完成或真实渠道验收完成。

建议用户授权下一轮测试迁移：只追加上述三个测试文件的相关断言及 `worker-02.md` 写入范围，保留已批准的产品实现和独有安全保护。用户确认后再编写 `rework-01-request.md`、登记返工并派发；稳定后由 worker 运行受影响链路及最终全量、Ruff、格式和 diff 检查。App/CLI 续接装配和真实渠道验收仍留后续卡。
