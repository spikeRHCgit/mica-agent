# GenericAgent / Mica 项目指南

## 1. 文档职责

本文件是项目级 Agent 导航和工作规则。开始处理项目问题前先读本文件，再根据任务路由到具体源码。

- Mica 正式产品方向、范围和架构边界：`PROJECT_PLAN.md`
- Mica 历史讨论与决策来源：`docs/refactor_draft.md`，只在追溯决定时读取，不作为实施依据
- 项目级可复用经验：`tasks/lessons.md`
- 项目介绍和安装概览：`README.md`
- Claude Code 兼容说明：`CLAUDE.md`，除非用户要求，否则不要同步修改

不要在本文件记录逐次学习进度，也不要把源码细节大段复制到本文件。

Mica 正式重建采用 cAc：当前 Codex 是 master，负责项目计划、阶段边界、worker 任务单、审查和文档收口；外部 Claude Code 是 worker，默认不修改 `PROJECT_PLAN.md` 或自行决定产品方向。具体阶段开始前再读取对应阶段计划；尚未建立阶段计划时不得从草案自行推断施工范围。

## 2. 项目定位

当前仓库包含作为参考的 GenericAgent 旧实现，并将在独立 `src/mica_agent/` 中重建 Mica。处理旧源码问题时使用本文件后续的 GenericAgent 路由；处理 Mica 规划、实现或审查时以 `PROJECT_PLAN.md` 为准。

GenericAgent 是一个 Python 实现的通用 Agent 框架。它把以下能力组合成可循环执行的系统：

1. 接收来自 CLI、TUI、Web、桌面端或聊天平台的用户任务。
2. 通过统一 Client 接口调用不同供应商的 LLM。
3. 向 LLM 提供工具 Schema，并解析模型返回的工具调用。
4. 将工具名动态分发到本地 Python 方法。
5. 把工具结果和工作记忆送回 LLM，持续循环直到完成、退出或达到轮数上限。

项目的核心不是某个前端，而是 `agentmain.py`、`agent_loop.py`、`ga.py` 和 `llmcore.py` 组成的运行链。

## 3. 核心调用链

```text
前端或命令行
  -> GenericAgent.put_task()
  -> task_queue
  -> GenericAgent.run()
  -> 创建 GenericAgentHandler
  -> agent_runner_loop()
  -> client.chat(messages, tools)
  -> LLM 返回文本和 tool_calls
  -> BaseHandler.dispatch()
  -> GenericAgentHandler.do_<tool_name>()
  -> StepOutcome(data, next_prompt, should_exit)
  -> 下一轮 LLM 调用，或结束任务
  -> display_queue
  -> 前端显示流式结果和最终结果
```

分析具体问题时，不要默认整条链都有问题。先确定问题属于输入、编排、循环、模型适配、工具执行还是展示层。

## 4. 核心接口契约

### 任务输入与输出

- `GenericAgent.put_task()` 把任务放入共享 `task_queue`，并返回该任务独立的 `display_queue`。
- `GenericAgent.run()` 在后台持续消费任务。
- `display_queue` 中的 `next` 表示中间流式内容，`done` 表示任务结束。

### LLM 调用

- Agent Loop 调用 `client.chat(messages=..., tools=...)`。
- `chat()` 是生成器：迭代阶段产出文本片段，生成器返回值是统一响应对象。
- Agent Loop 依赖响应对象的 `content` 和 `tool_calls`。
- 不同供应商格式的转换和流式解析由 `llmcore.py` 负责。

### 工具注册与分发

- LLM 可见的工具名称、描述和参数位于 `assets/tools_schema*.json`。
- 工具 `name` 必须对应 `GenericAgentHandler.do_<name>()`。
- `BaseHandler.dispatch()` 根据工具名动态查找并调用对应方法。
- 新增或重命名工具时，必须同时检查 Schema、Handler 方法和相关提示词。

### 工具结果

- 工具方法统一返回 `StepOutcome`。
- `data` 是工具执行结果。
- `next_prompt` 控制是否继续下一轮；无下一轮提示通常表示当前任务完成。
- `should_exit` 表示主动退出当前循环，例如等待用户输入。

## 5. 根目录主要文件

