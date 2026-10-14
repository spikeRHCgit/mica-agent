# 第 2 轮汇报 · 2026-10-14

依据：[`T-032.md`](T-032.md)、[`rework-01-request.md`](rework-01-request.md)。

- 结果：完成 —— 九项调用侧测试已迁移为精确 USER 历史断言；相关链路 194 passed，全量 2546 passed、2 项既有平台跳过，静态检查通过。
- 改动：仅三个授权测试文件及本报告；第一轮实现与文档保留。
- 验证：最终相关链路 `194 passed in 41.40s`；最终全量 `2546 passed, 2 skipped in 193.52s`（含安装态）；Ruff、格式与 diff 检查通过。
- 偏离与疑点：无。
- 建议：无。

### 明细

- [x] 七项 App 测试的完整 USER 历史、后续恢复及固定 SYSTEM 头；保留不同取消入口、Permission/Ask 执行边界和检查点事实。
- [x] 工具取消批次精确比较整批 JSON、A 真实结果、B null；保留真实执行和取消事件。
- [x] TUI Escape 历史精确比较；保留键盘、运行开始、草稿 x、idle/stop、frame 和退出保护。
- [x] 相关链路验证及最终全量（含安装态）、Ruff、格式和 diff 检查。
- [x] 审查 diff、原保护迁移及范围核对；第一轮和 master 文件字节不变，main 暂存区为空。
- [x] 记录平台跳过、隔离边界及未执行的真实渠道/终端/远端验证。

### 本轮写入与边界

本轮只修改 `tests/test_app.py`、`tests/test_tool_session_integration.py`、`tests/test_tui_main_buffer_screen.py` 和本报告。第一轮留下的 SessionService、专项测试和 module_map 三项既有改动保留；开工时未发现三个本轮测试文件的既有改动。

已通读本轮三个测试文件的完整 diff。另以 AST 比较核对：App 仅七个授权测试和三个新增文件内历史消息 helper 改变；工具集成仅一个授权测试重命名并迁移；TUI 仅一个授权测试改变。fixture、Provider、ToolExecutor、键盘输入及 ScreenHarness 装配没有修改，未新增 skip/xfail、删除用例或跨测试模块导入 helper。所有 expected 来自明确消息标签及事实 JSON，不调用被测投影或私有编码函数。

开工/收尾 SHA-256 对比确认以下七个文件原样保留：`src/mica_agent/session_service.py`、`tests/test_session_service.py`、`docs/module_map.md`、`worker-01.md`、任务卡、返工要求和第一轮审查报告。仍在 main，暂存区为空；未建分支、暂存、提交或推送，没有修改 master 管理文件。

### 独有保护的迁移

