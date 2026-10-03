# 任务卡与并行协作本地约定

> aTa 的角色卡是任务施工、范围判断、验证和汇报的唯一通用规则。本文件只记录 Mica 的路径、命名和本地事实源，不复制角色卡。

## 任务入口

- 任务卡位于 `docs/tasks/T-###.md`；master 在卡片中写目标、必读、写入范围、约束和完成标准，worker 在同一张卡追加交付报告。
- 任务 ID 使用全局递增的 `T-###`，与阶段编号无关；已使用编号不复用。当前编号由 `docs/STATUS.md` 的 master 记录。
- 当前任务汇总、主线和待决策见 `docs/STATUS.md`；卡片状态仅由 master 按 aTa 更新。决定理由见 `docs/decisions.md`，讨论中结论见 `docs/scratchpad.md`。

## 分支与 worktree

- 任务分支使用 `task/T-###-<英文短名>`，worktree 使用 `../mica-wt/T-###`，这是本项目的默认命名约定。
- 每个 worktree 使用独立 `.venv`，首次开发使用前执行 `uv sync --python 3.11`；工具链与隔离要求见 `AGENTS.md` 和 `docs/code_spec.md`。
- 不在 `main` 上并行施工；共享契约、治理入口、依赖配置和热点文件由 master 安排顺序，不以并行 worktree 掩盖写入冲突。
- 会话数据根使用独立临时目录；安装态验证在仓库外执行，不向真实 `~/.mica` 写测试数据。

## 文档归属

- 项目定位、路线、范围和架构归 `docs/PROJECT_PLAN.md`；阶段计划归 `docs/phaseN_plan.md`；领域行为归对应规格文档。
- 代码位置归 `docs/module_map.md`；使用命令归 `README.md`；视觉参考及其验收规则归 `docs/assets/tui/README.md`。
- master、worker 的项目特有经验分别归 `docs/ata/master_practices.md`、`docs/ata/worker_practices.md`；候选经验归 `docs/ata/inbox.md`。
- 历史日志只作可追溯证据，不作为当前行为事实源；禁止为了迁移删除有效的安全、兼容、取消、持久化或真实验收边界。

## 计划变更

目标、契约、验收或写入范围变化按 aTa 的变更规则处理；推翻已确认决定、项目级架构约束或已验收行为必须先取得用户确认并追加决定。计划外小型工作可直接使用任务卡，但仍须写明独立验收边界。
