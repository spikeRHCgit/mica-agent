# 第 1 轮汇报 · 2026-10-11

依据：[`T-030.md`](T-030.md)。

- 结果：完成 —— 独立 Responses 适配器及测试已交付，相关测试 325 passed，全量 2337 passed、2 skipped，指定静态与格式验证通过。
- 改动：新增 `src/mica_agent/providers/openai_responses.py`、`tests/test_openai_responses_provider.py`；在 `docs/module_map.md` Provider 组登记；本汇报。
- 验证：`uv run pytest tests/test_openai_responses_provider.py tests/test_provider_contract.py tests/test_engine.py -q -W error` → `325 passed in 6.13s`（exit 0）；`uv run pytest -q -W error -rs` → `2337 passed, 2 skipped in 184.78s (0:03:04)`（exit 0，附加 `-rs` 仅为列出跳过原因）；`uv run ruff check src/mica_agent tests` → `All checks passed!`；`uv run ruff format --check src/mica_agent tests` → `114 files already formatted`；`git diff --check` → 通过。最终全量包含既有 `tests/test_distribution.py` 离线构建、隔离安装态验证。
- 偏离与疑点：按用户授权直接在 main 工作区施工，不建分支，不做 Git 写操作。
- 建议：无。

### 协议与实现说明

- 已核对 [Responses create](https://developers.openai.com/api/reference/python/resources/responses/methods/create)、[Responses streaming events](https://developers.openai.com/api/reference/resources/responses/streaming-events)、[function calling](https://developers.openai.com/api/docs/guides/function-calling?strict-mode=disabled)、[reasoning](https://developers.openai.com/api/docs/guides/reasoning) 和 [conversation state](https://developers.openai.com/api/docs/guides/conversation-state)。原 create 页面体积超过浏览工具上限，改用同一官方 API Reference 的 Python 展示页核对 HTTP 字段；产品不引入 SDK。
- 本卡使用 `store=false`、完整 input、兼容的 `include`，不发送 `previous_response_id` 或 `reasoning.context`。当前官方 reasoning 文档说明 stateless 输出默认包含 encrypted_content，并继续接受该 include 值；保留已审查的请求形状。
- 私有续接用 continuation_size 度量；摘要与密文释放后，仅保留长度/摘要散列用于核对后续快照，不保存原始 response。已知 reasoning_text 只显示并核对，不因已知 content 包装字段未保存而降级；新增语义字段则降级。
- 施工首轮鉴权测试发现 httpx 会把 URL 用户信息转换为 Basic 鉴权并覆盖 Bearer，已清除 URL userinfo 并以请求头测试回归。全量首轮在已知 reasoning_text 包装字段的最终复核修正前主动终止，不作为最终通过证据；修正后重新执行完整验证。
- 测试通过 RecordingTransport、固定 SSE、受控 AsyncByteStream 和真实 AgentEngine/ToolExecutor 验证，不读取真实用户配置或凭据、不请求真实模型渠道。安装态与本地模拟 HTTP 由既有全量测试验证；不宣称真实模型渠道或终端验收。

### 明细

- [x] 请求字面量、system、预算、工具 schema、思考映射与端点：完整 payload 字面量、默认/显式/空映射、错误接口和不支持档位拒绝；URL 用户信息清除，Bearer 鉴权保持正确，repr 不含 URL。
- [x] 真实 Provider/Engine 两步多工具往返与 call_id、续接顺序：固定 SSE 样本交错两个调用参数碎片，真实 AgentEngine/ToolExecutor 执行；Finish 与第二次请求的 reasoning → 调用 → commentary 正文 → 调用 → 真实结果逐字段核对；输出项 id 不充当 call_id。
- [x] reasoning、phase、快照补齐与 delta/done 一致：纯思考、空步骤、最终快照补齐、文本与思考后缀不重复、最终密文从 done 或 completed 取；已知 reasoning_text 独立显示，续接只存白名单字段。
- [x] 来源绑定、槽位消费、不可回放和孤立工具结果：供应商/模型/接口不符、未知来源、None 与 unreplayable 退回通用历史；槽位始终消费；孤立、重复或先于调用的工具结果拒绝，未完成历史调用不补造结果。
- [x] 参数、身份、索引与快照冲突安全拒绝：非对象/非法 JSON、重复键、非有限数值、非法 Unicode 和过深参数拒绝；call_id/name/item_id/output_index/response_id 及 delta/done/completed 冲突不 Finish；真实 Engine 证明失败前工具不执行。
- [x] SSE 帧、真实终态、错误安全与取消资源收口：多行 data、注释、event/type 一致性；仅 completed 正常结束，done/failed/incomplete/error/[DONE]/EOF/断流保留失败；安全固定诊断；取消传播 CancelledError、响应关闭。
- [x] 未知输出、phase、多正文、缺失密文和 refusal 降级：通用正文及真实调用保留；未知服务端工具不作为本地调用；新增语义字段降级，未知内容替换已发部分时拒绝冲突。
- [x] 1 MiB 边界、UTF-8/转义计量及超限不可恢复：continuation_size 精确边界与超出 1 字节，Unicode/转义样本；摘要或最终密文超限后释放专属累计，后续 delta 不再保存，completed 不重装；内部检查只用于证明内存释放。
- [x] 重试有界、输出后不重试与幂等关闭：连接超时/错误与临时 HTTP 失败限定次数，退避前关闭 HTTP 响应；建立后的正文/思考/工具/专属输出或空流断流均不重试；aclose 重复调用只关闭客户端一次。
- [x] module_map 登记、最终 diff 和写入范围核对：登记两文件；直接通读新增实现与测试、审查已有文件 diff；最终仍为 main，状态仅四个允许文件，未暂存、提交、建分支或推送；App 装配与 C2 未施工。
- [x] 指定相关测试、全量测试（含安装态）、Ruff 和格式检查；记录平台跳过项：最终验证结果如上，无失败与 warning。下述两项均为既有符号链接保护场景的环境跳过，未修改测试或放宽断言。

### 平台跳过项

- `tests/test_tool_presentation.py:273`：`test_workspace_reparse_point_is_not_retained_in_presentation`，创建目录符号链接时 `WinError 1314`，当前进程没有所需特权。
- `tests/test_workspace_paths.py:69`：`test_resolver_rejects_existing_symbolic_link_in_path`，同样因 `WinError 1314` 无法创建目录符号链接。
- 没有新增跳过标记；Windows 专用的 ACL、Git 路径、控制台和目录 junction 测试没有因非 Windows 条件跳过。
