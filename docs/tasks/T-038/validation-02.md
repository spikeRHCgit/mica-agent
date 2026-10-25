# T-038 验证证据（返工 1）

> 验证环境：Windows（`os.name == "nt"`）· Python 3.11.15 · uv 0.11.19 · pytest 9.1.1 · Ruff 0.16.2
> worktree：`C:/Users/10601/Desktop/learning/mica-wt/T-038` · 分支 `task/T-038-model-catalog` · 本轮工作 HEAD `21d587d`
> 本轮保留第 1 轮未提交的 `model_catalog.py` / `test_model_catalog.py` / `module_map.md` 改动；只使用隔离临时目录、假 Key、HTTPX transport 替身和本地 asyncio Event，未读取真实 `~/.mica`、真实凭据或渠道。未提交、未推送。

## 1. 命令与结果

| 阶段 | 命令 | 结果 |
|---|---|---|
| 修前首次 | 4 个新增用例，`-q -W error` | 4 failed（0.78s；3 个产品失败 + 1 个测试装配错误，见 2.1） |
| 修前复现 | 修正装配后的同 4 个用例 | **3 failed, 1 passed**（0.70s） |
| 修后回归 | 同 4 个用例 | **4 passed**（0.17s） |
| 测试采集 | `uv run --no-sync --offline pytest tests/test_model_catalog.py --collect-only -q` | 66 tests collected（0.14s） |
| 专项回归 | `uv run --no-sync --offline pytest tests/test_model_catalog.py tests/test_config_store.py tests/test_config_service.py -q -W error` | **271 passed**（102.01s） |
| 全量 | `uv run --no-sync --offline pytest -q -W error` | **2889 passed, 3 skipped**（358.37s） |
| Ruff | `uv run --no-sync --offline ruff check src/mica_agent tests` | All checks passed |
| 格式 | `uv run --no-sync --offline ruff format --check src/mica_agent tests` | 122 files already formatted |
| 空白 | `git diff --check` | 通过（无输出） |

`tests/test_model_catalog.py` 采集 66 项（第 1 轮 62 项 + 本轮 4 项）。全量 3 项跳过为仓库既有平台/条件用例，数量与第 1 轮一致；本轮未新增 `skip`、未删除或放宽既有断言。

## 2. 修前失败、根因与复现

### 2.1 首次运行

首次执行本轮 4 个新增用例为 **4 failed**：

- 三个请求头用例按预期失败，异常为 HTTPX 请求构建阶段的 `UnicodeEncodeError`，不是 `ConfigError`。
- 第 4 个“Go 未使用非 ASCII Key”用例失败，原因是测试 helper 在 `asyncio.run` 返回后调用 `asyncio.current_task()`，触发 `RuntimeError: no running event loop`。这是新增测试的事件循环装配错误，不是产品缺陷；未据此修改产品。

将 `_pending_tasks()` 断言移入事件循环后，重新执行同一组用例：

```text
FFF.                                                                     [100%]
3 failed, 1 passed in 0.70s
```

三个失败分别对应：DeepSeek Bearer Key、自定义 Anthropic Messages 的 `x-api-key`、Go 的 `x-opencode-session`。第 4 个 Go 未使用 Key 用例通过，说明修复前该路径本来就不读取也不发送 `api_key`。

### 2.2 失败位置

三个用例的 traceback 都停在 HTTPX 构建请求头处，尚未进入 `RecordingTransport.handle_async_request`：

```text
.../httpx/_client.py: build_request
.../httpx/_models.py: Headers.update
.../httpx/_models.py: _normalize_header_value
    return value.encode(encoding or "ascii")
E   UnicodeEncodeError: 'ascii' codec can't encode characters ...
```

这证明失败发生在出站前；review-01 的独立复核也记录 `mock_requests: 0`、`pending_tasks: 0`。未观察到真实请求、真实凭据读取或资源残留。

### 2.3 根因

1. `_required_key` 与 `_session_id` 只使用 `is_text`，其语义是“非空、可打印文本”，并非“可安全编码为 HTTP 头”。因此中文等非 ASCII 可打印字符可以通过目标校验。
2. `_request` 将 Key 拼入 `Authorization: Bearer ...`，或作为 `x-api-key`，并将会话 ID 作为 `x-opencode-session`，没有在真正出站前检查头值编码。
3. HTTPX 对 str 请求头执行 ASCII 编码；`UnicodeEncodeError` 不是 `_fetch` 当前处理的 `_PayloadError`、`TimeoutException`、`DecodingError`、`HTTPError` 或 `OSError`，于是绕过固定错误边界，直接抛给 `discover` 调用方。
4. 这不是“普通文本合法就等于 HTTP 头可表示”的问题，也不能靠存储层允许 Unicode Key 来保证出站安全。

