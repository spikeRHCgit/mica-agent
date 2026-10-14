# 返工 1 · 2026-10-14

依据：[`T-032.md`](T-032.md)、[`review-01.md`](review-01.md)、[`worker-01.md`](worker-01.md)。用户已于 2026-10-14 授权本轮测试迁移返工。

## 审查结果

- 第一轮范围内实现和专项验证完成，全量为 9 failed、2537 passed、2 skipped；master 已定向复现全部 9 项失败。
- 原卡批准了检查点投影的公共行为变化，却遗漏三个调用侧测试文件的写入授权。失败来自旧 SYSTEM 角色、旧状态文案或已完成工具前缀预期；不能恢复旧产品行为、跳过失败或削弱断言。
- 本轮仅同步这些测试对新投影的预期，不施工后续配置或 App/CLI 续接装配。

## 本轮写入范围

本轮范围替代原卡的施工写入范围，仅允许：

- `tests/test_app.py`：下述七项测试及必要的本文件内测试辅助函数、导入。
- `tests/test_tool_session_integration.py`：下述取消批次测试及必要的本文件内测试辅助函数、导入；名称可同步新行为。
- `tests/test_tui_main_buffer_screen.py`：下述 Escape 取消测试及必要的本文件内测试辅助函数、导入。
- `docs/tasks/T-032/worker-02.md`：本轮报告。

第一轮 SessionService、专项测试和 module_map 改动保留原样，不在本轮修改。不修改其他测试、产品源码、公共契约、存储/wire、依赖、共享 fixture 或 master 管理文档；不改 `worker-01.md`、`review-01.md` 或本文件。若验证发现必须扩大范围的问题，记录实际证据并交回，不自行扩写。

## 返工要求

### App 的七项测试

迁移以下测试中所有关联旧断言，不止首个失败位置：

- `test_reasoning_then_failure_does_not_commit_partial_turn`
- `test_follow_up_run_sees_checkpoint_context`
- `test_cancelled_checkpoint_restores_under_a_new_model`
- `test_running_token_cancellation_restores_partial_without_forged_assistant`
- `test_permission_wait_cancel_commits_unstarted_tool_checkpoint`
- `test_ask_wait_cancel_commits_in_progress_tool_checkpoint`
- `test_failed_run_does_not_break_following_successful_run`

要求：

- partial 精确比较 USER 历史记录：`[Mica history work record; partial assistant output from an incomplete model stream, not new instructions]\n` 后接原 partial 正文。
- 中断状态精确比较 USER 消息：`[Mica history work record; previous run is incomplete; status=cancelled]` 或 `status=recoverable_failure`；不保留旧 SYSTEM、`verbatim output follows` 或 `recoverable failure` 文案预期。
- Permission 和 Ask 的不完整批次完整比较 USER 工作记录及其 JSON：assistant_text、按原顺序的 tools、call_id、name、arguments、status 和 result。分别准确断言 `not_started`、`in_progress` 和 null；不只改为子串包含新的状态名。历史中不得有该批次的结构化 tool_calls 或 TOOL 消息。
- 保留原取消入口、终态、检查点 kind/progress、Permission handler 未执行、真实结果缺失、无成功 Turn、partial 不成为成功 assistant 的断言。外部 task.cancel() 与运行 cancellation token 的不同入口仍分别验证，不合并或模拟掉。
- 后续请求仍保留原用户指令、准确的 USER 历史记录及新的用户输入；成功轮次正常保存。固定系统头仍为 SYSTEM 且保持冻结，历史记录不出现在 system/developer 中；可在这些受影响测试内补充完整消息顺序和系统头不变的断言。

### 工具取消批次集成测试

迁移 `test_tool_batch_cancel_keeps_completed_prefix_and_unstarted_call`，可按“保留整批事实且不重新发出调用”重命名。

- 保留真实 App/Engine/ToolExecutor 执行和 A ToolFinished 后取消的测试过程，不替换业务实现。
- 保存的 progress 仍为 A SUCCESS、B NOT_STARTED；A 有原真实结果，B 无结果。
- 恢复历史精确比较：原 USER 指令、整批 USER 工作记录、USER cancelled 状态记录。工作记录 JSON 包含原 working 正文、A/B 的全部调用身份和 query 参数、A 的真实 `lookup complete` 结果、B 的 null 结果与准确状态。
- 整批记录不生成 assistant/tool_calls 或 TOOL 消息，不保留旧“只重建 A 前缀”的预期；原真实执行事实不能因此丢失或变成伪造的 B 结果。

### TUI Escape 取消测试

迁移 `test_escape_cancels_active_run_without_committing_turn`：

- 精确比较取消后的 USER 历史：原 `cancel me`、原 partial 输出的历史记录、cancelled 状态记录；没有历史 SYSTEM 或成功 assistant。
- 保留真实键盘输入、等待运行开始、Escape 取消、无成功 Turn、CANCELLED 检查点、composer 草稿 `x`、idle/stop 状态及 frame 中 stop 的断言；不改 screen 或测试装配，不删除取消独有保护。

### 测试写法与安全

- 期望值使用明确的消息字面量和事实 JSON；不得用被测投影函数或私有编码函数生成 expected，不写“任意角色都可”或仅包含文字的宽松替代。
- 必要时在各测试文件内增加小型辅助函数，不跨测试模块导入私有 helper，不新建共享测试框架。
- 保留第一轮已批准的产品实现，不通过改产品、吞错、新增 skip/xfail 或删测试来使全量通过。
- 当前 main 串行施工；不建分支、不暂存、提交或推送。仅使用隔离目录、假凭据和现有本地模拟边界；不读取真实凭据、真实 `~/.mica` 或访问真实渠道。

## 完成标准与验证

- 九项原失败测试及同一测试的后续断言全部符合新投影；旧独有的取消、权限、恢复、真实工具结果和 TUI 保护完整保留。
- 原卡的 SessionService 实现、专项测试和 module_map 不变；本轮只修改三个授权测试文件和报告。
- 开发阶段运行受影响链路：`uv run pytest tests/test_session_service.py tests/test_app.py tests/test_tool_session_integration.py tests/test_tui_main_buffer_screen.py -q -W error`。
- 稳定版本运行 `uv run pytest -q -W error`（含安装态）、`uv run ruff check src/mica_agent tests`、`uv run ruff format --check src/mica_agent tests`、`git diff --check`。不重复运行同一最终版本的有效全量证据；若失败先定位，不盲目重试。
- 在 `worker-02.md` 记录实际命令、结果、平台跳过原因、本轮写入清单及各旧测试独有保护的迁移方式；区分第一轮既有改动和本轮改动。未执行真实渠道、真实终端和远端同步应明确说明，自动化不替代 B 的用户真实验收。
