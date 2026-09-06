# Mica

Mica 是一个面向个人开发者的轻量 Agent 基座。

> 当前状态：Textual TUI、基础文字降级 CLI、五个内置工具、权限确认、交互式 ask、持久化会话和仓库收口已完成。`mica cli` 默认启动 Textual TUI；`mica cli --plain` 使用基础文字界面。两种入口都按当前工作区创建、恢复和继续会话。

## 当前可用能力

- 独立源码包：`src/mica_agent/`。
- 交互式对话：`mica cli` 与 `python -m mica_agent cli`；默认启动 Textual TUI。
- 基础文字降级入口：`mica cli --plain` 与 `python -m mica_agent cli --plain`。
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

- 支持 OpenAI-compatible Chat Completions 的流式输出（SSE），文本增量实时写出；基础 CLI 也会实时显示经过清洗的 Bash 输出。
- 基础 CLI 已装配五个工具：`read`、`write`、`edit`、`bash` 和 `ask`。Mica 不再提供独立 `ls`；目录查看通过 `bash` 完成。`read` 读取工作区文件，`write`/`edit` 修改工作区，`ask` 在当前 Run 中暂停并询问用户。
- 权限模式只保留 `--permission-mode default` 和 `--permission-mode solo`，旧 `accept-edits` / `accept_edits` 参数会被拒绝。default 自动允许普通文件操作、日常 Bash 和普通网络访问；受保护文件、危险命令、无法可靠分类的 Shell 结构及自定义工具需要一次确认。solo 跳过权限策略，但保留参数校验、工具失败、取消和 ask 需求交互。new/continue/resume 使用相同装配。
- `read`、`write`、`edit`、`bash` 和自定义工具可能需要输入 ASCII `y`/`yes` 确认；`n`/`no` 或空回答拒绝本次调用，其他输入会提示后重新询问。`ask` 的回答可以为空，并只作为当前工具结果返回模型；结构化 ask 会在 stderr 显示带编号的 2—4 个选项，输入选项编号提交该选项，允许自定义回答时还会显示 `Type something.` 入口。
- 持久化多轮历史：完整成功的轮次写入当前工作区的 JSONL 会话，后续请求携带完整前文和工具历史。
- 默认 `mica cli` 创建当前工作区的新会话；`mica cli --continue` 恢复该工作区最后活跃的健康会话；`mica cli --resume` 展示并选择该工作区的健康会话。以上参数均可与 `--plain` 组合，例如 `mica cli --continue --plain`。
- TUI 和基础文字入口共享同一套会话选择、启动上下文、系统提示词、工具、权限模式和 Provider 装配；TUI 会直接显示已选会话的历史，但不会重放历史工具或重新执行历史 Run。
- 只有正常完成的一轮才会持久化；失败、取消、等待中断或流式输出不完整时不会形成半轮。工具执行后的工作区副作用不会自动回滚；后续失败时请检查工作区。
- 分类错误诊断与有限重试：鉴权、限流、超时与临时服务端错误按类别报告，仅对可重试类别重试。
- `Ctrl+C` 可取消进行中的请求并清理退出；EOF 正常结束（Windows 下按 `Ctrl+Z` 再按 `Enter`，Unix/macOS 下按 `Ctrl+D`）。

默认用户数据根目录为 `~/.mica`，会话位于按工作区隔离的路径：
`~/.mica/sessions/<workspace-slug>-<path-hash>/`。工作区移动后视为新的工作区；自动化使用的
数据根目录可以注入，不会依赖真实用户目录。

文件工具仍只支持工作区相对路径、普通 UTF-8 文件和大小限制，不支持父跳转、链接跟随或设备；敏感文件由 ToolExecutor 授权，default 批准后可实际访问，solo 不询问。`edit` 要求旧文本
唯一匹配；Bash 具有清理后的环境、超时、输出限制和取消后的进程树回收，当前优先支持
Windows Git Bash。

命令分析仅提供有限防误操作提示：识别删除、格式化、提权、权限修改、危险 Git 操作、
内联解释器和组合命令；解析失败、动态扩展、未知包装语义和工作目录变化保守询问。
普通脚本和未知普通程序不读取内容审查，“未命中规则”不代表安全。
**没有 OS 级文件或网络沙箱**：工作区 cwd 不是隔离，Bash 仍可访问工作区外；
规则不能保证拦截间接副作用、编码执行或数据外传。

当前 **没有** 配置文件、斜杠命令或扩展系统；也不包含会话命令界面、摘要压缩、云同步、加密能力、npm 启动器或正式发布。它也不保证兼容所有 OpenAI-compatible 服务（协议差异限定在 Provider 适配边界内处理）。

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
- `docs/module_map.md`：按单个源码文件说明职责的技术地图，用于快速定位修改入口。
- `docs/tui_design.md`：已经确认的 Textual TUI 交互、视觉和动态呈现规则。
- `docs/phase1_plan.md`：第一阶段的范围、验收标准和未包含内容。
- `docs/phase2_plan.md`：第二阶段（Engine、Provider、App、CLI 闭环）的范围与验收标准。
- `docs/phase3_plan.md`：第三阶段（工作区隔离、JSONL 会话、恢复与上下文）的范围与验收标准。
- `docs/phase4_plan.md`：第四阶段（仓库收口与旧实现清理）的范围与验收标准。
- `docs/phase5_plan.md`：已完成并通过验收的第五阶段（工具执行、权限控制与统一取消）计划。