## 3. 修复边界

- 在 `_request` 返回前统一校验实际要发送的每个头值：`_header_value` 要求非空、ASCII、可打印的 `str`，否则抛出固定的 `ConfigError("catalog request header is invalid")`。
- 校验发生在 `_prepare` 中、`_bounded_fetch` 创建请求任务之前；因此异常路径不会创建 HTTPX client、不会发出请求、不会留下请求/取消任务。
- 覆盖实际入口：内置渠道 Bearer Key、自定义 OpenAI 风格 Bearer、自定义 Anthropic `x-api-key`、Go `x-opencode-session`，以及固定的 User-Agent 等所有实际头值。
- 合法 ASCII 值原样返回；没有 Unicode 转义、替换字符、截断、去头重试、静默忽略或有损编码。既有测试仍断言 `Authorization: Bearer TEST_ONLY_KEY`、`x-api-key: TEST_ONLY_KEY`、`x-opencode-session: session-123` 的原值。
- 没有增加宽泛 `except Exception`；没有把 `CancellationRequested` / `CancelledError` 转成普通失败。`_fetch` 的既有固定错误类别和取消收口未改。
- Go 的请求构造不读取 `api_key`，所以非 ASCII 的未使用 Key 不会被新增条件拒绝，也不会被放入请求头。
- 未修改 `_required_key` / `_session_id` 的通用文本规则、CredentialStore、ConfigStore、配置格式、依赖、Provider/Engine、App/CLI/TUI 或顶层导出。非 ASCII Key 仍可能存在于既有存储；本轮按“不改存储契约”的要求，只在目录发现出站前安全拒绝。

## 4. 回归覆盖与断言

新增 4 项测试：

1. `test_non_ascii_bearer_key_is_rejected_before_request`：DeepSeek 目标使用含非 ASCII 字符的假 Key。断言抛出 `ConfigError`，错误文本不含该假 Key、`api.deepseek.com`、`/models` 或 `UnicodeEncodeError`；`transport.requests == []`、`transport.responses == []`、`aclose_count == 0`、无遗留任务。
2. `test_non_ascii_anthropic_key_is_rejected_before_request`：自定义 `anthropic-messages` 目标使用非 ASCII 假 Key，覆盖 `x-api-key` 入口；断言同上，另外确认错误不含 `custom.test` 与 `/v1/models`。
3. `test_non_ascii_go_session_is_rejected_before_request`：Go 目标使用不含路径/空白/控制字符的非 ASCII 会话 ID，覆盖 `x-opencode-session` 入口；断言同上，另外确认错误不含 `opencode.ai` 与 `/inference/go/v1/models`。
4. `test_go_does_not_reject_or_send_unused_non_ascii_key`：Go 目标带非 ASCII 假 Key 和合法 ASCII 会话 ID；断言目录成功、请求存在、无 `Authorization` / `x-api-key`、假 Key 不在请求头字符串中、`x-opencode-session` 为原值。该用例在修前和修后均通过，用于防止为未使用 Key 增加过度拒绝。

公共 helper 在事件循环内执行上述资源断言，避免把“没有运行中的 loop”误记为任务泄漏。拒绝用例的失败发生在任何请求任务创建之前，因此 `aclose_count == 0` 表示没有创建 client，而不是 client 未关闭。

## 5. 最终验证

- 专项回归 **271 passed**：第 1 轮的 62 项目录测试、4 项本轮回归，以及既有 `test_config_store.py` / `test_config_service.py` 保护均通过。
- 全量 **2889 passed, 3 skipped**：相对第 1 轮 2885 passed 增加 4 项，未新增跳过；既有取消、出站、资源收口、CAS、存储冲突和 ConfigService 闭环断言均保留。
- Ruff 检查、格式检查和 `git diff --check` 均通过。
- 工作区只包含第 1 轮允许的产品/文档改动与本轮新增的两份文档；未修改任务卡、STATUS、审查、返工要求、第一轮报告或第一轮验证。
- 未暂存、未提交、未推送、未 stash、未还原或清理他人文件。

## 6. 隔离与未验证项

- 全部网络路径仍为 `RecordingTransport` / `httpx.MockTransport` 模拟；没有真实 DNS、TLS、渠道或模型请求，没有读取真实 `~/.mica` 或真实环境 Key。
- 未实现或验证 `/login` 菜单、真实渠道登录、账号资格、额度或真实模型调用；目录业务和模拟保存通过不代表这些条件成立。
- 本机为 Windows；未在 POSIX 实机补跑。全量 3 项跳过与第 1 轮一致。
- OpenRouter reasoning 仍保持未知；DeepSeek `/v1` 兼容基址仍只匹配固定目录 URL。第 1 轮的这些决定未在本轮改变。
