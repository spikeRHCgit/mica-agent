# 第 1 轮汇报 · 2026-10-14

依据：[`T-032.md`](T-032.md)。

- 结果：受阻 —— 范围内实现及专项验证完成；全量有 9 项范围外旧投影断言失败，须由 master 处理测试写入范围后完成验收。
- 改动：`src/mica_agent/session_service.py`、`tests/test_session_service.py`、`docs/module_map.md`、本报告；没有其他源码或文档改动。
- 验证：专项 `55 passed`；全量 `9 failed, 2537 passed, 2 skipped`；Ruff、格式和 diff 检查通过。详见下文。
- 偏离与疑点：任务已批准旧入口行为变更，但允许写入的测试只有 `test_session_service.py`，9 项范围外旧断言未获同步授权；未越界改动、跳过失败或恢复旧安全语义。
- 建议：master 授权后迁移下表 9 项断言，保留其取消、权限、会话恢复和 TUI 独有保护；之后重跑全量。App/CLI 续接装配与真实渠道验收仍归后续任务。

### 明细

- [x] 统一快照一次读取：新增模块内 `SessionContextSnapshot`（frozen、slots、repr=False）及 `model_context_snapshot`；旧入口仅委托返回 `.messages`。`test_model_context_snapshot_loads_once_and_old_entry_delegates` 核对实际读取次数和委托；空快照、不可变性及 repr 测试通过，原模块依赖回归保留并通过。
- [x] 时间线及来源：`test_mixed_timeline_preserves_sources_statuses_thinking_and_lifecycle` 完整比较多 Turn/checkpoint 快照，混合三类接口、不同供应商/模型、None/REPLAYABLE/UNREPLAYABLE；保存选择与记录来源不同仍全部保留。每个 assistant 精确一个槽位，并复用 `check_step_association`。两轮各自合规但全会话超过 1 MiB 的样本仍保留两条原续接。
- [x] 完整步骤：三类 API × 有/无正文的完整双工具批次测试保留全部原调用、SUCCESS/FAILED 真实结果及续接；之前的空进度不占槽，后续无续接步骤占 None 槽。纯思考且无 assistant_message 时补空正文 assistant；已有空正文 assistant 也保留；无消息且无续接的步骤跳过。
- [x] 不完整批次：成功+未开始、失败+进行中、进行中+未开始、全未开始 × 有/无正文，完整比较 USER 记录及 JSON。保留调用顺序、不同工具名、参数（含嵌套 JSON）、真实结果及 null；整个批次没有结构化 tool_calls/TOOL 或续接槽，后续 Turn 对齐不变。原会话字节和保存续接不变，不包含 presentation 或专属思考/signature/data。
- [x] 部分输出及终态：固定标签标明未完成模型流；CANCELLED/RECOVERABLE_FAILURE 状态均作为 USER 历史记录，不附加执行旧工具或宣称成功的指令。无进度、无 partial、空 partial 均完整比较；绝对路径清洗、正常 URL、用户正文及空正文既有保护保留。缺失/损坏文件和关联错误仍安全报错，不宽松解析或改写文件。
- [x] 旧格式与生命周期：手写实际 v1～v5 wire 字段的固定样本，不调用新 serializer 改版本号；逐版本完整比较快照与 None 槽，恢复前后字节及版本不变。v6 混排与完整/不完整批次恢复同样只读；branch/rewind/clear 的剩余时间线及续接精确一致，源会话字节不变。
- [x] 文档与旧测试保护：module_map 同步上述职责；被替代旧测试独有保护迁移见下文。旧 SYSTEM 和前缀投影私有路径已删除，没有保留第二套旧逻辑。
- [x] 检查执行及范围：已审查完整 diff 和新报告；全量（含安装态）执行完毕，专项及静态检查通过，平台跳过原因已实测确认。当前仍为 main，暂存区为空；未建分支、暂存、提交或推送。仅写允许的四个文件。
- [ ] 全量绿色验收：9 项范围外断言阻塞；未将本轮标为完成。

### 实际实现

一次 `store.load` 得到 entries 后，正常 Turn 直接拼接 `context_messages` 和规范化的 `continuations`；检查点在同一条投影路径中生成两组数据，最后逐 assistant 使用既有关联校验核对非空记录。没有接口过滤、专属字段清洗/重写或全会话限额；没有顶层包导出、存储/wire/公共持久化契约或运行时依赖变更。

完整工具批次按全部原调用重建 assistant，紧接全部原结果，并携带该步原续接；不根据结果正文猜 SUCCESS/FAILED。不完整批次整体成为一条 USER 历史记录，以公开 `to_mutable_json` 转换参数，紧凑、排序键的确定性 JSON 保留真实事实。partial 和中断状态分别作为 USER 记录。恢复流程不执行工具、不修改源文件。

### 验证记录

