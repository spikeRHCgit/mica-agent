# T-038 worker-02 执行报告（返工 1）

## 结论

按 [review-01.md](review-01.md) 与 [rework-01-request.md](rework-01-request.md)，本轮只修复目录请求头的输入安全失败边界。第 1 轮全部产品实现、测试和文档保留；未改存储契约、配置格式、依赖、Provider/Engine/App/CLI/TUI、module_map、任务卡或真实渠道。

修前（测试装配修正后）新增回归为 **3 failed, 1 passed**：三个请求头入口在 HTTPX 构建请求时抛出未处理的 `UnicodeEncodeError`；Go 未使用 Key 的对照用例通过。修后为 **4 passed**；专项 **271 passed**，全量 **2889 passed, 3 skipped**，Ruff、格式和空白检查通过。

## 缺陷与根因

- `_required_key` 与 `_session_id` 使用通用文本规则 `is_text`，接受非 ASCII 可打印字符串；`_request` 随后把 Key 直接拼入 `Authorization: Bearer ...`、`x-api-key`，或把会话 ID 放入 `x-opencode-session`。
- HTTPX 将 str 请求头按 ASCII 编码。请求构建阶段在 `build_request` / `Headers` / `_normalize_header_value` 抛出 `UnicodeEncodeError`，发生在 transport handler 之前；`_fetch` 的固定错误类别不包含该异常，因此它越过 `ConfigError` / 固定结果边界直接传到调用方。
- review-01 的独立复核记录为 0 次模拟请求、0 个遗留任务，未观察到真实网络或凭据泄露；本轮修前失败同样停在请求构建阶段。问题在于错误输入没有被安全归类，而不是已发生泄漏。

## 修复

- `_request` 在返回 `_Request` 前，对实际要发送的每个头值调用 `_header_value`。该函数要求值为非空、ASCII、可打印字符串；否则抛出固定安全错误 `ConfigError("catalog request header is invalid")`。
- 校验发生在 `_prepare` 内、`_bounded_fetch` 之前，因此不创建 HTTPX client、请求任务或取消观察任务；不会发出请求。没有使用宽泛 `except Exception`，取消仍由原有 `CancellationRequested` / `CancelledError` 路径处理。
- 合法 ASCII 头值原样返回，不做替换、截断、丢弃、重编码或“去掉该头再重试”。因此合法 Key 和合法会话 ID 的值不变。
- 该边界覆盖 Bearer Key（DeepSeek、OpenRouter 和自定义 OpenAI 风格）、自定义 Anthropic Messages 的 `x-api-key`、Go 的 `x-opencode-session`，以及固定的 User-Agent 等实际头值。
- Go 的目标路径不把 `api_key` 放入请求头；非 ASCII 的未使用 Key 不会被新增条件拒绝，也不会被发送。新增对照用例验证该行为。
- 未修改 `_required_key` / `_session_id` 的通用文本规则，未修改 CredentialStore 或配置存储；非 ASCII Key 仍可能存在于既有存储中，但目录发现会在出站前安全拒绝。

## 修改文件

- `src/mica_agent/model_catalog.py`：新增 `_header_value`，在 `_request` 统一校验实际出站头值；其余目录、解析、取消和保存逻辑未改。
- `tests/test_model_catalog.py`：新增 4 项回归，测试文件共 66 项：非 ASCII Bearer Key、非 ASCII Anthropic `x-api-key`、非 ASCII Go 会话 ID 均在请求前得到安全 `ConfigError`；非 ASCII 的 Go 未使用 Key 仍不发送、也不被拒绝。
- `docs/tasks/T-038/worker-02.md`：本报告。
- `docs/tasks/T-038/validation-02.md`：修前失败、根因、修后回归与最终命令证据。
- `docs/module_map.md` 未改：新增的头值校验属于既有“有界发现”职责，没有新增文件或职责边界。第 1 轮的 `docs/module_map.md` 变更保持原样。

## 验证摘要

- 修前（测试装配修正后）：`uv run --no-sync --offline pytest ... 3 failed, 1 passed in 0.70s`；三个失败均为 HTTPX ASCII 编码异常。
- 修后：同一命令 `4 passed in 0.17s`。
- 专项：`uv run --no-sync --offline pytest tests/test_model_catalog.py tests/test_config_store.py tests/test_config_service.py -q -W error` → **271 passed**（102.01s）。
- 全量：`uv run --no-sync --offline pytest -q -W error` → **2889 passed, 3 skipped**（358.37s）；3 项跳过为既有平台/条件用例，本轮未新增跳过。
- 静态：`uv run --no-sync --offline ruff check src/mica_agent tests` 通过；`uv run --no-sync --offline ruff format --check src/mica_agent tests` 为 122 files already formatted；`git diff --check` 通过。
- 修后回归断言确认安全错误不包含假 Key/会话值、目标 URL、路径或 `UnicodeEncodeError` 文本；拒绝路径为 0 请求、0 响应、0 次 client 关闭，且无遗留任务。修前失败停在 HTTPX 构建请求阶段，review-01 的独立复核另记录 0 请求、0 遗留任务。
- 既有请求测试继续断言合法 ASCII 的 `Authorization: Bearer TEST_ONLY_KEY`、`x-api-key: TEST_ONLY_KEY` 和 `x-opencode-session: session-123` 原值；合法行为未变。

## 未完成事项与风险

- 仍未实现 `/login`、真实渠道登录、账号资格、额度或真实模型请求验收；本轮只用假 Key 和模拟 transport。
- 非 ASCII Key 仍可能被既有凭据存储接受；本轮按返工要求不改存储契约，只在目录发现出站前安全拒绝。若后续需要提前阻止保存，应由存储/凭据契约任务单独决定。
- 本机为 Windows；未在 POSIX 实机补跑，取消逻辑无平台分支，全量 3 项跳过与第 1 轮一致。
- OpenRouter reasoning 保持未知、DeepSeek `/v1` 兼容基址只匹配固定目录等第 1 轮决定未变，本轮不重新讨论。
- 未提交、未推送、未暂存、未 stash，未读取真实 `~/.mica`、真实 Key 或真实渠道。

交回 master 验收。
