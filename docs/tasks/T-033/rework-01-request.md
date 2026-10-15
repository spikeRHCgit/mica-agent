# 返工 1 · 2026-10-15

依据：[`T-033.md`](T-033.md)、[`review-01.md`](review-01.md)。用户于 2026-10-15 确认按最小范围调整继续，本轮汇报写入 [`worker-02.md`](worker-02.md)。

## 审查结果

首轮在实现前受阻，未产生产品或测试改动：Messages 的默认预算是请求编码中的字面量 4096，没有原方案要求的公开 `DEFAULT_MAX_TOKENS`。这是编卡前提遗漏，不是 worker 的实现缺陷；本轮先修正该前提，再完成原卡全部目标。

## 本轮要求

- 在 `src/mica_agent/providers/anthropic_messages.py` 增加模块级公开 `DEFAULT_MAX_TOKENS = 4096`，并将请求编码中的 `"max_tokens": request.max_tokens or 4096` 改为 `"max_tokens": request.max_tokens or DEFAULT_MAX_TOKENS`。仅此两处变更；保留原 `or` 语义、默认数值和显式预算行为，不修改思考、协议、续接、网络、取消、资源行为或顶层导出。
- ConfigService 的固定接口默认值表直接引用该常量及原默认思考映射，与另外两个适配器一致。不在配置层另写 Messages 预算，不借用 Completions 常量作为其预算来源。
- 其余实现、边界和完成标准沿用原卡，完成三接口连接绑定、按实际接口计算有效能力、ConfigService 专项测试、两项 App 未知协议样本迁移及源码地图同步。原卡关于显式映射、来源、偏好降级、选择优先级、不回退、安全诊断和临时 Completions 身份的方案不变；不提前装配 App/CLI。

## 写入范围

本轮以此节覆盖原卡的写入范围冲突：

- `src/mica_agent/config_service.py`、`tests/test_config_service.py`：沿用原卡授权。
- `tests/test_app.py`：仍仅允许原卡指定的两处 `anthropic-messages` → `unknown-protocol` 输入迁移，保留测试名称、参数项、装配及全部原断言。
- `docs/module_map.md`：仍仅同步 ConfigService 及对应专项测试职责。
- `src/mica_agent/providers/anthropic_messages.py`：仅授权上述公开常量定义及原请求预算表达式引用两处变更，这是原卡“禁止修改适配器”的唯一例外。
- `docs/tasks/T-033/worker-02.md`：本轮汇报。不再修改 `worker-01.md`；不得修改任务卡、返工要求、审查、STATUS 或其他 master 管理文件。

Messages 测试文件不在新增写入范围内：已有默认预算 4096 和显式预算 17 的行为断言足以覆盖本次常量提取，继续使用独立字面量 expected，不为纯常量提取增加重复测试。其他禁止范围与环境隔离要求不变；当前 main 串行施工，不建分支、不暂存、提交、推送、stash 或还原。范围外问题先定位并交回，不通过 skip/xfail、删参数或放宽断言绕过。

## 完成标准与验证

- 原卡全部完成标准仍适用；新增确认 Messages 默认预算和显式预算请求行为保持不变，且配置层使用该适配器公开预算常量。
- 保留原卡专项、相关链路、最终全量（含安装态）及静态检查要求；新增 Messages 专项。按稳定版本安排，已有同一最终版本的有效证据不机械重复：
  - `uv run pytest tests/test_anthropic_messages_provider.py -q -W error`
  - `uv run pytest tests/test_config_service.py -q -W error`
  - `uv run pytest tests/test_config_service.py tests/test_app.py -q -W error`
  - `uv run pytest -q -W error`
  - `uv run ruff check src/mica_agent tests`
  - `uv run ruff format --check src/mica_agent tests`
  - `git diff --check`
- 本轮报告列出全部改动路径、实际命令与结果、平台跳过及未完成事项；区分本地配置可用、自动化验证和尚未完成的 App/CLI 装配、B 第 6 步整体及真实渠道验收。无依赖或打包变更，不额外要求构建。
- 测试继续使用隔离目录、受保护假 Key 和模拟网络边界，不读取真实凭据或真实 `~/.mica`，不访问真实渠道；错误、日志和 repr 不得新增 Key、地址或不可信映射目标。
