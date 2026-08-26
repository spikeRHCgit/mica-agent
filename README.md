# Mica

Mica 是一个面向个人开发者的轻量 Agent 基座。

> 当前状态：基础文字 CLI、六个工作区工具、权限确认、交互式 ask、持久化会话和仓库收口已完成。`mica cli` 可以连接 OpenAI-compatible 服务，按当前工作区创建、恢复和继续会话；第五阶段工具与权限闭环已交付，最终 TUI 仍未实现。

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
- 基础 CLI 已装配六个工具：`read`、`ls`、`write`、`edit`、`bash` 和 `ask`。`read`/`ls` 查看工作区，`write`/`edit` 修改工作区，`bash` 在工作区运行受限 Bash 命令，`ask` 在当前 Run 中暂停并询问用户。
- 可用权限模式：`--permission-mode default`（修改和 Bash 通常询问）、`--permission-mode accept-edits`（自动允许 `write`/`edit`，Bash 仍询问）和 `--permission-mode solo`（自动允许文件工具，Bash 仍询问）。工作区越界、敏感路径和工具硬性边界始终拒绝。
- `write`、`edit` 和 `bash` 可能需要输入 `y`/`yes` 确认；其他回答或空回答拒绝本次调用。`ask` 的回答可以为空，并只作为当前工具结果返回模型。
- 持久化多轮历史：完整成功的轮次写入当前工作区的 JSONL 会话，后续请求携带完整前文和工具历史。
- 默认 `mica cli` 创建当前工作区的新会话；`mica cli --continue` 恢复该工作区最后活跃的健康会话；`mica cli --resume` 展示并选择该工作区的健康会话。
- 当前恢复会话会把旧消息和完整工具历史加入后续模型请求，但基础 CLI 不会在启动时重新显示旧对话；历史回放属于后续完整 TUI。
- 只有正常完成的一轮才会持久化；失败、取消、等待中断或流式输出不完整时不会形成半轮。工具执行后的工作区副作用不会自动回滚；后续失败时请检查工作区。
- 分类错误诊断与有限重试：鉴权、限流、超时与临时服务端错误按类别报告，仅对可重试类别重试。
- `Ctrl+C` 可取消进行中的请求并清理退出；EOF 正常结束（Windows 下按 `Ctrl+Z` 再按 `Enter`，Unix/macOS 下按 `Ctrl+D`）。

默认用户数据根目录为 `~/.mica`，会话位于按工作区隔离的路径：
`~/.mica/sessions/<workspace-slug>-<path-hash>/`。工作区移动后视为新的工作区；自动化使用的
数据根目录可以注入，不会依赖真实用户目录。

工具遵守工作区路径边界、敏感文件拒绝、UTF-8 文件大小和目录输出限制；`edit` 要求旧文本
唯一匹配；Bash 具有清理后的环境、超时、输出限制和取消后的进程树回收，当前优先支持
Windows Git Bash。

当前 **没有** 配置文件、Textual TUI、历史回放、斜杠命令或扩展系统；也不包含会话命令界面、摘要压缩、云同步、加密能力、npm 启动器或正式发布。它也不保证兼容所有 OpenAI-compatible 服务（协议差异限定在 Provider 适配边界内处理）。

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
- `docs/phase3_plan.md`：第三阶段（工作区隔离、JSONL 会话、恢复与上下文）的范围与验收标准。
- `docs/phase4_plan.md`：第四阶段（仓库收口与旧实现清理）的范围与验收标准。
- `docs/phase5_plan.md`：已批准并开始实施的第五阶段（工具执行、权限控制与统一取消）计划。
