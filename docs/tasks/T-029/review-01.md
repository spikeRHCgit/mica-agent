# T-029 第 1 轮审查 · 2026-10-11

依据：[`T-029.md`](T-029.md)、[`worker-01.md`](worker-01.md)

## 审查范围

- 对照 `git status --short` 和 `git diff --stat`：三个源码文件、三个测试文件和本轮汇报，均在写入范围内；源码与测试合计 +1530/-22。
- 通读三个源码文件的实质 diff，核对请求槽位、Finish、来源绑定、原始消息清洗判定、关联检查、单轮上限、工具进度更新、完成回调及渠道编码。对照汇报的测试清单与卡片完成标准。
- 高风险卡按重跑档执行关键命令：`uv run pytest tests/test_provider_contract.py tests/test_openai_provider.py tests/test_engine.py -q -W error` → `337 passed in 5.05s`；`uv run ruff check src/mica_agent tests` → `All checks passed!`；`git diff --check` 无输出，均成功。
- 全量与格式检查采信 worker 的最终证据：`2171 passed, 2 skipped`、`112 files already formatted`。两项跳过为既有 Windows 符号链接权限限制，详见本轮汇报；本轮没有修改跳过条件。未执行真实渠道验收。

## 发现

- 请求新增字段都有默认值且位于末尾，Finish 隐藏续接 repr；输入槽位严格对应全部 assistant 消息，关联检查复用既有契约。Engine 和渠道编码均检查来源、模型、接口和可回放状态。
- Engine 在清洗原始 assistant 工具消息前判定续接，并额外检查原记录关联，避免清洗降级掩盖损坏的工具引用。绑定或关联错误在工具执行前产生固定诊断的 malformed 失败。
- 完成回调只交付本轮槽位，不混入输入历史；下一请求携带历史及本轮槽位。工具状态更新保留续接，单轮超限后同步降级已有进度且后续保持降级，不把多轮历史合并限额。
- Chat Completions 显式按渠道启用，默认关闭。DeepSeek 带 tools 时逐条回传兼容历史的 reasoning_content；OpenRouter 按到达顺序保留 details，不根据 index 重排或合并，回传只编码适用且非 None 的详情字段。
- 流式累计按既有紧凑 JSON 字节数限制，超限后释放累计专属内容，后续不恢复；未知详情降为不可回放，通用正文和工具事实保持。既有协议错误、断流、取消和重试路径没有被专属续接替代。
- 续接没有进入 Engine 对外事件、错误诊断或普通 repr。App、CLI、SessionService 等禁止范围未改，实际渠道装配、历史恢复和完成续接保存仍由第 6 步接入。

## 结论

通过。自动化验证不代替 B 最后一步的用户真实渠道验收。
