# T-038 返工 1 · 2026-10-25

依据：[任务卡](T-038.md)、[review-01.md](review-01.md)。本轮只修一个已复现的请求头输入边界缺陷，不重做目录实现或渠道调研。

## 问题

`_required_key` 和 `_session_id` 接受非 ASCII 可打印字符串，HTTPX 编码 str headers 时抛 UnicodeEncodeError，绕过 ConfigError/固定结果边界。master 已用假 Key 和 MockTransport 分别复现非 ASCII Key 与 Go 会话 ID，两例均在请求发出前失败，详见审查证据。

## 返工要求

1. 在目录请求边界校验实际要发送的 HTTP 头值。推荐沿用当前目标校验方式：不能安全表示为 HTTPX 请求头的输入在请求前转成固定、安全的 ConfigError；普通文本 `is_text` 不能代替头值校验。不改变既有存储或通用文本契约。
2. 覆盖 Bearer Key、自定义 Messages 的 x-api-key、Go 的 x-opencode-session 实际入口；不直接透传 UnicodeEncodeError 或任意底层异常正文。不要通过宽泛 `except Exception` 吞掉编程错误或取消；保留 CancellationRequested / CancelledError 语义。
3. 不对 Key 或会话 ID 做有损编码、替换、截断、丢头后重试或静默忽略；合法 ASCII 输入的原值必须保持。Go 公开目录不发送用户 Key，因此不应为未使用的 Key 新增拒绝条件或把它发送出去。
4. 先添加能证明当前问题的回归并记录修复前失败，再修实现；断言安全错误不含假 Key/会话值、URL、路径或原始底层异常文本，请求未发出且无任务/客户端泄漏。网络仍只模拟，不读取真实配置或凭据。

## 写入范围与交付

- 产品与测试只允许原卡的 model_catalog.py、test_model_catalog.py；module_map 仅在实际职责变化时同步，不改已验收存储、配置格式、依赖、Provider/Engine、App/CLI/TUI 或顶层导出。
- 本轮只新增 `docs/tasks/T-038/worker-02.md`、`validation-02.md`。第一轮 worker-01.md、validation.md 冻结；任务卡、STATUS、审查和本返工要求由 master 管理。
- 保留当前未提交产品改动，在同一 worktree 连续完成，不暂存/提交/推送/stash/还原或清理他人文件。原卡无真实配置、凭据、网络和 T-036 余额的边界不变。

## 完成标准

- 上述两类实际反例不再抛出未处理 UnicodeEncodeError；各认证头路径都有有效保护，合法请求行为不变。
- 新增回归修前失败、修后通过；不放宽、删除或跳过既有取消、出站、资源和 CAS 保护。
- 开发期间只跑必要用例；最终专项沿用 `uv run pytest tests/test_model_catalog.py tests/test_config_store.py tests/test_config_service.py -q -W error`。实现稳定后按原卡集中执行最终全量和全仓 Ruff，不在每个局部修正后重复全量；不改依赖/分发，不另构建。
- validation-02.md 保留首个失败、根因、最终命令和结果；报告明确仍未实现 `/login` 或真实渠道验收。交回后 master 继续验收，随后推进 C1 应用/终端闭环，不回到 Messages 诊断。
