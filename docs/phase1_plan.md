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

## 3. 具体实现

- **包与分发**：`pyproject.toml` 使用 `setuptools.build_meta` 构建，从 `src/` 查找 Python 包；`[project]` 定义 `mica-agent`、版本和 Python 最低版本，`[project.scripts]` 将 `mica` 指向 `mica_agent.cli:main`。源码包与分发项目名分离，因此安装后的代码不依赖仓库工作目录。
- **两个入口共用一处命令解析**：`src/mica_agent/__main__.py` 只调用 `cli.main()`；控制台脚本也指向同一函数。`cli.main()` 通过 `argparse` 处理帮助和参数错误，`--version` 从已安装的 `mica-agent` 分发元数据读取版本，而不是再维护一份版本常量。
- **导入与运行分离**：第一阶段把包导入和 CLI 执行分开，普通导入不启动命令、发起网络请求或创建运行资源。现行 `src/mica_agent/__init__.py` 继续保留这一边界；后续加入的 `AgentEngine`、`MicaApp` 等运行时出口由 `__getattr__` 延迟导入。
- **实现边界的验证**：`tests/test_cli.py` 同时驱动 `mica` 与 `python -m mica_agent`，检查标准输出、标准错误和退出码；`tests/test_distribution.py` 审计构建产物，并在仓库外的隔离环境验证安装后的入口。`uv.lock` 固定开发环境依赖，pytest 与 Ruff 分别验证行为和静态/格式约束。

## 4. 验收结论

第一阶段已通过自动化测试、静态检查、格式检查、离线构建、产物审计和仓库外安装验证；公共入口与无副作用导入行为均已验收。阶段完成不代表后续 Agent 业务能力已经交付。
