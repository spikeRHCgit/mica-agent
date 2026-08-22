# Mica 第一阶段：工程地基与独立包边界

> 状态：已完成
> 上位约束：`PROJECT_PLAN.md`

## 1. 阶段目标

第一阶段建立独立、可安装、可运行、可测试的 Mica Python 包。本阶段只交付可靠的工程起点，不交付 Agent 业务能力。

## 2. 已交付能力

- 建立标准 `src/mica_agent/` 包边界，Mica 不依赖其他应用源码。
- 根分发项目切换为 `mica-agent`，导入包为 `mica_agent`，终端命令为 `mica`。
- 初始版本为 `0.1.0`，运行时从分发元数据读取同一版本事实源。
- Python 最低版本为 3.11；使用 uv、setuptools、pytest 和 Ruff 建立开发与构建链路。
- 提供 `mica` 与 `python -m mica_agent` 两个公共入口，两者复用同一 CLI 实现。
- 建立 CLI 契约测试、分发产物审计、仓库外隔离安装和无副作用导入验证。
- README 已切换为 Mica 仓库入口，`.gitignore` 已覆盖当前测试、检查和构建产物。

## 3. 稳定契约

### 3.1 包与构建

- `src/mica_agent/` 是 Mica 唯一的 Python 源码包。
- 构建产物只包含 Mica 包、必要分发元数据、源码测试和项目说明。
- Mica 当前没有运行依赖；开发依赖只有 pytest 和 Ruff。
- `uv.lock` 保持可跟踪。

### 3.2 公共入口

```text
mica
mica --help
mica --version
python -m mica_agent
python -m mica_agent --help
python -m mica_agent --version
```

- 无参数和 `--help` 输出帮助，退出码为 0。
- `--version` 严格输出 `mica 0.1.0`，退出码为 0。
- 无效参数不输出正常结果，诊断写入标准错误，退出码为 2。
- 控制台入口与模块入口的标准输出、标准错误和退出码一致。
- 当前不注册不可使用的 `cli`、`tui` 或其他占位子命令。

### 3.3 导入边界

- 安装后的 `mica_agent` 可以在仓库外独立导入和执行。
- 导入不会读取本地配置、访问网络、创建文件、启动子进程或新增后台线程。
- 导入不依赖仓库路径、用户级 Python 包或其他应用源码。

## 4. 验收结论

第一阶段已由 master 在 `main` 上完成全量验收：

- Python 3.11 环境同步成功。
- 17 项 pytest 测试全部通过。
- Ruff 静态检查和格式检查全部通过。
- wheel 与 sdist 离线构建成功。
- wheel 在全新 Python 3.11 环境中无依赖安装成功，并在仓库外通过两个入口的完整行为验证。
- wheel 与 sdist 内容审计通过，只包含 Mica 所需内容。
- Mica 源码边界相对施工前基线保持清晰。
- README 描述与当前真实可用能力一致。

第一阶段验收通过。

## 5. 未包含范围

第一阶段没有实现以下能力：

- OpenAI-compatible Provider、HTTP 请求、模型发现或重试。
- Agent Engine、Run、Turn、流式输出、Tool Calling 或结构化事件。
- 标准消息、工具、权限、取消、会话、JSONL 和上下文压缩。
- 配置文件、环境变量覆盖、首次配置向导和 `AGENTS.md` 加载。
- Textual、TUI、状态栏、斜杠命令和快捷键。
- ExtensionAPI、MCP、Skill、npm 启动器和全局分发。
- 兼容层、旧数据迁移或仓库收口。
- GitHub、PyPI 或 npm 发布。

Mica 在第一阶段结束时仍不能连接模型、进行对话或调用工具。后续阶段需先单独讨论并批准计划，再实现 OpenAI-compatible Provider、Agent Engine 和基础 CLI 对话闭环。
