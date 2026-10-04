# Mica

Mica 是一个面向个人开发者的轻量 Agent 基座。

> 当前能力：主缓冲区终端前端、基础文字 CLI、五个内置工具、权限确认、交互式 ask、持久化会话和中断恢复；支持手工 JSON 配置启动与模型选择恢复。`mica cli` 默认启动终端界面，无模型配置也能进入并保留草稿；`mica cli --plain` 在无可用连接时输出诊断并退出 2。

## 当前可用能力

- 独立源码包：`src/mica_agent/`。
- 交互式对话：`mica cli` 与 `python -m mica_agent cli`；默认启动主缓冲区终端界面。
- 基础文字降级入口：`mica cli --plain` 与 `python -m mica_agent cli --plain`。
- 临时连接仍可通过环境变量提供（不自动保存 Key 或会话选择）：

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
- 终端前端和基础文字入口共享同一套会话选择、启动上下文、系统提示词、工具、权限模式和 Provider 装配；终端前端会直接显示已选会话的历史，但不会重放历史工具或重新执行历史 Run。
- 正常完成的一轮持久化为完整 `TurnEntry`；主动取消或可恢复 Provider 失败会另存安全的中断检查点，保留原始用户指令、已完成工具结果和真实部分进度，后续请求或重启恢复时可继续，但不会伪造最终助手回复、未执行工具结果或成功终态。工具执行后的工作区副作用不会自动回滚；中断或失败后仍应检查工作区。
- 分类错误诊断与有限重试：鉴权、限流、超时与临时服务端错误按类别报告，仅对可重试类别重试。
- 默认主缓冲区中，`Esc` 取消进行中的 Run；一次明确短按 `Ctrl+C` 只取消或布防退出，约两秒内第二次明确短按才退出，空闲且草稿为空时 `Ctrl+D` 也可退出。锁定的 Windows 输入 API 无法区分延迟自动重复与新的独立按键，因此不要持续按住 `Ctrl+C`。

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

当前支持手工配置文件，但 **没有** `/model`、`/login`、`/settings`、`/reload`、会话内连接切换或扩展系统；也不包含会话命令界面、摘要压缩、云同步、加密能力、npm 启动器或正式发布。仅装配 `openai-completions`，其他协议可记录但不可执行；不保证兼容所有远端服务。

## 手工配置

用户数据根目录中的三份 UTF-8 JSON 各有职责；工作区只读取 `<workspace>/.mica/config.json`，不得存 Key。配置文件缺失允许未配置启动；语法损坏、重复键、不可读文件阻止启动，诊断不回显原值。

`~/.mica/models.json` 定义稳定供应商 ID、连接与模型，以下地址和模型只是占位示例：

```json
{
  "providers": {
    "local": {
      "name": "My connection",
      "channel": "custom",
      "enabled": true,
      "baseUrl": "https://example.test/v1",
      "api": "openai-completions",
      "models": [{"id": "chat", "maxTokens": 4096, "reasoning": false}]
    }
  }
}
```

`~/.mica/auth.json` 单独绑定凭据，以下 Key 是虚构样本：

```json
{"providers":{"local":{"type":"api_key","apiKey":"FICTIONAL_TEST_ONLY_KEY"}}}
```

必须先限制认证文件和父目录权限。Linux/macOS 文件只允许所有者本人访问（例如 `chmod 600 ~/.mica/auth.json`），父目录必须属于当前用户且其他用户无写入权（例如 `chmod 700 ~/.mica`）。Windows 要实际限制 owner/DACL：文件和父目录 owner 必须是当前用户；文件只允许当前用户、SYSTEM 和 Administrators 访问，父目录不得授予其他账号创建/删除子文件、修改权限或取得所有权的能力。文件与目录属性的“安全”页可禁用继承、移除宽泛授权、保留本人权限；程序读取前查询真实 ACL，不以 `chmod` 代替，并拒绝目录链接/reparse。保护无法证明安全时不读取 auth，给出固定警告并禁用该认证来源，独立环境/直接连接仍可工作；保存选择指向不可用凭据时保留引用，不回落其他来源。实际读取失败或 JSON 损坏仍阻止启动。认证本轮只读，不自动修复 ACL。

`~/.mica/config.json` 保存默认选择和共享设置；同形的工作区文件优先：

```json
{"defaultModel":{"provider":"local","model":"chat"},"reasoningEffort":"off","permissionMode":"default","reasoningDisplay":"collapsed"}
```

启动选择优先级为显式 `--provider`/`--model`、已保存恢复选择、完整环境临时连接、工作区默认、全局默认。两个 CLI 选择参数须同时提供且各出现一次，例如 `mica cli --plain --provider local --model chat`；只影响本次进程，不改已保存选择。`--permission-mode` 省略时使用共享设置，显式提供时在本次进程持续覆盖。

新会话实际采用可用配置默认时保存稳定 ID；环境/显式 CLI 连接不自动保存。恢复失效、删除或禁用的引用时保留引用并要求修正，不换成另一供应商。旧 v1～v4 会话只读字节不变，首次业务写入原子升级到 v5；成功、中断、重命名、分支、回退和清空都保留已有选择。

每个 Run 冻结模型能力、输出预算、思考与权限；下一 Run 重读共享设置和能力。启动连接 URL/Key 改动需重启，不能在一轮中换连接。未声明能力时按文本输入、不支持思考、估计上下文 200000、适配器输出预算 4096 处理；这些不是远端可用性保证。模型声明支持思考后，可用 `thinkingLevelMap` 映射档位；不支持的偏好有效值为 off，保留偏好并提示。明确输出预算与明确上下文容量冲突会拒绝提交，不删历史或压缩。

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

测试分组：`slow` 标记必须保留墙钟观测的四项后代进程测试；`process_tree` 标记这四项真实 OS
进程树清理证据，不表示仅限 Windows。默认 `uv run pytest` 仍运行全部测试，不排除任何分组。

```bash
uv run pytest -m "not slow"                  # 快速反馈，不代替最终全量验收
uv run pytest -m process_tree                # 单独运行真实进程树证据
uv run pytest -q -W error --strict-markers    # 最终全量，包含上述四项
```

`uv run mica` 与 `uv run python -m mica_agent` 使用同一 CLI 逻辑。构建产物写入被 Git 忽略的 `dist/`。

## 目录与文档入口

- `src/mica_agent/`：当前 Mica 的唯一 Python 源码包。
- `tests/`：CLI 契约、Provider/Engine/App 测试与分发隔离验证。
- `pyproject.toml`：分发元数据、入口与开发依赖。
- `AGENTS.md`：所有 agent 会话的项目入口，包含定位、文档路由和通用工程原则。
- `docs/PROJECT_PLAN.md`：Mica 的正式项目方向和长期架构边界。
- `docs/STATUS.md`：当前开发主线、进行中任务和待决策。
- `docs/configuration_spec.md`：配置与模型的完整规格；手工配置和恢复已实现，后续菜单、登录和新协议仍以阶段交付为准。
- `docs/module_map.md`：按功能分组定位全部现有源码和测试文件。
- `docs/code_spec.md`：已定稿的产品代码、测试与验证、注释编写规范。
- `docs/decisions.md`：已确认的项目决策及其理由。
- `docs/tui_design.md`：已确认的终端前端交互、视觉和动态呈现规则。
- `docs/phase1_plan.md` 至 `docs/phase5_plan.md`：已完成阶段的交付与验收结论。
