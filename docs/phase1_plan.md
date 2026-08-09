# Mica 第一阶段计划：工程地基与独立包边界

> 状态：实施中
> 上位约束：`PROJECT_PLAN.md`
> 阶段职责：建立 Mica 可安装、可运行、可测试的新包边界，不实现 Agent 业务能力。

## 1. 阶段目标

第一阶段要把 Mica 从 GenericAgent 的参考源码中独立出来，形成可持续开发的最小工程骨架：

```text
Windows 终端
  -> mica 命令或 python -m mica_agent
  -> argparse 入口
  -> 唯一应用装配入口
  -> 明确输出与退出码
```

阶段结束时必须做到：

1. `src/mica_agent/` 是 Mica 唯一的 Python 源码包。
2. 根目录构建配置正式切换为 `mica-agent`，版本为 `0.1.0`。
3. Mica 可以独立导入、构建、安装和执行，不依赖 GenericAgent 旧模块或旧数据。
4. 控制台入口和模块入口具有一致的帮助、版本与错误语义。
5. uv、pytest、Ruff 和 Python 构建链路可以在 Windows 上无交互运行。
6. 后续 Provider、Engine、会话和前端只能通过唯一应用装配入口接入。

本阶段交付的是可靠的工程起点，不交付可对话的 Agent。

## 2. 已确认决策

- Python 最低版本为 3.11，不设置无必要的最高版本上限。
- Python 环境和构建流程使用 uv。
- Python 构建后端继续使用 setuptools，不在本阶段迁移构建框架。
- 自动测试使用 pytest，格式和静态检查使用 Ruff。
- CLI 参数解析使用标准库 `argparse`。
- Python 分发包、导入包和终端命令分别为 `mica-agent`、`mica_agent` 和 `mica`。
- 初始版本号为 `0.1.0`，版本信息必须只有一个事实源。
- 根目录 `pyproject.toml` 在本阶段切换为 Mica，不并行维护 GenericAgent 构建配置。
- GenericAgent 旧源码继续保留为只读参考，但不再保证通过根目录安装后提供 `ga` 命令。
- 第一阶段不建立 CI 工作流，但所有验证必须可被未来 CI 直接调用。
- 第一阶段不设置覆盖率百分比；每个实际存在的分支和外部行为必须有测试。

## 3. 构建范围

### 3.1 包与构建

- 建立标准 `src` 布局的 `mica_agent` 包。
- 将根目录项目元数据、构建目标和控制台入口切换为 Mica。
- 继续使用 setuptools，不为本阶段引入额外构建框架。
- 建立 uv 开发依赖和锁文件；运行依赖保持为空。
- 生成标准 wheel 和源码包。
- 构建产物只包含 Mica 包及必要元数据，不包含 GenericAgent 根模块、前端、资源和记忆文件。

### 3.2 程序入口

本阶段只提供以下稳定行为：

```text
mica
mica --help
mica --version
python -m mica_agent
python -m mica_agent --help
python -m mica_agent --version
```

行为约束：

- 无参数启动显示帮助并正常退出。
- `--help` 显示帮助并正常退出。
- `--version` 输出 `0.1.0` 并正常退出。
- 无效参数返回非零退出码，诊断信息进入标准错误输出。
- 控制台入口与模块入口不得维护两套解析和分发逻辑。
- 本阶段不注册不可使用的 `cli`、`tui` 或其他占位子命令。

### 3.3 应用装配边界

- CLI 只负责参数解析、终端输出和进程退出码。
- 未来业务组件的创建和连接必须收敛到唯一应用装配入口。
- 第一阶段不要求为了装配概念创建无状态类、空协议或未来目录。
- 当第二阶段出现 Provider 和 Engine 后，再把装配入口具体化为完整 `MicaApp`。
- 不在入口中读取配置、创建会话、访问网络或导入 GenericAgent。

### 3.4 工程文档与忽略规则

- README 只增加 Mica 正在重建、GenericAgent 作为冻结参考和当前开发验证入口等必要说明。
- `.gitignore` 补充 uv、pytest、Ruff、覆盖率和 Python 构建缓存规则。
- `tasks/lessons.md` 保持可跟踪，运行历史、密钥和本地数据保持忽略。
- 不在本阶段创建 `module_map.md`；代码规模尚不足以证明该文档有必要。

## 4. 明确不包含

- OpenAI-compatible Provider、HTTP 请求、模型发现或重试。
- Agent Engine、Run、Turn、流式输出、Tool Calling 或 Engine Event。
- 标准消息、工具、权限、取消、会话、JSONL 和上下文压缩。
- 配置文件、环境变量覆盖、首次配置向导和 `AGENTS.md` 加载。
- Textual、TUI、状态栏、斜杠命令和快捷键。
- ExtensionAPI、MCP、Skill、npm 启动器和全局分发。
- 真实 API Key、外部网络和模型集成测试。
- GenericAgent 兼容层、旧数据迁移和旧源码删除。
- GitHub、PyPI 或 npm 发布。
- 为后续能力预留空接口、空实现或第二套入口。

`httpx` 和 `textual` 等后续运行依赖必须等到对应能力真正实现时再加入。

## 5. 实施任务

| 任务 | 内容 | 状态 |
|---|---|---|
| P1-01 包边界 | 切换项目元数据，建立 `src/mica_agent/`、版本事实源和最小包入口 | 已完成 |
| P1-02 入口闭环 | 建立统一 argparse 解析与控制台/模块入口，落实输出和退出码语义 | 已完成 |
| P1-03 测试与构建 | 建立 pytest、Ruff、wheel/源码包和隔离安装验证 | 已完成 |
| P1-04 文档收口 | 更新必要 README、忽略规则和开发验证说明 | 待开始 |
| P1-05 阶段验收 | master 审查范围、产物、测试、旧源码保护和 Git 状态 | 待开始 |

