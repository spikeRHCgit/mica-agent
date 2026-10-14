# 第 2 轮审查 · 2026-10-14

依据：[`T-032.md`](T-032.md)、[`worker-02.md`](worker-02.md)、[`rework-01-request.md`](rework-01-request.md)。第一轮实现判断见 [`review-01.md`](review-01.md)。

## 审查范围与证据

- Git 核对：main，暂存区为空；本轮新增改动为三个授权调用侧测试文件及本轮报告，第一轮 SessionService、专项测试和 module_map 的既有改动仍在。没有范围外源码、契约、依赖或共享 fixture 改动。
- 核对三个测试文件完整实质 diff 和关键取消流程：App 只迁移七项测试并增加三个文件内历史消息 helper；工具集成只迁移一项并重命名；TUI 只迁移一项及必要导入。expected 使用明确消息标签和事实 JSON，不调用被测投影或私有编码函数。
- 核对第一轮产品及 module_map diff，与第一轮审查的统一入口、完整步骤及 USER 工作记录实现一致；第一轮三个文件和管理文件的字节不变证据采信本轮报告中的开工/收尾 SHA-256 比较，不将其表述为 master 独立完成的哈希核对。
- 采信本轮稳定版本相关链路 194 passed、最终全量（含安装态）2546 passed、2 skipped，以及 Ruff、格式和 diff 检查；详情见本轮报告。两项 skip 为既有 Windows 符号链接创建权限不足（WinError 1314），没有新增 skip/xfail。
- 本卡风险为高，master 定向抽验九项迁移：`uv --directory C:/Users/10601/Desktop/learning/mica_agent run pytest tests/test_app.py tests/test_tool_session_integration.py tests/test_tui_main_buffer_screen.py -q -W error --tb=line -k 'reasoning_then_failure_does_not_commit_partial_turn or follow_up_run_sees_checkpoint_context or cancelled_checkpoint_restores_under_a_new_model or running_token_cancellation_restores_partial_without_forged_assistant or permission_wait_cancel_commits_unstarted_tool_checkpoint or ask_wait_cancel_commits_in_progress_tool_checkpoint or failed_run_does_not_break_following_successful_run or tool_batch_cancel_preserves_all_facts_without_reissuing_calls or escape_cancels_active_run_without_committing_turn'` → `9 passed, 130 deselected in 0.95s`。未重复全量。

## 发现与判断

- 七项 App 测试精确比较 USER partial/终态/工具事实和后续请求顺序，固定 SYSTEM 头独立保留；外部 task.cancel、运行 token 取消、Permission handler 未执行、Ask 等待中断、无成功 Turn、原 partial 与恢复后正常保存的保护均保留，没有只修首个旧断言。
- 工具集成保留真实 App/Engine/ToolExecutor 和 A 完成后取消；完整核对 A/B 原调用、参数、A 原真实结果、B NOT_STARTED/None，以及整批 USER JSON。ToolStarted 仅 A；A SUCCESS、B CANCELLED 终态事件与 B 无保存结果分别断言，没有将取消通知伪造成 TOOL 结果。
- TUI 保留真实键盘输入、等待运行开始、Escape 取消和退出流程；历史完整相等，无成功 Turn，保存 CANCELLED 和原 partial，草稿 x、idle/stop 与 frame stop 均保留。
- 报告所述首次新增事件预期失败已定位为误漏 B 的取消终态事件；最终断言精确匹配实际事件列表，没有吞错、放宽匹配、删测试或改变产品取消语义。未发现阻止本卡交付的问题。

## 结论

第二轮通过，T-032 已完成。第一轮调用侧测试范围遗漏已解决；统一消息与续接恢复投影及相关自动化可以验收，第一轮受阻报告与审查保留为历史证据。

本结论不代表 B 第 6 步整体或 Phase 6 完成。配置接口与能力、App/CLI 续接装配及集成验收仍留后续卡；三类接口真实渠道、跨接口切换和真实 Windows Terminal 验收本轮未执行，不能由自动化替代。远端同步在验收提交后按长期授权执行并单独核对。
