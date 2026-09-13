# Mica 项目指南

## 项目定位

Mica 是面向个人开发者的轻量 Agent 基座。当前仓库只包含 Mica 的源码、测试和项目文档；产品范围、长期架构和非目标以 `PROJECT_PLAN.md` 为唯一事实源。

## 文档路由

- 产品定位、长期范围与架构边界：`PROJECT_PLAN.md`。
- 当前施工范围、接口契约与验收标准：对应的 `docs/phaseN_plan.md`。
- 源码文件定位与职责：`docs/module_map.md`；新增、删除或重命名源码时同步更新。
- 终端交互前端已确认的架构、交互与视觉规则：`docs/tui_design.md`。
- 面向使用者的安装与运行说明：`README.md`。
- 第三阶段构建记录：`docs/phase3_build_log.md`。除非用户明确要求，master 不读取或改写该日志。
- `CLAUDE.md` 只作为 Claude Code 的入口引用，不维护第二套规则。

未建立下一阶段计划时，不得从草案或未来设想自行推断实现范围。

## 当前代码定位

| 需求 | 优先读取 |
|---|---|
| 命令解析、交互循环与会话选择 | `src/mica_agent/cli.py` |
| 终端交互前端设计与迁移任务 | `docs/tui_design.md`、`docs/phase6_plan.md` |
| 应用装配、提交边界与上下文组合 | `src/mica_agent/app.py`、`src/mica_agent/context_builder.py` |
| Run 生命周期与事件顺序 | `src/mica_agent/engine.py`、`src/mica_agent/events.py` |
| 模型请求、SSE 和重试 | `src/mica_agent/providers/openai_compatible.py` |
| 会话模型、JSONL、锁与恢复 | `src/mica_agent/session_contracts.py`、`src/mica_agent/jsonl_store.py`、`src/mica_agent/session_service.py` |
| 工作区身份与数据目录 | `src/mica_agent/workspace.py` |
| 公共导出与安装入口 | `src/mica_agent/__init__.py`、`src/mica_agent/__main__.py`、`pyproject.toml` |

按问题读取最小相关模块及其直接调用方；不要为定位一个问题而通读全部源码。

## 修改与验证

1. 先执行 `git status --short`，保留用户已有的无关改动。
2. 默认做最小必要修改；修改公共契约时同时检查调用方、实现方和公开导出。
3. 不把 Provider 私有对象、文件路径、密钥或未完成回复写入会话记录或用户诊断。
4. 涉及 Engine、Provider、会话存储或 CLI 的改动，按阶段计划运行目标测试、全量测试、Ruff、格式检查、离线构建和必要的安装态验证。
5. 不执行未经用户明确授权的 `git reset --hard`、`git clean`、历史重写或远程推送。
6. 每个 `PN-0N` 任务通过 master 验收后，完成阶段状态更新和本地提交，并将当前分支推送到已配置的 GitHub 远端；只有推送成功才算同步完成，认证、网络或远端冲突必须明确报告。
7. 增加、迁移或删除产品路径时，同步检查并维护对应测试资产；删除测试前必须确认该产品路径已退出使用，且仍需保留的公共行为、安全边界或回归覆盖已经迁移。

## 协作与文档

Mica 的正式重建默认采用 aTa：当前对话负责计划、边界、审查和 Git 收口；worker 默认是外部 Claude Code 或另一个 Codex 对话。用户明确要求当前对话直接实现时，按用户指令执行。

每个新阶段开始前先确认阶段目标、边界、非目标和验收方式，再创建或更新对应的阶段计划。构建日志只记录该任务的目标、设计、取舍、成果和后续学习入口，不记录命令流水或敏感内容。

## 学习协作

教学优先结合当前 Mica 代码和真实运行链路；一次集中说明一个小知识块，区分已有事实与工程推断。除非用户明确要求，不因一次讨论自动创建学习笔记或修改业务源码。
