# T-038 第 1 轮审查 · 2026-10-25

依据：[任务卡](T-038.md)、[worker-01.md](worker-01.md)、[validation.md](validation.md)。

## 范围与证据

- 实际变更为新增 model_catalog.py、对应测试、两份交付文档及 module_map 的两行职责登记；符合任务范围。主仓与任务 worktree 的基线均为 45ed62d，没有额外产品变更需要合并复验。
- 审查了固定目录/同源检查、认证头、不可变结果、取消及 HTTPX 资源所有权、字段白名单和选择合并、完整记录 CAS。现有实现不猜 Go 接口或 OpenRouter effort 值，不修改默认、凭据或会话。
- 采信 worker 最终专项 267 passed、全量 2885 passed / 3 skipped、Ruff 与 diff check 证据。首次测试装配和期望问题的修正有具体解释，不把已有模拟通过当真实登录或 UI 交付。
- 高风险关键抽查实际执行：

```text
uv --directory C:/Users/10601/Desktop/learning/mica-wt/T-038 run --no-sync --offline pytest tests/test_model_catalog.py::test_builtin_target_mismatch_is_rejected_before_request tests/test_model_catalog.py::test_opencode_go_is_public_and_sends_mica_session_without_key tests/test_model_catalog.py::test_cancel_while_reading_body_closes_response_and_stream tests/test_model_catalog.py::test_external_cancel_propagates_and_closes_client_and_tasks tests/test_model_catalog.py::test_go_new_models_need_explicit_api_choices_and_keep_existing_api tests/test_model_catalog.py::test_stale_expected_provider_rejects_save_without_replacing_connection -q -W error
```

结果：**13 passed in 0.28s**。未重复全量。

## 需要修复的具体缺陷

model_catalog.py 的 `_required_key`（398～401 行）和 `_session_id`（404～415 行）接受非 ASCII 可打印字符串，但 `_fetch_result` 把它们作为 str 直接传入 HTTPX headers（444 行）；HTTPX 使用 ASCII 编码字符串头，抛出的 UnicodeEncodeError 没有被当前安全错误边界处理。

这不是必须依赖真实渠道才能确认的问题：普通文本合法不等于 HTTP 头可编码。既有 CredentialStore 也允许可打印 Unicode Key，所以不能假定上游已经排除了这种输入。Go 会话 ID 的当前校验同样未拒绝中文。

master 在临时 HOME/配置根中，以显式 CatalogTarget、假 Key 和 HTTPX MockTransport 做独立复核。两种输入分别为带中文的假 Key，以及不含路径/空白/控制字符的中文会话 ID；只输出枚举和计数，不输出异常正文或凭据。实际结果：

```json
{"checks":[{"case":"non_ascii_api_key","outcome":"UnicodeEncodeError","mock_requests":0},{"case":"non_ascii_session_id","outcome":"UnicodeEncodeError","mock_requests":0}],"real_requests":0,"real_credentials_read":false,"pending_tasks":0}
```

随后验证“结果应为安全返回或 ConfigError”的断言失败，命令 exit 1。这里是一个复核脚本中的两种输入，不是声称 pytest 新增了两个失败用例；现有 13 项关键回归仍通过。

影响：错误输入越过可供后续菜单处理的 ConfigError/固定结果边界；不能按已完成安全失败处理验收。未观察到网络请求、资源残留或真实秘密泄露，不夸大为已发生泄漏。

## 结论

**针对性返工，暂不验收产品。** 保留全部实现和第一轮报告，按 [rework-01-request.md](rework-01-request.md) 修复目录请求头输入边界并补回归，不扩大到存储、Provider、真实渠道或新一轮调研。

worker 关于 HTTPX 流包装层的经验建议作为候选收录 inbox，尚未晋升为项目规则。`/login`、真实账号/额度和 POSIX 实机验证仍未完成。
