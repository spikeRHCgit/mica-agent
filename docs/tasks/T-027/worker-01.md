# 第 1 轮汇报 · 2026-10-11

依据：[`T-027.md`](T-027.md)。

- 结果：完成 —— 独立续接契约、严格 wire、关联与体量校验、清洗判定及测试已完成，指定验证通过。
- 改动：新建 `src/mica_agent/continuation_contracts.py` 和 `tests/test_continuation_contracts.py`；在 `message_sanitizer.py` 新增并导出 `sanitize_step_continuation`；在 `module_map.md` 公共契约组登记新文件；本文件记录施工证据。
- 验证：
  - `uv run pytest tests/test_continuation_contracts.py tests/test_session_contracts.py tests/test_tool_presentation.py -q -W error` → `271 passed, 1 skipped in 0.63s`，退出码 0；其中新测试 126 项全部通过。
  - `uv run ruff check src/mica_agent tests` → `All checks passed!`，退出码 0。
  - `uv run ruff format --check src/mica_agent tests` → `112 files already formatted`，退出码 0。
  - 为确认既有跳过项原因，补跑 `uv run pytest tests/test_tool_presentation.py -q -W error -rs` → `26 passed, 1 skipped in 2.42s`；跳过的是既有 `test_workspace_reparse_point_is_not_retained_in_presentation`，Windows 创建符号链接时报 `WinError 1314`（所需权限不可用），测试及跳过条件均未修改。
  - `git diff --check` → 通过；通读已跟踪 diff 和两份新建源码/测试。使用 `uv run python` 对比 HEAD 与当前清洗模块 AST，11 个既有函数完全一致；逐条核对 Git 状态，仅出现五个允许文件。
- 偏离与疑点：
  - 用户转交文字中的 `/Users/richard/Desktop/learning/mica_agent` 在本机不存在；使用会话环境指定的 `C:\Users\10601\Desktop\learning\mica_agent`。当前分支为 main，开工时工作区干净、领先 origin/main 三个提交。无 Git 写操作。
  - 关联检查始终先验证 assistant 角色；Completions 不使用引用项，按设计跳过引用匹配；不可回放记录仅含空项标记，亦跳过引用匹配。正常可回放 Responses/Messages 记录严格核对工具顺序/集合及正文存在性。
  - 本卡按指定的隔离测试范围验证，未运行含本地网络服务器的安装态全仓测试；未读取真实 `~/.mica` 或凭据，未访问网络，未新增依赖。仅提供独立契约与判定能力，未接入会话格式、Provider、Engine、App 或顶层公共导出。
- 建议：无。

### 明细

- [x] 六种项及 ReasoningDetail：`test_fixed_wire_shapes_and_round_trips` 用三份固定 JSON 字面量逐字段核对六种项和三种 detail；各接口不可回放空数组、nullable 字段及全部 phase 另有覆盖。
- [x] 严格拒绝多余/缺失字段及未知 type、api、status、phase：`test_parse_rejects_invalid_wire_without_echoing_values` 逐层变异；`test_parse_requires_every_field_at_every_level` 删除记录、每种项和每种 detail 的每个必需字段；非对象输入另测。
- [x] 接口白名单、Messages phase、状态、Chat 项数及非空内容校验：构造与解析负向测试覆盖；空明文、details-only Chat 合法路径另测。
- [x] 正文项唯一、call_id 唯一、ID 和不透明字段校验：重复正文/调用、带空白/控制字符/超长 ID，以及空值、控制字符和错误类型的 opaque 字段均拒绝；Unicode 控制字符按 Cc 分类检查。
- [x] details 内容类型匹配，index 拒绝负数与 bool：每种 detail 的必填内容、无关内容、signature 许可范围及 index 均有负向保护。
- [x] assistant 角色、工具引用顺序/集合与正文引用关联：顺序一致和纯思考通过；倒序、缺少、增加、空正文带引用、非空正文无引用及非 assistant 角色拒绝。
- [x] 单轮体量恰好上限和超过一字节，None 保持：`test_size_is_compact_sorted_utf8_and_limit_is_atomic` 校验 UTF-8 紧凑排序 JSON 字节数；两条记录合计恰为 1048576 字节时保持对象身份，1048577 字节时全部非空记录丢弃 items 并保留来源，None 不变。
- [x] 清洗判定：正常和已不可回放记录保持身份；隔离工具参数凭据导致整步不可回放；六类明文字段覆盖绝对路径/凭据；五类不透明字段中的凭据样式保持原样，不做正则替换；原始消息与记录不被修改。
- [x] repr 与解析错误不泄露明文、密文、签名及 data：记录、各项及 details 的 repr 均检查；未知枚举、非法字段内容、控制字符等解析/构造失败只输出静态字段与原因。
- [x] 既有清洗行为及测试不变并通过：HEAD 与当前 AST 的全部 11 个既有函数相同；两个既有测试文件无改动，指定回归通过（一个已有平台权限跳过项详见验证）。
- [x] 源码地图公共契约组登记新源码及测试：仅新增对应两行。
- [x] 最终指定测试、Ruff 检查与格式检查通过，审阅 diff，状态仅含允许文件：见上方命令和证据；新契约仅由清洗模块及新测试引用，无禁止模块接入或顶层导出改动。
