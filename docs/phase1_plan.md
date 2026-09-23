# Mica 第一阶段：工程地基与独立包边界

> 状态：已完成
> 上位约束：`docs/PROJECT_PLAN.md`

## 1. 阶段目标

建立独立、可安装、可运行、可测试的 Mica Python 包，为后续 Agent 能力提供可靠的工程起点。

## 2. 已交付能力

- 建立 `src/mica_agent/` 源码包；分发项目名为 `mica-agent`，导入包名为 `mica_agent`。
- 提供 `mica` 和 `python -m mica_agent` 两个共享 CLI 实现的公共入口，包括帮助、版本与参数错误处理。
- 以分发元数据作为版本来源，并建立 Python 3.11、uv、setuptools、pytest、Ruff 和可跟踪的 `uv.lock` 构建验证链路。
- 建立 CLI 契约、无副作用导入、分发产物审计及仓库外隔离安装验证。
- 将 README 和忽略规则调整为 Mica 项目入口及当前开发产物边界。

当前的入口用法、依赖和验证命令以 `README.md` 为准；源码位置以 `docs/module_map.md` 为准。

## 3. 验收结论

第一阶段已通过自动化测试、静态检查、格式检查、离线构建、产物审计和仓库外安装验证；公共入口与无副作用导入行为均已验收。阶段完成不代表后续 Agent 业务能力已经交付。