任务执行顺序：

1. P1-01 先固定包名、目录、版本和构建入口。
2. P1-02 基于已固定的包契约完成入口闭环。
3. P1-03 可以在入口契约稳定后独立实施或审查。
4. P1-04 只记录最终真实行为，不提前描述后续功能。
5. P1-05 由 master 执行，不由实现 worker 自行宣布阶段完成。

## 6. Git 与多 Worker 协作边界

- 所有 worker 必须从包含已批准阶段计划的同一基线开始。
- 一个任务只允许一个 worker 拥有 `pyproject.toml` 和锁文件的最终修改权，避免构建配置并发冲突。
- 其他 worker 可以独立负责测试、构建验证或审查，但不得同时重写入口契约。
- 每个 worker 使用独立分支或 worktree，不在共享分支上直接并发提交。
- worker 不修改 `PROJECT_PLAN.md`、GenericAgent 旧业务源码或未在任务单中授权的文档。
- worker 提交必须保持单一职责，并报告提交号、修改文件、验证结果和剩余风险。
- master 依据实际 Git diff 和测试结果决定合入、返工或拒绝，不只接受文字报告。
- 是否在本阶段启用多个 worker，由用户另行指示；本计划不自动派发并行任务。

受保护的旧实现至少包括：

```text
agent_loop.py
agentmain.py
ga.py
llmcore.py
TMWebDriver.py
simphtml.py
ga_cli/
frontends/
memory/
reflect/
plugins/
assets/
```

只有 `.gitignore`、README 和根构建配置等已明确进入本阶段的共享文件可以按任务单修改。

## 7. 自动化测试安排

### 7.1 CLI 契约测试

- 验证无参数、`--help`、`--version` 和无效参数。
- 验证标准输出、标准错误和退出码。
- 验证控制台入口与 `python -m mica_agent` 行为一致。
- 测试解析与分发行为，不锁定帮助文本的无关空格或 argparse 内部实现。

### 7.2 包与边界测试

- 验证 `mica_agent` 可独立导入。
- 验证所有公开入口读取同一版本事实源。
- 验证 Mica 导入时不读取本地配置、创建文件、访问网络或启动后台任务。
- 验证构建产物不包含 GenericAgent 旧模块和目录。
- 验证安装后的 Mica 不需要仓库根目录出现在 Python 导入路径中。

### 7.3 工程质量检查

阶段质量门禁至少包括：

```text
uv sync
uv run pytest
uv run ruff check src/mica_agent tests
uv run ruff format --check src/mica_agent tests
uv build
```

若实际目录或 uv 命令需要轻微调整，worker 必须在报告中解释；不得通过扩大 Ruff 忽略范围让错误静默通过。

### 7.4 隔离安装测试

在临时环境中完成以下闭环：

1. 构建 wheel。
2. 创建不继承项目依赖的全新 Python 环境。
3. 只安装刚构建的 wheel。
4. 切换到仓库之外的工作目录。
5. 执行 `mica --help`、`mica --version` 和模块入口。
6. 确认命令不依赖旧 `ga` 脚本、仓库相对路径或未声明依赖。

临时环境和构建产物必须位于可安全清理的位置，不写入用户全局 Python 环境。

## 8. 达标标准

以下条件必须全部满足：

- `mica_agent` 可以独立导入、构建、安装和执行。
- 版本、帮助、错误输出和退出码符合本计划约定。
- wheel 与源码包构建成功，隔离安装测试成功。
- pytest、Ruff 检查和 Ruff 格式验证全部通过。
- 默认验证不需要网络、API Key、模型输出或人工输入。
- Mica 运行时依赖为空，开发依赖只包含当前质量链路所需工具。
- 构建产物不包含 GenericAgent 旧代码、前端、资源、记忆或本地数据。
- 新核心不导入、读取或修改 GenericAgent 旧实现和旧数据。
- GenericAgent 受保护文件相对施工前基线没有业务代码改动。
- README 与真实可用入口一致，不宣称尚未交付的功能。
- 没有占位命令、空扩展点或未使用依赖。
- master 已审查实际 diff、构建产物和验证输出。

任何一项失败都不能将阶段状态标记为完成。

## 9. 风险与处理原则

| 风险 | 处理原则 |
|---|---|
| 根构建配置切换后旧 `ga` 安装入口失效 | 这是已确认的不兼容重建边界；旧源码保留，但不维护并行打包 |
| setuptools 自动收集旧根模块 | 使用显式 `src` 包发现，并检查 wheel 内容和隔离安装结果 |
| 入口为了未来功能提前膨胀 | 第一阶段只保留帮助、版本、错误和装配责任边界 |
| 测试只在仓库目录内通过 | 必须执行仓库外的隔离安装测试 |
| 多 worker 同时修改共享配置 | `pyproject.toml`、锁文件和入口契约实行单一任务所有权 |
| README 与施工状态不一致 | 文档只描述已经通过验收的行为，不写未来完成式 |

遇到需要新增运行依赖、改变已确认入口语义、修改旧业务源码或扩大阶段范围时，worker 必须停止并交由 master 与用户确认。

## 10. 阶段完成后的状态

第一阶段完成后，Mica 仍不能调用模型或进行对话。第二阶段将在本阶段稳定包边界之上，讨论并实现 OpenAI-compatible Provider、Agent Engine、结构化事件和基础 CLI 对话闭环。

阶段验收后，master 应把本文件压缩为已交付能力、稳定契约、验收结论和未包含范围，不保留 worker 报告流水。