| 实际命令 | 结果 |
|---|---|
| `uv run ruff format src/mica_agent/session_service.py tests/test_session_service.py` | 2 个允许范围内文件格式化 |
| `uv run pytest tests/test_session_service.py -q -W error` | 首次 `55 passed in 3.45s`；样本补强后 `55 passed in 2.80s`，无跳过 |
| `uv run pytest -q -W error` | `9 failed, 2537 passed, 2 skipped in 156.46s`；安装态套件包含在此执行中，未报告安装态失败 |
| `uv run ruff check src/mica_agent tests` | `All checks passed!` |
| `uv run ruff format --check src/mica_agent tests` | `116 files already formatted` |
| `git diff --check` | 通过，无输出 |
| `uv run pytest tests/test_tool_presentation.py::test_workspace_reparse_point_is_not_retained_in_presentation tests/test_workspace_paths.py::test_resolver_rejects_existing_symbolic_link_in_path -q -W error -rs` | `2 skipped in 0.14s`；两项均实测为 Windows 创建符号链接缺少权限（WinError 1314），没有新增或放宽 skip |
| `git status --short`、`git diff --name-only`、`git diff --cached --stat`、`git branch --show-current` | 仅允许的四个文件；暂存区为空；main |

全量启动后，唯一进一步调整为专项测试样本中的第二工具恢复为原保护中的 `write`（含 content 参数），以保持不同工具名的独有保护；产品实现未再改变。该调整后专项、全仓 Ruff/格式及 diff 检查已重新通过。未机械重复已有 9 项范围外失败的全量执行。

测试使用 tmp_path 隔离目录和固定样本，没有读取真实凭据、真实用户 `~/.mica` 或访问真实渠道；全量安装态沿用已有隔离 HOME、假凭据和本地 loopback 测试边界。未执行三类接口真实渠道验收、真实 Windows Terminal 验收或远端同步；这些不属于本卡交付，自动化结果不替代 B 的真实渠道验收。

### 范围外阻塞与交回项

以下均为本次实际失败位置；相关文件没有改动。批准的新投影在失败输出中实际表现为 USER 记录及准确 JSON 状态，旧断言仍要求 SYSTEM、旧文字或已完成前缀 assistant。

| 文件与失败位置 | 测试 | 冲突 |
|---|---|---|
| `tests/test_app.py:344` | `test_reasoning_then_failure_does_not_commit_partial_turn` | 中断状态要求 SYSTEM |
| `tests/test_app.py:635` | `test_follow_up_run_sees_checkpoint_context` | partial 要求 SYSTEM |
| `tests/test_app.py:1095` | `test_cancelled_checkpoint_restores_under_a_new_model` | partial 要求 SYSTEM |
| `tests/test_app.py:1188` | `test_running_token_cancellation_restores_partial_without_forged_assistant` | 恢复 partial 要求 SYSTEM |
| `tests/test_app.py:1242` | `test_permission_wait_cancel_commits_unstarted_tool_checkpoint` | 要求旧文案 `was not started`，新 JSON 为 `not_started` |
| `tests/test_app.py:1293` | `test_ask_wait_cancel_commits_in_progress_tool_checkpoint` | 要求旧文案 `did not complete`，新 JSON 为 `in_progress` |
| `tests/test_app.py:1345` | `test_failed_run_does_not_break_following_successful_run` | 后续请求的中断历史要求 SYSTEM |
| `tests/test_tool_session_integration.py:122` | `test_tool_batch_cancel_keeps_completed_prefix_and_unstarted_call` | 要求截取已完成前缀重建 assistant；新要求整批 USER |
| `tests/test_tui_main_buffer_screen.py:538` | `test_escape_cancels_active_run_without_committing_turn` | TUI 取消后中断状态要求 SYSTEM |

需要 master 决定追加这三个测试文件的迁移写入范围或另派任务；不得删除其取消入口、工具真实执行边界、固定系统头、后续请求恢复及 UI 草稿/终态等独有断言。没有发现必须改 App、Engine、适配器或持久化契约才能实现本卡的产品缺陷。

### 被替代检查点测试的独有保护

| 原测试 | 保留方式 |
|---|---|
| `test_checkpoint_timeline_context_and_lifecycle_operations` | 原有 entries/turns/checkpoints 时间线及 lifecycle 断言保留；旧局部角色/包含文字断言改为完整快照精确相等，另核对 branch、rewind、clear 后消息与 None 槽位，以及分支不受源 clear 影响。 |
| `test_tool_batch_context_keeps_a_and_does_not_fabricate_b` | 替换为 `test_incomplete_tool_batch_preserves_all_facts_without_reissuing_calls` 参数化场景。保留成功 read A 的真实结果、未开始 write B 的全部身份/参数/缺失结果与 recoverable failure；完整比较全部 JSON。增加真实失败、进行中、全未开始、空正文、嵌套参数、原续接不改及后续槽位对齐，禁止工具结构化请求和虚构结果。 |
| `test_context_snapshot_redacts_checkpoint_absolute_paths` | 原有持久化无绝对路径、合法 URL 保留、用户文本不改、恢复参数与结果精确清洗断言全部保留；partial 和终态改为精确 USER 记录，增加无 SYSTEM 和快照槽位检查。 |
| `test_only_checkpoint_session_is_healthy_and_activity_uses_checkpoint`、模块依赖、读取错误与其他生命周期测试 | 原保护未删除；专项通过。新增安全错误测试还覆盖两个快照入口拒绝损坏及错误关联数据，且源文件字节不变。 |
