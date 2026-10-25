# T-038 验证证据

> 验证环境：Windows 10/11（`os.name == "nt"`）· Python 3.11.15（`uv sync --python 3.11`）· uv 0.11.19 · pytest 9.1.1 · Ruff 0.16.2
> worktree：`C:/Users/10601/Desktop/learning/mica-wt/T-038` · 分支 `task/T-038-model-catalog` · 开始 HEAD `45ed62d`
> 本轮只使用隔离临时目录、假 Key（`TEST_ONLY_*`）、HTTPX transport 替身和本地 asyncio Event；未读取真实 `~/.mica`、真实凭据或渠道，未发送真实模型请求。未提交、未推送。

## 1. 命令与结果

| 阶段 | 命令 | 结果 |
|---|---|---|
| 环境 | `uv sync --python 3.11` | 成功，CPython 3.11.15，25 个包 |
| 首次新测试 | `uv run pytest tests/test_model_catalog.py -q -W error` | 12 failed, 48 passed（2.58s；失败原因见第 2 节） |
| 修正后新测试 | `uv run pytest tests/test_model_catalog.py -q -W error` | 62 passed |
| 专项回归（最终） | `uv run pytest tests/test_model_catalog.py tests/test_config_store.py tests/test_config_service.py -q -W error` | **267 passed**（87.59s） |
| 全量（最终） | `uv run pytest -q -W error` | **2885 passed, 3 skipped**（294.79s） |
| 静态检查 | `uv run ruff check src/mica_agent tests` | All checks passed |
| 格式 | `uv run ruff format --check src/mica_agent tests` | 122 files already formatted |
| 空白 | `git diff --check` | 通过（无输出） |

新增测试文件采集 62 项，全部通过。全量 3 项跳过为仓库既有平台/条件用例（数量与 T-037 记录一致），本卡未新增 `skip`、未放宽断言。无依赖、锁文件或分发变更，未另做构建。

## 2. 首次失败、根因与修正证据

按实际发生顺序记录；失败与修正均为测试装配或测试期望问题，没有通过放宽产品断言掩盖：

1. **首次运行 12 failed / 48 passed**。首个错误为 `test_invalid_payloads_are_not_reported_as_empty` 的 `TypeError: Response object is not callable`。根因是测试把 `_response(...)` 返回的 `httpx.Response` 直接传给 `RecordingTransport`，而该替身参数应为异步 handler。修正为 `RecordingTransport(_handler(content=body))`；产品代码未改。
2. **分片/取消正文的流关闭断言失败**：`stream.closed` 为 `False`。根因是 `httpx.Response(content=AsyncIterable)` 会把可迭代对象包进 `AsyncIteratorByteStream`，其继承的 `aclose` 不转发到内层自定义流；测试原先观察的是包装层，而不是产品是否关闭响应/client。产品路径始终使用 `client.stream` 并在上下文退出时关闭响应；测试改为用 `httpx.Response(stream=...)` 让内层流的 `aclose` 可观察，同时保留 `response.is_closed`、`transport.aclose_count` 和“无遗留任务”断言。未删除或放宽取消/关闭保护。
3. **ConfigService 集成测试 `keep["maxTokens"]` KeyError**。根因是种子记录 ID 为 `keep`，而手写 DeepSeek 目录中没有同 ID 条目，测试实际没有覆盖“同一 ID 保留手工字段并补缺失字段”。改为种子已有 `deepseek-chat`（手工名称/上下文/API/未知字段）加远端缺席的 `remote-gone`，并新增新模型 `deepseek-reasoner`，使同 ID 补字段、保留手工字段、保留缺席选中项和显式取消都被实际执行；产品合并逻辑未改。
4. **开发期 Ruff**：`TRY004`（类型检查抛 `ValueError`）、`SIM117`（嵌套 `async with`）和格式检查失败；修正为 `TypeError`、合并上下文并按项目 Ruff 格式化，随后通过。

首次运行后没有跳过失败用例、没有删除独有保护、没有放宽跨源/取消/冲突断言。

## 3. 目录请求、解析与结果区分证据

### 3.1 三渠道手写样本

样本为测试文件内手写的假模型 ID 与响应，不调用被测解析器生成期望：

