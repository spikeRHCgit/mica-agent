# 第 1 轮审查 · 2026-10-15

依据：[`T-033.md`](T-033.md)、[`worker-01.md`](worker-01.md)。

## 审查范围与证据

- Git 核对：main，较 origin/main 领先两个既有草案/派发提交，暂存区为空；工作区仅新增本轮报告，无产品、测试或源码地图改动，与报告一致。
- 独立搜索三适配器默认值并读取 Messages 常量区和请求编码：Completions、Responses 定义公开 `DEFAULT_MAX_TOKENS = 4096`；Messages 只定义默认思考映射，在 `providers/anthropic_messages.py:686` 使用 `"max_tokens": request.max_tokens or 4096`，没有定义或导入任务卡要求的预算常量。
- 核对源码地图及 Messages 专项测试中的预算断言：`tests/test_anthropic_messages_provider.py` 的 `test_request_literal_endpoint_auth_system_schema_budget` 断言显式预算 17，`test_default_effort_mapping_and_minimal_rejection` 断言默认预算 4096。
- 本轮未实现，无实现版本可验收；worker 未执行测试或 Ruff，master 也未补跑机械验证。不将只读检查表述为自动化通过或产品完成。

## 发现与判断

- 受阻成立。任务卡误将 Messages 的字面量默认预算写成已经存在的公开常量，并同时禁止修改适配器，导致 worker 无法按已批准方案施工。根因是 master 编卡前事实核对遗漏，不是 worker 产品实现缺陷。
- worker 只提交报告、未自行提取常量或改用其他预算来源，符合写入范围和前提不符时交回的要求。三接口目标、默认思考映射、显式映射及调用侧测试方案无需因此推翻。
- 推荐最小范围调整：仅授权 `src/mica_agent/providers/anthropic_messages.py` 新增模块级公开 `DEFAULT_MAX_TOKENS = 4096`，并将原预算表达式中的字面量替换为该常量；保留原 `request.max_tokens or ...` 语义，不修改协议、思考、续接、网络、取消或资源行为，不扩大顶层包导出。
- 不推荐在 ConfigService 另写一份 Messages 预算，或借用 Completions 常量冒充 Messages 默认来源；提取现有值可沿用已审查的按接口引用方案，且不形成两个预算事实源。
- 如用户确认，下一轮范围沿用原卡并仅增加上述适配器两处变更及 `worker-02.md` 报告；新增 Messages 专项回归命令，结合原配置/App 链路、最终全量和静态证据验收。已有默认与显式预算断言继续使用字面量 expected，不必为纯常量提取新增重复测试。

## 结论

第一轮受阻，未验收。任务卡和 STATUS 登记为阻塞中，等待用户确认最小范围调整；本审查中的建议尚未授权或派发，不允许 worker 据此越界施工。

用户确认后沿用 T-033，另写下一轮要求与报告入口，不修改第一轮报告或将未执行验证补记为通过。App/CLI 装配、B 第 6 步整体及真实渠道验收仍未完成。
