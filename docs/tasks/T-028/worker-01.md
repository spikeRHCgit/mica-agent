# 第 1 轮汇报 · 2026-10-11

依据：[`T-028.md`](T-028.md)。

- 结果：完成 —— v6 会话契约、严格兼容读取及原子升级验证完成，全量 2091 项通过、2 项平台权限跳过，改动均在允许范围内。
- 改动：`src/mica_agent/session_contracts.py`、`checkpoint_contracts.py`、`continuation_contracts.py`；`tests/test_session_contracts.py`、`test_jsonl_store.py`；其余六个获准测试文件仅更新当前版本断言；本汇报。
- 验证：`uv run pytest -q -W error` → `2091 passed, 2 skipped in 200.53s (0:03:20)`，退出码 0；`uv run ruff check src/mica_agent tests` → `All checks passed!`，退出码 0；`uv run ruff format --check src/mica_agent tests` → `112 files already formatted`，退出码 0。局部验证 `uv run --offline pytest tests/test_session_contracts.py tests/test_jsonl_store.py -q -W error` → `202 passed in 1.48s`。
- 偏离与疑点：用户明确授权直接在 main 工作区施工；不进行 Git 写操作。契约卡指定非 tuple 的 `continuations` 抛 `ValueError`，与 Ruff TRY004 默认类型错误建议冲突；遵守已确认契约，仅该行使用 `noqa: TRY004` 并解释原因。两个契约测试的未知版本样本从 6 改为 7，保留未知版本拒绝保护。
- 建议：无。

### 明细

- [x] 核对版本常量引用方和禁止范围，无需越界：`jsonl_store.py` 的 create、append、update/rewrite 及 `session_service.py` 的元数据写入均引用 `CURRENT_SESSION_FORMAT_VERSION`；这些文件未改。
- [x] 实现 v6 轮次和检查点续接的清洗、关联、上限与严格 wire：直接调用 `sanitize_step_continuation`、`check_step_association`、`continuation_size`、`parse_continuation`、`serialize_continuation`；`ContinuationFormatError` 属于 `ValueError`，由现有边界转为无原值的 `SessionFormatError`。未重写续接解析或关联规则；`_freeze_sequence` 仅补返回注解及所需 `Any` 导入。
- [x] v6 规范字节样本和对象往返相等：`test_v6_tool_turn_and_checkpoint_have_exact_canonical_wire_bytes` 对完整文件逐字节比较；包含两步工具往返、Responses reasoning/text/tool_call、一个 null、最终回答续接及第一步含 Messages 续接的检查点。
- [x] v5 固定样本只读不变、业务写入升级、替换失败与重试：`write_v5_fixture` 直接写字面量旧 wire，含 selection、工具轮次、工具检查点及部分回答；`test_fixed_v5_load_preserves_bytes_and_business_upgrade_can_retry` 的 turn/checkpoint × 正常/替换失败四种情况全部通过。
- [x] 非法 wire 与构造拒绝且错误信息不泄漏续接：覆盖缺少字段、数量或容器错误、未知项、多余字段、引用顺序、正文引用缺失、单记录超限、多个记录合计超限；旧 turn/progress 加新字段仍拒绝。另验证 UTF-8 合计、恰好 1 MiB 接受及多一字节拒绝。
- [x] 本地绝对路径降级与 repr 隐藏续接：Messages thinking、Responses summary 的 Windows/POSIX 路径均使整条续接不可回放；Turn 在中间消息清洗前判定原始工具参数。三种对象 repr 均隐藏思考、签名、密文及来源标识。docstring 明确 Engine 负责原始消息判定，契约仅兜底。
- [x] 旧版本读取保持严格，v1～v4 固定样本不变；其他测试仅更新当前版本断言：v3～v5 turn 与 v4/v5 checkpoint 的字段集合不放宽；脚本比较 HEAD 与最终内容确认 v1/v3 fixture AST 和 v2/v4 固定字面量不变，六个受限测试文件只有获准版本断言改动。既有旧版本读入版本号断言保留。
- [x] 最终全量 pytest、Ruff 检查、格式检查通过，列出平台跳过项：上述命令均退出 0；全量在隔离的临时 HOME/USERPROFILE 中运行，去除继承的 `MICA_*`、`OPENAI_API_KEY`、`ANTHROPIC_API_KEY` 环境变量，设置 `UV_OFFLINE=1` 与 `PYTEST_ADDOPTS=-ra`。测试使用临时资源、假凭据及现有本机回环服务，没有读取真实 `~/.mica` 或凭据、访问外部网络；分发测试的构建和安装均为离线。
- [x] 审查最终 diff 与写入范围：已通读产品 diff 和测试变更，`git diff --check` 通过；`git status --short` 仅列允许的 11 个源码/测试文件和本汇报。

### 平台跳过项

本轮没有新增 skip，也没有修改既有跳过条件；`tests/test_distribution.py` 没有跳过项。全量实际跳过以下两项：

- `tests/test_tool_presentation.py::test_workspace_reparse_point_is_not_retained_in_presentation`（第 273 行）：创建符号链接失败，`WinError 1314`，当前 Windows 用户缺少所需特权。
- `tests/test_workspace_paths.py::test_resolver_rejects_existing_symbolic_link_in_path`（第 69 行）：创建符号链接失败，`WinError 1314`，当前 Windows 用户缺少所需特权。

### 交付边界

本卡仅交付持久化契约；Engine 原始消息清洗判定、续接产生与使用仍由后续卡片接入。没有新依赖、公共导出变更或禁止文件改动；没有暂存、提交、建分支、切换分支或推送。工作区仍为 `main`。
