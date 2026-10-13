# 返工 1 · 2026-10-13

依据：[`T-030.md`](T-030.md)、[`review-01.md`](review-01.md)。

## 审查结果

- 问题：没有实际续接内容、也没有发生降级的正常完成步骤被错误标记为 `UNREPLAYABLE`，而非 `None`。
- 证据：`src/mica_agent/providers/openai_responses.py:516` 的 `_ResponseStep.record()` 在空 items 且未 discarded 时落入不可回放分支；`tests/test_openai_responses_provider.py:342` 和 `:856` 的空步骤预期固化了该错误。
- 契约依据：`docs/phase6_plan.md`「续接与 v6 wire」规定不产出续接的步骤记为空；`unreplayable` 只表示这一步有过续接但不能回放。

## 返工要求

- 修复 `_ResponseStep.record()`：已知 provider_id、未发生降级且最终 items 为空时返回 `None`；非空且可忠实回放的 items 返回 `REPLAYABLE`；真实降级返回 items 为空的 `UNREPLAYABLE`。未知 provider_id 仍返回 `None`。
- 保留 `_items()` 解析异常所触发的降级，以及未知语义字段、未知 phase、缺失有效密文、refusal、无法忠实表示的多正文等既有降级条件。不得把所有空 items 统一改为 `None`，以免抹掉真实降级。
- 保留 `check_budget()` 的 provisional 检查、精确 wire 字节计量和超限后的 sticky 状态；超限后仍释放专属内容，后续 delta 或 completed 快照不能恢复为可回放，也不能变成 `None`。
- 逐样本修正 `test_complete_snapshot_only_and_empty_steps`：空 output、空正文且无其他续接内容和降级时断言 `continuation is None`；有效 reasoning、非空正文等样本断言 `REPLAYABLE` 和具体 items。核对有效工具调用引用的既有测试，补足必要的状态回归，不用宽泛的非空断言代替语义断言。
- 修正 `test_multiline_sse_comments_and_event_type_matching`：该空 output 样本应得到 continuation 为 `None` 的 Finish，保留多行 data、注释、event/type 一致性和终态后停止消费的断言。
- 检查其余 `UNREPLAYABLE` 断言的场景，保留真实降级的预期。不得统一放宽或批量替换断言。第一轮已修复的 reasoning_text 包装字段识别、URL userinfo 鉴权保护不应回退。

## 写入范围

- 沿用原卡的源码、测试及 `docs/module_map.md` 范围；module_map 仅在登记确有必要时修改，不扩大实现范围。
- 本轮汇报授权为 `docs/tasks/T-030/worker-02.md`，不再修改 `worker-01.md`。
- 不修改任务卡状态、审查或返工要求、STATUS、已有 Provider/Engine/App、续接契约、公共导出和依赖。
- 继续使用当前 main 工作区串行施工，不另建分支，不进行 Git 暂存、提交或推送；不要改动 master 的返工管理文档。
- 测试只用 fake key、替身传输或本机模拟服务，不读取真实凭据、真实 `~/.mica` 或请求真实模型渠道。

## 返工完成标准

- 正常 completed 的空 output 和无其他续接内容的空正文均 Finish 且 continuation 为 `None`；未知来源仍为 `None`。
- 非空正文、有效 reasoning 和有效工具调用仍返回绑定正确、顺序及内容正确的 `REPLAYABLE` 记录。
- 真实降级仍返回 items 为空的 `UNREPLAYABLE`；1 MiB 边界和超出 1 字节、超限后释放及不能恢复的保护保持通过。
- 运行原卡指定验证：`uv run pytest tests/test_openai_responses_provider.py tests/test_provider_contract.py tests/test_engine.py -q -W error`；稳定版本运行 `uv run pytest -q -W error`（含安装态验证）、`uv run ruff check src/mica_agent tests`、`uv run ruff format --check src/mica_agent tests`、`git diff --check`。
- 在 `worker-02.md` 记录修复位置、逐场景预期、命令和实际结果、平台跳过原因；状态核对时区分已存在的 master 管理文档与本轮 worker 改动。不宣称真实渠道或用户终端验收。
