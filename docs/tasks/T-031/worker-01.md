# 第 1 轮汇报 · 2026-10-14

依据：[`T-031.md`](T-031.md)。

- 结果：完成 —— 独立 Messages 适配器按卡交付，171 项专项与全量（含安装态）验证通过；两项既有 Windows 符号链接测试因权限跳过，未暂存、提交或推送。
- 改动：新建 `src/mica_agent/providers/anthropic_messages.py` 和 `tests/test_anthropic_messages_provider.py`；`docs/module_map.md` Provider 组新增两项；本报告。
- 验证：`uv run pytest tests/test_anthropic_messages_provider.py -q -W error` → `171 passed in 1.16s`；`uv run pytest -q -W error` → `2518 passed, 2 skipped in 196.42s (0:03:16)`、退出码 0（含 `tests/test_distribution.py` 的离线构建、隔离 wheel 安装及安装态行为）；`uv run ruff check src/mica_agent tests` → `All checks passed!`；`uv run ruff format --check src/mica_agent tests` → `116 files already formatted`；`git diff --check` → 无输出、退出码 0。
- 偏离与疑点：无范围或契约变更。未知客户端工具参数 delta 无法证明参数完整，按 malformed 拒绝；其他未知语义降级，不转换服务端工具。工具结果没有独立状态字段，真实 content 原样传递，不猜 `is_error`。本卡只实现 adaptive；模型不支持 disabled/adaptive/effort 或服务端拒绝签名时保留真实失败。
- 建议：无。

### 明细

- [x] 请求字面量、端点鉴权、system、tools、预算、off 与思考映射：`test_request_literal_endpoint_auth_system_schema_budget` 比较完整 payload/headers/URL；默认、显式、空映射、minimal、接口不符与中途 system 均有正负向断言，不发送 beta/budget_tokens。
- [x] 历史配对、ID 无碰撞映射、绑定与续接槽位、签名回放限制：`test_id_mapping_reserves_legal_ids_is_deterministic_and_does_not_mutate_history` 保留后出现的合法 ID；三类绑定不符、未知来源、None/unreplayable 与后续兼容槽位逐项覆盖；孤立、重复、缺失、先于调用和中途插入 user 均在 HTTP 前拒绝；含签名/密文且需改 ID 拒绝。
- [x] 真实适配器与既有 Engine 两步多工具往返：`test_real_engine_two_step_multi_tool_roundtrip_and_original_order` 首步 Finish 精确比较思考→调用→正文→调用；Engine 完成两次工具执行，sink 的原消息/续接正确；次请求完整字面量比较，真实结果紧接 assistant 合并到 user。
- [x] 纯思考、密文、正文、工具、空步骤的 Finish 和续接：`test_complete_finish_shapes_and_known_unknown_source` 参数化验证；含空 thinking + 有效 signature、空正文；无续接为 None，真实降级为空 items 的 UNREPLAYABLE。
- [x] SSE 帧、碎片、生命周期、身份与严格 JSON 校验：注释、多行 data、event/type、ping、start 正文/思考、签名碎片与对象参数碎片均覆盖；重复/缺失起止、索引、stop 后 delta、工具 ID/名称冲突、类型错配、非对象/重复键/NaN/Infinity/溢出浮点/非法 Unicode 均安全拒绝。
- [x] 真实终态、截断/拒绝/暂停/error/EOF/断流，工具不执行：仅 message_stop 发 Finish；message_delta/block_stop/[DONE]/EOF 不成功；五类异常 stop reason 和明确 error.type 分类断言单个安全错误终态；`test_real_engine_never_executes_incomplete_or_server_tools` 证明不完整工具、暂停、截断及服务端工具均无客户端执行。
- [x] 未知语义与不可表示输出降级，保留通用事实：缺失/空/非法控制符签名和密文、多正文、引用信息、未知块/事件/语义字段降级；保留已发正文/思考，未知服务端工具不成为本地调用，已知包装字段不误判。
- [x] 精确 1 MiB、超 1 字节、UTF-8/转义与增量释放 sticky 降级：紧凑 UTF-8 wire 计量含绑定、引用与项分隔；正文引用 + thinking、redacted + thinking + tool 两组精确边界覆盖；`test_incremental_overflow_releases_private_data_and_is_sticky` 检查所有专属 saved 已释放，后续块和签名不恢复，长度/摘要仍保留。
- [x] 取消、超时、HTTP、有限重试与资源关闭：连接错误/超时有限重试；HTTP 错误在 backoff 前关闭；HTTP 400 签名/disabled 拒绝不改参不重试；HTTP 建立后的读超时/断流无论是否已输出均 malformed、不重试；取消传播 CancelledError，response/body 关闭；client aclose 两次仅关闭 transport 一次。
- [x] 模块登记、最终 diff 与写入范围核对：module_map 仅增加两个文件登记；main 串行施工，初始工作区干净；当前仅四个允许路径，暂存区为空，未提交/推送或新建分支，旧 Provider、Engine 与契约未修改。
- [x] 专项、全量（含安装态）、Ruff 与 git diff --check 验证：最终代码版本专项 171 passed，全量 2518 passed/2 skipped，安装态包含于全量；全部静态检查通过。之后仅更新本报告，代码和测试未再变化。

### 平台跳过

两项既有测试 `tests/test_workspace_paths.py::test_resolver_rejects_existing_symbolic_link_in_path` 与 `tests/test_tool_presentation.py::test_workspace_reparse_point_is_not_retained_in_presentation` 因创建 Windows 目录符号链接所需权限不足（`WinError 1314`）跳过。以 `uv run pytest tests/test_workspace_paths.py::test_resolver_rejects_existing_symbolic_link_in_path tests/test_tool_presentation.py::test_workspace_reparse_point_is_not_retained_in_presentation -q -W error -rs` 单独核对，结果 `2 skipped in 0.09s`，明确显示同一权限原因；未修改或新增 skip，也未放宽断言。本卡新增的 171 项无跳过。

### 协议核对与证据边界

2026-10-14 只读核对官方 [Messages API](https://platform.claude.com/docs/en/api/messages/create)、[Streaming](https://platform.claude.com/docs/en/build-with-claude/streaming)、[Thinking](https://platform.claude.com/docs/en/build-with-claude/thinking)、[Effort](https://platform.claude.com/docs/en/build-with-claude/effort)、[Thinking 工具工作流](https://platform.claude.com/docs/en/build-with-claude/thinking-tool-workflows)、[工具定义](https://platform.claude.com/docs/en/agents-and-tools/tool-use/define-tools)、[Stop reasons](https://platform.claude.com/docs/en/build-with-claude/handling-stop-reasons)。`/api/messages` 入口抓取失败后读取具体 create API 页；工具定义旧链接由官方重定向。

HTTP 字段、adaptive/summarized、五档 effort、工具对象参数和真实 stop reason 与卡片方案一致。官方说明部分模型拒绝 disabled，故不宣称全模型兼容；本轮只有 fake key、固定 SSE 与替身传输证据，未访问真实凭据、真实 `~/.mica` 或真实渠道，真实连接验收与应用装配仍留后续步骤。

专项首次运行的 6 个失败来自测试构造：跨 API record 在替换 api 之前即触发契约校验，以及将 AsyncByteStream 放入 `content=` 后被 httpx 包装，无法观察底层 aclose。已分别改为构造时指定 api、`Response(stream=...)`，未放宽关闭断言。后续两项边界样本误含 opaque 字段不允许的换行，改用合法 UTF-8/转义密文样本；非法控制符负向覆盖仍保留。最终专项全部通过。
