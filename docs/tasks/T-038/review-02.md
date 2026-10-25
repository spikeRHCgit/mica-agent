# T-038 第 2 轮审查 · 2026-10-25

依据：[任务卡](T-038.md)、[返工 1](rework-01-request.md)、[worker-02.md](worker-02.md)、[validation-02.md](validation-02.md)。

## 审查与证据

- Git 状态与报告一致：保留第 1 轮 model_catalog.py、对应测试和 module_map 改动，本轮新增两份报告；未改存储、契约、依赖、Provider/Engine、App/CLI/TUI 或既有过程文件。主仓与任务分支基线为 21d587d，没有任务以外的产品变化。
- `_request` 在构造私有请求前统一校验实际发送的头值；`_header_value` 要求非空 ASCII 可打印文本，拒绝时使用固定 ConfigError，不携带未信任值。校验先于 `_bounded_fetch`，没有创建客户端或后台任务，也没有宽泛捕获取消。
- 合法头值直接原样返回，不重编码、不删头重试；Go 请求不使用 api_key，因此未使用的非 ASCII Key 不被错误拒绝或发送。没有改变既有凭据存储文本契约。
- 新回归覆盖三个实际认证/会话头入口，观察安全错误、0 请求/响应、未创建 client 和无遗留任务；保留 Go 未使用 Key 的对照及合法头值旧断言。修前 3 failed / 1 passed、修后 4 passed 的证据成立；初次测试事件循环装配错误已单独说明，没有当作产品失败或靠放宽断言解决。
- 采信 worker 最终专项 **271 passed**、全量 **2889 passed / 3 skipped**，全仓 Ruff、格式及 diff check 通过；不机械重跑全量。

master 高风险定点抽查：

```text
uv --directory C:/Users/10601/Desktop/learning/mica-wt/T-038 run --no-sync --offline pytest tests/test_model_catalog.py::test_non_ascii_bearer_key_is_rejected_before_request tests/test_model_catalog.py::test_non_ascii_anthropic_key_is_rejected_before_request tests/test_model_catalog.py::test_non_ascii_go_session_is_rejected_before_request tests/test_model_catalog.py::test_go_does_not_reject_or_send_unused_non_ascii_key tests/test_model_catalog.py::test_deepseek_request_and_explicit_metadata_mapping tests/test_model_catalog.py::test_custom_anthropic_uses_the_explicit_interface_headers tests/test_model_catalog.py::test_opencode_go_is_public_and_sends_mica_session_without_key -q -W error
```

实际结果：**7 passed in 0.51s**。第 1 轮 13 项抽查作为历史证据保留，不写成此次重跑。

## 结论

**验收通过。** T-038 已交付三内置渠道/同源自定义目录的有界可取消发现、明确字段解析、勾选结果合并及完整目标记录 CAS 保存；第 1 轮请求头错误边界缺陷已修复。

本轮没有真实配置、凭据或渠道访问；目录成功不等于 Key 认证、账号资格、额度或推理可用。`/login`、应用/终端维护闭环和 C1 整体仍未交付；POSIX 未实机补跑，不追认为已验收。下一步按配置与选择模块先实现登录维护业务编排，再接应用/终端菜单，不回到 T-036 诊断。

Git 收口结果：master 提交 `e92768a`、ff-only 归并并按 STATUS 已有授权推送；本地 main 与远端 refs/heads/main 精确核对为 `e92768ab41568358b14cfe4e6cc3a0539286893b`，主仓和 T-038 worktree 均干净。下一张 T-039 的派工文档随后仅本地保存，不混为已推送产品。
