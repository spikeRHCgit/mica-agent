# T-028 第 1 轮审查 · 2026-10-11

依据：[`T-028.md`](T-028.md)、[`worker-01.md`](worker-01.md)

## 审查范围

- 改动对照：`git status --short` 只有写入范围内的 11 个文件和汇报；`git diff --stat` 为 +813/-56。六个受限测试文件和 `continuation_contracts.py` 逐行看过 diff；`test_jsonl_store.py`、`test_session_contracts.py` 删掉的行逐条看过。
- 判断审查：通读 `TurnEntry`、`RunCheckpointProgress`、`RunCheckpoint` 的新增校验，以及 `session_contracts.py` 中 turn、progress、checkpoint 的 v6 读写分支；对照计划「v6 会话记录」和卡片完成标准，核对 v6 规范字节测试、v5 固定样本测试和负面测试清单。
- 补充验证：卡片标为高风险，按「重跑」档在 main 工作区重跑卡片指定的三条命令：pytest 结果为 `2091 passed, 2 skipped`，跳过的两项都是 Windows 缺少创建符号链接的权限（`test_tool_presentation.py:273`、`test_workspace_paths.py:69`）；ruff check 结果为 `All checks passed!`；format check 结果为 `112 files already formatted`。均与汇报一致。

## 发现

- 版本与兼容：当前版本为 6，支持 1～6；selection 的条件改为版本至少为 5；检查点允许 4、5、6。v3～v5 的 turn 和 v4、v5 的 progress 仍然只接受原有字段，出现续接字段按多余字段拒绝（包括值为 `null`）。
- 构造：续接按 `context_messages` 中 assistant 消息的顺序一一对应，空元组补成全 `None`；判定在中间消息清洗之前进行，用的是传入的原始消息。检查点的每一步还原成 assistant 消息后判定，全部步骤合计也受 1 MiB 上限约束；两处都不自动降级。docstring 写明了契约只兜底、由 Engine 用原始消息判定。
- 读取：续接相关的失败都转成不带原值的 `SessionFormatError`；测试对 turn 和 checkpoint 两种对象分别覆盖缺失、类型错误、未知项、多余字段、引用顺序、缺少正文引用、超出上限，并断言异常文本不含续接内容。
- 测试改动：六个受限文件只改了「当前版本」的断言；两个主测试文件删掉的行也只有当前版本断言，以及因新增导入而重排的 import 行。v1～v4 固定样本的字面量没有改动，读入旧版本后保留原版本号的断言仍在。
- 两处偏离可以接受：非 tuple 的续接字段抛 `ValueError`，卡片原文在这里有歧义，worker 加了 `noqa: TRY004` 并写明原因；测试里用作「未知版本」的样本从 6 改为 7，这是升版本的必要改动。
- T-027 审查遗留的 `_freeze_sequence` 返回类型注解已补上。

## 结论

通过。