- **DeepSeek**：`test_deepseek_request_and_explicit_metadata_mapping` 断言 `GET`、固定 URL、`Authorization: Bearer TEST_ONLY_KEY`、Mica User-Agent；解析 `deepseek-chat` 的名称、`context_window=128000`、`max_output_tokens=8192`、`input_modalities=["text","image","audio"]→("text","image")`、`effort.supported_levels`→现有 low/medium/high 映射；未知字段忽略；响应与 client 关闭。`test_deepseek_v1_base_keeps_the_fixed_directory_url` 验证 `/v1` 兼容基址不会改变固定目录 URL。
- **OpenRouter**：`test_openrouter_metadata_incomplete_next_and_no_pagination` 断言固定 URL、Bearer、请求无 `offset/limit` 查询参数；`context_length`、`top_provider.max_completion_tokens`、`architecture.input_modalities` 映射；含 `reasoning` 的 `supported_parameters` 不生成思考能力（保持未知）；`links.next` 非空返回 `incomplete`。`links.next` 为 `null` 时返回 `ok`；空 `data` 且 `next` 非空返回 `incomplete` 而不是 `empty`。
- **OpenCode Go**：`test_opencode_go_is_public_and_sends_mica_session_without_key` 断言固定公开 URL、无 `Authorization`/`x-api-key`、假 Key 不进入请求、Mica User-Agent、`x-opencode-session: session-123`；`go-alpha/go-beta` 解析为最小元数据（无伪造名称/上下文/reasoning）。非法基址、路径形/空白会话 ID 和目录 URL 覆盖均在请求前拒绝且 transport 请求数为 0。
- **自定义**：`test_custom_without_runtime_url_requires_manual_entry` 验证无运行期 URL 返回 `manual` 且不建 client；`test_custom_same_origin_directory_uses_minimal_shape` 验证同源 URL、Bearer 和最小 `data/id` 结构；`context_length` 等未知字段不写成发现能力。`test_custom_anthropic_uses_the_explicit_interface_headers` 验证 Anthropic 接口的 `x-api-key`/`anthropic-version`，不发送 Bearer。跨源、带 query、未知接口、缺 Key 的目标全部在请求前拒绝。

### 3.2 请求前拒绝与不跨源发送 Key

`test_builtin_target_mismatch_is_rejected_before_request` 覆盖：DeepSeek 恶意基址、固定目录 URL 被覆盖、接口不匹配、缺 Key、OpenRouter 目录 URL 被覆盖、`chatgpt-subscription`；`test_go_target_mismatch_is_rejected_before_request` 覆盖 Go 非官方基址、路径形/空白会话、目录覆盖；`test_custom_target_mismatch_is_rejected_before_request` 覆盖跨源、query、未知接口、缺 Key。所有用例断言 `transport.requests == []` 且 `aclose_count == 0`。

### 3.3 空/失败/不完整/手填

- **空**：`test_empty_directory_is_distinct_from_failure` 返回 `empty`、模型为空、无错误。
- **失败**：`test_http_failures_are_fixed_categories_without_remote_text` 覆盖 401/403→`authentication`、429→`rate_limited`、500→`http`、302→`redirect`；远端正文中的 `TEST_ONLY_SECRET` 不出现在结果 repr，且只发一次请求、不跟随重定向。`test_network_failure_is_safe_and_closes_resources` 覆盖建连失败→`network`；`test_disconnected_body_is_safe_and_closes_resources` 覆盖正文读取中断→`network`，内层流与响应均关闭。
- **超时**：`test_total_deadline_returns_timeout_and_closes_resources` 将 deadline 缩短为 0.2s，请求等待头时超时，返回 `timeout`，client 关闭且无遗留任务。
- **坏结构**：`test_invalid_payloads_are_not_reported_as_empty` 参数化覆盖坏 JSON、根为数组、`data` 非数组、条目非对象、缺 ID、重复 ID、重复 JSON 字段、NaN、非 UTF-8；全部返回 `invalid_response`，不误报空目录。
- **字段级无效**：`test_invalid_metadata_is_counted_and_not_written_as_discovered` 覆盖名称为非文本、上下文为 bool、输出预算为 0、输入模态类型错误、effort 结构错误，得到 `invalid_metadata == 5` 且不写伪能力；另一条记录中未知模态 `video`、未知等级 `turbo` 被过滤，合法 text/low 仍保留。
- **大小与条目上限**：`test_decompressed_body_limit_is_enforced` 用 gzip 压缩 `16 MiB + 1` 的解压内容，返回 `too_large`；`test_entry_limit_is_enforced` 用 10001 条返回 `too_large`。
- **gzip/分片**：`test_gzip_and_chunked_bodies_are_parsed_and_closed` 验证 gzip 解码和多块分片正文均成功解析，内层流、响应和 client 均关闭。

## 4. 取消与资源收口证据

- `test_cancel_before_request_sends_nothing`：token 已取消时不创建 client、不建请求。
- `test_cancel_while_waiting_for_headers_closes_client_and_tasks`：等待响应头时取消，抛 `CancellationRequested`，transport `aclose_count == 1`，无遗留任务。
- `test_cancel_while_reading_body_closes_response_and_stream`：正文读到一半取消，抛 `CancellationRequested`；内层流 `closed == True`、响应 `is_closed == True`、transport `aclose_count == 1`、无遗留任务。
- `test_external_cancel_propagates_and_closes_client_and_tasks`：外部 `task.cancel()` 的 `CancelledError` 原样传播，client 关闭，无遗留任务。
- 关键取消、失败和资源用例均通过 `asyncio.all_tasks()` 断言没有遗留请求任务或取消观察任务；等待使用 `asyncio.Event` / `wait_for`，不以 sleep 猜时序。
- 请求成功、状态失败、重定向和取消用例均断言实际请求数；重定向用例明确断言请求数为 1 且未跟随 `Location`。