| 路径 | 职责 | 何时读取 |
|---|---|---|
| `agent_loop.py` | Agent 核心循环、工具分发基类、`StepOutcome`、生成器辅助逻辑 | 循环、退出条件、工具分发、流式执行异常 |
| `agentmain.py` | `GenericAgent` 编排、任务队列、LLM Client 管理、系统提示词组装、CLI/task/reflect 模式 | 输入如何进入、并发、队列、模型切换、启动模式 |
| `ga.py` | `GenericAgentHandler` 和本地原子工具实现 | 文件、代码、浏览器、询问用户、工作记忆等工具问题 |
| `llmcore.py` | 多供应商 LLM Session/Client、流式协议、Tool Calling 解析和格式转换 | API 请求、模型兼容、消息历史、工具调用解析、流式错误 |
| `TMWebDriver.py` | 基于浏览器桥接和 CDP 的 Chrome 控制 | 标签页连接、JavaScript 执行、浏览器通信问题 |
| `simphtml.py` | 网页 HTML 清理、压缩和结构化 | `web_scan` 内容缺失、页面简化或 token 压缩问题 |
| `pyproject.toml` | Python 版本、核心依赖、可选前端依赖、`ga` 命令入口 | 安装、打包、依赖和 Python 版本问题 |
| `README.md` | 项目定位、安装方式和面向使用者的概览 | 首次了解项目或核对公开使用方式 |
| `launch.pyw` | Windows 桌面启动和 Streamlit/WebView 组合 | Windows GUI 启动问题 |
| `hub.pyw` | Windows 服务发现与启动管理界面 | 多服务启动器或桌面 Hub 问题 |

## 6. 目录结构

### `assets/`

运行时配置和提示资源，不是普通静态图片目录。

- `sys_prompt*.txt`：Agent 系统行为规则。
- `tools_schema*.json`：提供给 LLM 的工具定义。
- `global_mem_insight_template*.txt`、`insight_fixed_structure*.txt`：记忆模板和固定结构。
- `code_run_header.py`：代码执行工具使用的辅助头部。
- `tmwd_cdp_bridge/`：浏览器扩展和 CDP 桥接资源。
- 其他脚本只在对应功能被追踪时读取。

### `ga_cli/`

安装后的 `ga` 命令入口和前端启动调度。命令解析问题先读 `ga_cli/cli.py`，不要先读具体 UI。

### `frontends/`

包含 TUI、Streamlit、桌面、ACP 以及多个聊天平台接入。它们共享 `GenericAgent`，但各自处理平台事件、消息格式和展示。

- 通用聊天平台逻辑优先读 `frontends/chatapp_common.py`。
- TUI 问题读 `frontends/tuiapp.py` 或实际启用的 `tuiapp_v2.py`。
- Streamlit 问题读 `frontends/stapp.py` 或实际启用的 `stapp2.py`。
- 桌面桥接问题读 `frontends/desktop_bridge.py` 和 `frontends/desktop/`。
- 某个平台的问题只读对应的 `*app.py`，不要遍历全部前端。
- 多 Agent 编排界面和服务端逻辑位于 `frontends/conductor.py`。

### `memory/`

该目录同时包含 SOP 文档、长期记忆相关文件和可被 Agent 调用的辅助脚本，不是一个单一 Python 包。

- 记忆写入规则先读 `memory/memory_management_sop.md`。
- 清理问题读 `memory/memory_cleanup_sop.md`。
- 某项能力的 SOP 只在任务明确相关时读取。
- `L4_raw_sessions/` 用于原始会话层相关处理。
- ADB、OCR、UI 检测等 `.py` 文件是按需辅助工具，不属于核心 Agent Loop。

### `reflect/`

自动触发和扩展运行模式，包括定时、目标模式、自主模式和团队 Worker。只有任务涉及监控、调度、自治或多 Agent 时才读取。

### `plugins/`

可选观测或集成功能。当前可见的 Langfuse 插件用于调用链追踪；核心 Agent 运行逻辑不依赖先理解插件。

## 7. 问题到文件的读取路由

