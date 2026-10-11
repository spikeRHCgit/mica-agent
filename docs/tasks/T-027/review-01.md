# T-027 第 1 轮审查 · 2026-10-11

依据：[`T-027.md`](T-027.md)、[`worker-01.md`](worker-01.md)

## 审查范围

- 改动对照：`git status --short` 只有写入范围内的五个文件；`git diff --stat` 与汇报一致（`message_sanitizer.py` +48/-1、`module_map.md` +2，另有两份新建文件和汇报）。
- 判断审查：通读 `continuation_contracts.py` 全文和 `message_sanitizer.py` 的 diff；对照计划「续接与 v6 wire（设计审查稿）」逐条核对项白名单、接口归属、状态与项数、`text` 唯一、`call_id` 唯一、ID 和不透明字段约束、`reasoning_details` 类型规则、关联规则、单轮上限和清洗判定；浏览新测试的用例清单（18 个测试函数，参数化后 126 项）。
- 补充验证：卡片标为高风险，按「重跑」档在 main 工作区重跑卡片指定的三条命令：pytest 结果为 `271 passed, 1 skipped`，ruff check 结果为 `All checks passed!`，format check 结果为 `112 files already formatted`，均与汇报一致。

## 发现

- 契约与计划一致：wire 字段齐全，缺省写 `null`；解析拒绝多余和缺失字段、未知 `type`、未知枚举；构造期和解析期的错误只带字段名和原因，`ContinuationFormatError` 用 `from None` 截断异常链；内容字段在 repr 中隐藏。
- 清洗判定按卡片实现：工具消息清洗会改动的步骤和明文字段需要清洗的步骤，都改为不可回放；不透明字段不做替换。`sanitize_tool_message` 不改动正文，持久化的正文与原始正文一致，所以只比较清洗前后是否相等，就足以保证回放前缀不被改动。
- 关联检查对不可回放记录和 Completions 记录直接返回，这与计划「Completions 不使用引用项」一致。
- 既有清洗函数未改，两个既有测试文件未改；新模块只被清洗模块和新测试引用，没有越界接入，也没有改顶层导出。
- 汇报中的路径疑点：汇报写的两个路径一模一样，看起来是 worker 在 Windows 环境里换算路径时产生的表述，与施工位置无关，不影响结论。
- 小问题：`_freeze_sequence` 缺少返回类型注解；不影响行为，留待后续卡顺手处理，本轮不返工。

## 结论

通过。