## 5. 勾选保存与 ConfigService 闭环证据

`test_discovery_selection_save_round_trips_through_config_service`（隔离 `data/`、`workspace/.mica/`、受保护假 `auth.json`）：

1. 种子供应商已有 `deepseek-chat`（手工名称、`contextWindow=999999`、显式 `api=openai-responses`、未知字段 `manual`）和远端缺席的 `remote-gone`。
2. 真实 `discover` 手写 DeepSeek 目录后，选中 `deepseek-chat`、`remote-gone` 和新模型 `deepseek-reasoner` 保存。
3. 保存顺序为既有顺序后追加新模型；`deepseek-chat` 保留手工名称/上下文/API/未知字段，补入远端缺失的 `maxTokens/input/reasoning/thinkingLevelMap`；`remote-gone` 原样保留；新模型显式写入 `api` 与目录白名单字段。
4. `other` 供应商、`futureRoot`、`config.json` 默认模型字节、`auth.json` 凭据字节均不变。
5. 现有 `ConfigService.load()` 解析出上述模型与能力（`deepseek-chat` 的 `openai-responses` 显式接口和手工预算；`deepseek-reasoner` 的上下文/输出/输入/思考映射）。
6. 再次保存只选 `deepseek-reasoner`，`deepseek-chat` 与 `remote-gone` 被显式取消并移除，证明“远端暂缺不等于取消、显式取消才移除”。

其他保存保护：

- `test_incomplete_catalog_saves_seen_models_and_keeps_selected_absent_model`：OpenRouter `incomplete` 结果可保存已见项；已选但远端未返回的 `selected-old` 保留，未选的 `dropped-old` 移除，未选的 `or-basic` 不写入。
- `test_go_new_models_need_explicit_api_choices_and_keep_existing_api`：无 `api_choices` 时新 Go 模型保存被拒；非法 `google` 选择被拒且字节不变；显式 `openai-completions` 成功后，既有 `go-old` 的继承接口和 `go-explicit` 的 `anthropic-messages` 不被刷新改写。
- `test_unknown_provider_api_needs_an_explicit_model_choice`：供应商没有显式接口时，新模型无选择被拒，有显式选择才保存。
- `test_failed_empty_and_manual_results_never_overwrite_models`：`failed`、`empty`、`manual` 结果保存均为 `ConfigError` 且 `models.json` 字节不变。
- `test_unknown_selected_identifier_rejects_the_whole_save`：选中既不在目录也不在既有记录的 ID 时整体拒绝，不伪造条目。
- `test_stale_expected_provider_rejects_save_without_replacing_connection`：发现后并发编辑、禁用或删除供应商，保存均 `ConfigConflictError`，冲突后的旧字节不变，不重建连接。
- `test_save_requires_the_complete_expected_record`：`expected_provider=None` 或把 `selected_ids` 传成字符串均拒绝。
- `test_validation_failure_does_not_partially_save`：合并结果超过存储上限时 `ConfigError`，旧字节不变且无临时文件残留。

## 6. 安全、隔离与范围证据

- 全部测试使用 `tmp_path` 临时数据根和工作区；`auth.json` 为假 Key 且仅在需要 `ConfigService` 时用既有 `protect()` 设置真实隔离 ACL。
- 网络只经过 `RecordingTransport` / `httpx.MockTransport`；没有真实 DNS、TLS、渠道或模型请求。
- 未读取真实 `~/.mica`、真实环境 Key、OAuth 凭据或其他客户端身份；Go 请求不发送用户 Key。
- 未修改 Provider/Engine/App/CLI/TUI、依赖、锁文件、既有配置/取消/权限契约、计划、决定、STATUS、任务卡或渠道依据；未在包顶层导出新符号。
- 未执行提交、推送、暂存、stash、还原或清理他人文件。

## 7. 未验证与限制

- 未做真实 DeepSeek/OpenRouter/OpenCode Go 请求、真实 Key 认证、账号资格或额度验证；目录成功不代表这些条件成立。
- 未实现 `/login` 菜单、登录表单、连接维护或应用/终端装配；本卡只交付目录与勾选保存业务。
- 本机为 Windows；取消/HTTPX 逻辑无平台分支，但未在 POSIX 实机补跑。全量 3 项跳过为既有平台/条件用例。
- OpenRouter 明确未把 `supported_parameters` 中的 `reasoning` 外推为全部 effort 可用；该能力保持未知，待渠道明确字段或后续产品决定。
- DeepSeek 允许既有配置示例中的 `/v1` 兼容基址参与匹配，但 Key 只发往固定目录 `https://api.deepseek.com/models`。
- 新模型条目写入显式 `api`（`api_choices` > 内置渠道固定映射 > 显式供应商接口）；既有条目的显式或继承接口事实不因刷新改写。
