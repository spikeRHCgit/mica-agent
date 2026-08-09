# GenericAgent — 源码学习文档

## 项目定位

GenericAgent 是一个极简自进化 Agent 框架，核心代码约 3200 行 Python。通过 9 个原子工具 + 约 100 行的 Agent Loop，赋予 LLM 对本地计算机的系统级控制能力（浏览器、终端、文件系统、键鼠、ADB）。

技术栈：Python 3.10-3.13，MIT 协议。

核心设计哲学：不预设技能，靠进化获得能力。每完成一个任务，Agent 将执行路径固化为 Skill 存入记忆，后续同类任务直接复用。

## 架构总览

```
用户输入 → 前端层(frontends/) → 编排层(agentmain.py) → 执行循环(agent_loop.py)
                                                            ↓
                              LLM 调用(llmcore.py)  ←→  工具执行(ga.py)
                                                             ↓
                                                    记忆系统(memory/)
```

## 文件结构

### 核心代码（根目录 .py 文件）

| 文件 | 行数 | 职责 |
|------|------|------|
| `agent_loop.py` | 127 | Agent 核心执行循环，定义了 BaseHandler 和 agent_runner_loop |
| `agentmain.py` | 287 | 编排层：GenericAgent 类，含任务队列、LLM 客户端管理、三种启动模式 |
| `ga.py` | 589 | GenericAgentHandler 类，实现 9 个原子工具的 do_xxx 方法 |
| `llmcore.py` | 1032 | LLM 抽象层：统一调用 Claude/Gemini/Kimi 等多供应商 API |
| `TMWebDriver.py` | 283 | Chrome 浏览器控制（CDP 协议注入 Tab 页） |
| `simphtml.py` | 873 | HTML 页面简化，将网页内容压缩为 LLM 可消化的精简格式 |

### 配置与资源

| 文件 | 用途 |
|------|------|
| `pyproject.toml` | 项目定义：包名、版本、依赖（核心/可选分层） |
| `mykey_template.py` | API Key 配置模板，复制为 `mykey.py` 填入密钥 |
| `assets/sys_prompt.txt` | 系统提示词——Agent 的"人格"和行为规则 |
| `assets/tools_schema.json` | 工具 JSON Schema——告诉 LLM 有哪些工具及参数 |
| `assets/tmwd_cdp_bridge/` | 浏览器桥接 Chrome 扩展（JS） |
| `assets/insight_fixed_structure*.txt` | 记忆结构定义模板 |
| `assets/code_run_header.py` | code_run 工具执行的代码前缀模板 |
| `assets/agent_bbs.py` | 内嵌 BBS 服务（用于子 Agent 间通信） |

### 子目录

| 目录 | 职责 |
|------|------|
| `ga_cli/` | CLI 入口包（`__init__` + `__main__` + `cli.py`） |
| `frontends/` | 多种前端：TUI（Textual）、Web（Streamlit）、IM Bot（微信/飞书/QQ/Telegram 等）、桌面端 |
| `memory/` | 记忆系统：`.py` 为工具辅助脚本(ADB/OCR/UI检测)，`.md` 为 Agent 可读取的 SOP 知识库 |
| `reflect/` | 主动监控模式：定时/事件驱动的 Agent 触发机制 |
| `plugins/` | 插件（Langfuse 链路追踪） |

## Agent 执行流程

1. 用户输入进入 `GenericAgent.put_task()`（agentmain.py）
2. 任务入队 → `agent_runner_loop()` 启动循环（agent_loop.py）
3. 构建消息 → 调用 LLM（llmcore.py）
4. LLM 返回 tool_call → `BaseHandler.dispatch()` 分发到对应的 `do_xxx` 方法（ga.py）
5. 工具执行完毕 → 返回 `StepOutcome(data, next_prompt, should_exit)`
6. `next_prompt` 作为新的 user 消息发回 LLM → 回到步骤 3
7. 直到 `should_exit=True` 或超过 `max_turns` → 结束

## 9 个原子工具

| 工具 | 功能 | 实现位置 |
|------|------|------|
| `code_run` | 执行任意 Python/Shell 代码 | `ga.py:do_code_run` |
| `file_read` | 读取文件 | `ga.py:do_file_read` |
| `file_write` | 写入文件 | `ga.py:do_file_write` |
| `file_patch` | 修改文件 | `ga.py:do_file_patch` |
| `web_scan` | 感知网页内容 | `ga.py:do_web_scan` → `simphtml.py` |
| `web_execute_js` | 控制浏览器（需 TMWebDriver） | `ga.py:do_web_execute_js` |
| `ask_user` | 人机协作确认 | `ga.py:do_ask_user` |
| `update_working_checkpoint` | 更新当前任务工作记忆 | `ga.py:do_update_working_checkpoint` |
| `start_long_term_update` | 触发长期记忆固化 | `ga.py:do_start_long_term_update` |

## Python 学习要点

在阅读这个项目时，你会遇到以下 Python 进阶用法（按出现频率排序）：

- `@dataclass` — 数据类装饰器
- `yield` / `yield from` — 生成器（用于 LLM 流式输出）
- 类型注解 (`Optional[str]`, `Any` 等)
- `threading` + `queue` — 多线程任务队列
- `json.dumps(default=...)` — 自定义序列化
- `functools.partial` / `importlib` — 动态加载
- `asyncio` + `aiohttp` — 异步 HTTP（llmcore.py 中）

## 学习路径建议

1. `agent_loop.py` → 理解 Agent 的心跳循环
2. `ga.py` → 理解每个工具是怎么实现的
3. `agentmain.py` → 理解任务怎么从用户到 Agent
4. `llmcore.py` → 理解 LLM API 调用细节
5. `memory/` → 理解记忆系统设计
