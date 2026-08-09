# Mica

Mica 是一个面向个人开发者的轻量 Agent 基座，当前正从 GenericAgent 的冻结参考源码中独立重建。

> 当前状态：第一阶段工程地基已完成。当前交付的是可安装、可验证的最小 Python 包，而不是可对话的 Agent。

## 当前可用能力

- 独立源码包：`src/mica_agent/`。
- 终端入口：`mica` 与 `python -m mica_agent`。
- 基础 CLI 行为：无参数或 `--help` 显示帮助，`--version` 输出版本；无效参数返回错误退出码并写入标准错误输出。
- 工程验证：pytest、Ruff、wheel、sdist，以及从构建 wheel 完成的隔离安装验证。

当前 **不能** 连接模型、进行对话或调用工具；也没有配置、会话、TUI 或扩展功能。

## 开发前置条件

- Python 3.11
- [uv](https://docs.astral.sh/uv/)

## 开发与验证

在仓库根目录执行：

```bash
uv sync --python 3.11
uv run mica
uv run mica --version
uv run pytest
uv run ruff check src/mica_agent tests
uv run ruff format --check src/mica_agent tests
uv build --offline
```

`uv run mica` 与 `uv run python -m mica_agent` 使用同一 CLI 逻辑。构建产物写入被 Git 忽略的 `dist/`。

## 目录与文档入口

- `src/mica_agent/`：当前 Mica 的唯一 Python 源码包。
- `tests/`：CLI 契约与分发隔离验证。
- `pyproject.toml`：分发元数据、入口与开发依赖。
- `PROJECT_PLAN.md`：Mica 的正式项目方向和长期架构边界。
- `docs/phase1_plan.md`：第一阶段的范围、验收标准和未包含内容。

## GenericAgent 参考源码

根目录中的 GenericAgent 旧源码仅作为冻结参考。它不再是根构建项目的一部分，也不保证通过当前安装流程提供旧 `ga` 入口。
