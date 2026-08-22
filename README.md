# Mica

Mica 是一个面向个人开发者的轻量 Agent 基座，当前正从 GenericAgent 的冻结参考源码中独立重建。

> 当前状态：第三阶段文本对话与持久化会话闭环已具备。`mica cli` 可以连接 OpenAI-compatible 服务，按当前工作区创建、恢复和继续会话；工具、权限、TUI 与扩展系统尚未实现。

## 当前可用能力

- 独立源码包：`src/mica_agent/`。
- 交互式对话：`mica cli` 与 `python -m mica_agent cli`。
- 连接参数通过环境变量提供：

  | 变量 | 含义 |
  |---|---|
  | `MICA_API_URL` | OpenAI-compatible 服务的 base URL 或完整 Chat Completions 端点；base URL 下会自动拼接 `/chat/completions`，`/v1` 只是常见路径示例，并非强制要求 |
  | `MICA_API_KEY` | 服务 API Key |
  | `MICA_MODEL` | 模型名称 |

  示例（Windows PowerShell）：

  ```powershell
  $env:MICA_API_URL = "https://api.example.com/v1"
  $env:MICA_API_KEY = "sk-your-key-here"   # 仅示例占位，请勿使用真实凭据
  $env:MICA_MODEL = "your-model"
  mica cli
  ```

  示例（Bash/zsh）：

  ```bash
  export MICA_API_URL="https://api.example.com/v1"
  export MICA_API_KEY="sk-your-key-here"   # 仅示例占位，请勿使用真实凭据
  export MICA_MODEL="your-model"
  mica cli
  ```

- 支持 OpenAI-compatible Chat Completions 的流式输出（SSE），文本增量实时写出。
- 持久化多轮历史：成功完成的轮次写入当前工作区的 JSONL 会话，后续请求携带完整前文。
- 默认 `mica cli` 创建当前工作区的新会话；`mica cli --continue` 恢复该工作区最后活跃的健康会话；`mica cli --resume` 展示并选择该工作区的健康会话。
- 只有正常完成的一轮才会持久化；失败、取消或流式输出不完整时不会形成半轮记录。
- 分类错误诊断与有限重试：鉴权、限流、超时与临时服务端错误按类别报告，仅对可重试类别重试。
- `Ctrl+C` 可取消进行中的请求并清理退出；EOF 正常结束（Windows 下按 `Ctrl+Z` 再按 `Enter`，Unix/macOS 下按 `Ctrl+D`）。

默认用户数据根目录为 `~/.mica`，会话位于按工作区隔离的路径：
`~/.mica/sessions/<workspace-slug>-<path-hash>/`。工作区移动后视为新的工作区；自动化使用的
数据根目录可以注入，不会依赖真实用户目录。

当前 **没有** 配置文件、工具调用、权限系统、最终 TUI、斜杠命令或扩展系统；也不包含会话命令界面、摘要压缩、云同步或加密能力。它也不保证兼容所有 OpenAI-compatible 服务（协议差异限定在 Provider 适配边界内处理）。

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
- `tests/`：CLI 契约、Provider/Engine/App 测试与分发隔离验证。
- `pyproject.toml`：分发元数据、入口与开发依赖。
- `PROJECT_PLAN.md`：Mica 的正式项目方向和长期架构边界。
- `docs/phase1_plan.md`：第一阶段的范围、验收标准和未包含内容。
- `docs/phase2_plan.md`：第二阶段（Engine、Provider、App、CLI 闭环）的范围与验收标准。
- `docs/phase3_plan.md`：当前阶段（工作区隔离、JSONL 会话、恢复与上下文）的范围与验收标准。

## GenericAgent 参考源码

根目录中的 GenericAgent 旧源码仅作为冻结参考。它不再是根构建项目的一部分，也不保证通过当前安装流程提供旧 `ga` 入口。