| 问题或任务 | 第一批读取 | 需要时继续读取 |
|---|---|---|
| Agent 为什么继续、完成或退出 | `agent_loop.py` | `ga.py` 中对应工具方法 |
| 工具名如何找到实现 | `agent_loop.py`、`ga.py` | `assets/tools_schema*.json` |
| 新增或修改工具 | `assets/tools_schema*.json`、`ga.py` | `agent_loop.py`、`assets/sys_prompt*.txt` |
| 用户输入为什么没有执行 | 对应前端、`agentmain.py` | `agent_loop.py` |
| 流式内容没有显示或重复 | 对应前端、`agentmain.py` | `llmcore.py` |
| 模型请求、鉴权或供应商兼容 | `llmcore.py` | `agentmain.py`、本地密钥配置 |
| Tool Calling 解析失败 | `llmcore.py` | `assets/tools_schema*.json`、模型原始日志 |
| 系统行为或提示词约束异常 | `assets/sys_prompt*.txt` | `llmcore.py`、`ga.py` 回调逻辑 |
| 文件或终端工具异常 | `ga.py` 对应函数和 `do_xxx` | `assets/code_run_header.py` |
| `web_scan` 内容不准确 | `ga.py`、`simphtml.py` | `TMWebDriver.py` |
| 浏览器连接或 JS 执行失败 | `ga.py`、`TMWebDriver.py` | `assets/tmwd_cdp_bridge/` |
| 工作记忆或长期记忆异常 | `ga.py` 记忆相关方法 | `memory/` 中对应 SOP/文件 |
| CLI 命令或前端启动异常 | `ga_cli/cli.py`、`pyproject.toml` | 被启动的前端文件 |
| `--task` 或 `--reflect` 异常 | `agentmain.py` 主入口 | `reflect/` 中对应脚本 |
| Windows GUI 启动问题 | `launch.pyw` 或 `hub.pyw` | 对应前端、进程和端口逻辑 |

## 8. 最小阅读规则

1. 先根据上表定位所属层，再读取最小相关函数及其直接调用者和被调用者。
2. `llmcore.py` 较大，必须先定位具体 Session、Client 或解析函数，不要默认通读全文件。
3. `frontends/` 实现较多，只选择用户实际使用的前端，并用一个代表实现理解公共模式。
4. `memory/` 包含大量按需 SOP，不要为了了解记忆系统一次性读取全部文件。
5. README 和文档用于导航，行为结论最终以当前源码和配置为准。
6. 涉及动态加载、配置项或运行生成文件时，先确认当前环境，不凭文件名推断。

## 9. 修改规则

1. 默认最小修改，不顺手重构邻近模块。
2. 修改共享契约前，检查调用者和实现者两侧。
3. 修改工具时检查 Schema 名称、参数、`do_xxx` 方法、返回的 `StepOutcome` 和提示词影响。
4. 修改 `agent_runner_loop()`、`GenericAgent.run()` 或 Client 接口属于高影响变更，必须扩大验证范围。
5. 保留用户已有改动；工作区状态不明确时先做只读检查。
6. 教学任务默认不修改业务源码，只有用户明确要求实验或实现时才写代码。
7. 当前项目初步勘察未发现独立测试目录；验证应结合静态检查、最小脚本、目标入口和实际交互路径。

## 10. 学习协作规则

1. 教学前先确认用户指定的当前文件和学习阶段；没有独立学习计划时不得自行推断进度锚点。
2. 一次讲清一个小知识块：文件职责、真实代码、具体执行路径，然后暂停确认。
3. 禁止脱离当前项目代码空讲理论；所有概念必须落到具体文件、符号和运行行为。
4. 每个知识块都要分析当前设计的原因、至少一种替代方案和相应取舍。
5. 设计意图有文档或注释证据时标为事实；否则明确标为工程推断，不替作者下结论。
6. 默认一次集中精读一个文件，按文件内部结构逐个推进；当前文件未完成前不得在多个文件之间来回讲解。
7. 当前代码关联其他文件时，只简要说明对方接口的输入、输出和职责，到对应文件阶段再展开实现。
8. 不重复学习者已经掌握的基础，也不跳过尚未掌握的前置概念。
9. “已经展示”不等于“已经掌握”；只有经过复述、预测、画图或实践验证后才能更新完成状态。
10. 阶段笔记只在形成稳定主题且用户明确要求时生成，写入 Obsidian 的 `编程/GenericAgent/`；不按对话或小函数创建流水账。
11. 笔记采用主题聚合：包含真实代码、执行链路、新知识、设计取舍、易混淆点、复习检查和源码索引；同一主题优先更新已有文件。
12. 用户明确结束学习会话时，只更新用户指定且实际存在的学习计划；是否生成 Obsidian 笔记以用户的阶段要求为准。
13. 如果用户只是要求探索、规划或讨论，不自动开始下一节源码讲解。