| 原测试 | 本轮精确断言与保留的独有保护 |
|---|---|
| `test_reasoning_then_failure_does_not_commit_partial_turn` | 原用户指令及 USER `recoverable_failure` 状态记录完整相等；保留 ReasoningDelta、RunFailed、无成功 Turn、检查点 kind 及无 partial。历史也不能混入专属思考。 |
| `test_follow_up_run_sees_checkpoint_context` | 原用户、原 partial 的 USER 标签记录、USER 中断状态完整相等；后续请求精确为原 SYSTEM 头、上述历史、新输入。保留失败检查点及 partial，成功恢复后保留一条检查点和一轮正常 Turn，并核对新用户与完整回答。 |
| `test_cancelled_checkpoint_restores_under_a_new_model` | 保留外部 task.cancel()、CancelledError 和 RunCancelled；精确 USER 历史。检查点仍 CANCELLED、无成功 Turn、保存原 partial；新模型请求精确含固定 SYSTEM 头、完整历史和新输入，partial 不成为 assistant；成功恢复正常保存一轮且旧检查点不变。 |
| `test_running_token_cancellation_restores_partial_without_forged_assistant` | 保留运行 CancellationToken 的独立取消入口及实际完成等待；无成功 Turn、CANCELLED 和原 partial 均保留。进程内历史、恢复请求完整相等，固定 SYSTEM 头不夹带历史；partial 不成为 assistant，恢复成功后旧检查点不变且只保存正常新 Turn。 |
| `test_permission_wait_cancel_commits_unstarted_tool_checkpoint` | 保留 PermissionRequested 时取消、handler 调用次数为 0、RunCancelled、NOT_STARTED、结果 None、无成功 Turn。完整比较 USER 记录及 JSON：空 assistant_text、原 write 身份和空参数、`not_started`、null；无结构化 tool_calls/tool_call_id 或 TOOL。 |
| `test_ask_wait_cancel_commits_in_progress_tool_checkpoint` | 保留 UserInputRequested 时取消、实际 broker 等待和 RunCancelled；补齐 CANCELLED、无成功 Turn。原 IN_PROGRESS、结果 None 保留；完整比较原 ask 身份/问题参数、`in_progress`、null 的 USER JSON；无结构化调用或 TOOL。 |
| `test_tool_batch_cancel_keeps_completed_prefix_and_unstarted_call` → `test_tool_batch_cancel_preserves_all_facts_without_reissuing_calls` | 真实 App/Engine/ToolExecutor 和 A ToolFinished 后取消过程不变。A/B 原身份与 query 参数、A SUCCESS/B NOT_STARTED、A 原 `lookup complete` TOOL 结果和 B None 精确核对。恢复三条 USER 消息完整相等，整批 JSON 保留 working、真实 A 结果和 B null，无 assistant/tool_calls/TOOL。只有 A ToolStarted；终态事件精确为 A SUCCESS/B CANCELLED，取消通知不成为保存结果。 |
| `test_failed_run_does_not_break_following_successful_run` | 后续请求精确包含原用户、USER recoverable_failure、新用户；SYSTEM 头与首次请求完全相同。补强失败/成功终态、恰一检查点和一成功 Turn；保留旧 kind 和成功回答，并核对新用户。 |
| `test_escape_cancels_active_run_without_committing_turn` | 原键盘输入、等待 Provider 开始、输入草稿 x、Escape 取消和 Ctrl+C 退出链路不变。历史精确为原用户、原 partial 的 USER 工作记录、USER cancelled，无历史 SYSTEM 或成功 assistant；保留无成功 Turn、CANCELLED、草稿 x、idle/stop 和 frame stop，补核对保存 partial。 |

### 验证过程与结果

| 实际命令 | 本轮结果 |
|---|---|
| `uv run ruff format tests/test_app.py tests/test_tool_session_integration.py tests/test_tui_main_buffer_screen.py` | 2 files reformatted, 1 file left unchanged；只涉及授权文件 |
| `uv run pytest tests/test_session_service.py tests/test_app.py tests/test_tool_session_integration.py tests/test_tui_main_buffer_screen.py -q -W error` | 首次 1 failed、193 passed in 43.47s；定位并修正新增事件预期后，最终 **194 passed in 41.40s**，无跳过 |
| `uv run pytest tests/test_tool_session_integration.py::test_tool_batch_cancel_preserves_all_facts_without_reissuing_calls -q -W error` | 定向验证 **1 passed in 0.22s** |
| `uv run pytest -q -W error` | 最终稳定版本 **2546 passed, 2 skipped in 193.52s**；含安装态套件，无失败 |
| `uv run ruff check src/mica_agent tests` | All checks passed! |
| `uv run ruff format --check src/mica_agent tests` | 116 files already formatted |
| `git diff --check` | 通过，无输出 |
| `uv run pytest tests/test_tool_presentation.py::test_workspace_reparse_point_is_not_retained_in_presentation tests/test_workspace_paths.py::test_resolver_rejects_existing_symbolic_link_in_path -q -W error -rs` | **2 skipped in 0.37s**；两项实际创建符号链接均失败于 WinError 1314（缺少权限），没有新增跳过 |

首次链路失败来自本轮新增的 ToolFinished 断言误认为只应出现 A。实际 Engine 会转发 B 的取消终态，遇到 CANCELLED 结果后直接保存进度并终止，不生成 B 的 TOOL 消息；ToolStarted 仍仅 A，检查点 B 仍 NOT_STARTED/None。只读核对 Engine 的 finished/result 分支后，将断言修正为精确的 A SUCCESS、B CANCELLED 列表；没有改产品行为、放宽匹配或删除负向保护。定向及完整相关链路均已通过。

本轮沿用隔离临时目录、固定 Provider 脚本、现有本地模拟边界；没有读取真实凭据、真实用户 `~/.mica` 或访问真实渠道。未执行真实三类接口验收、真实 Windows Terminal 验收和远端同步，自动化不能替代 B 的用户真实验收。
