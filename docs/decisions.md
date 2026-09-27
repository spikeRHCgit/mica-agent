# Mica 决策记录

> **文档职责**：记录已经由用户确认、会影响后续实现或协作的项目决定，包括当时的理由和取代关系，
> 避免后续 agent 重复争论已定问题或误用旧方案。
>
> **维护规则**：只追加，不改写已有条目。决定被推翻时追加新条目，并在旧条目标题末尾标注
> 「（已被 D-xxx 取代）」。只有用户确认的决定才能写入；仍在讨论的选项写在 `docs/STATUS.md` 的
> 「待用户决策」。每条 2～4 行，细节指向现行事实源；编号全局递增。
>
> D-001～D-007 根据 Git 历史和构建日志补记，日期取对应提交日期；原始记录没有写明理由的，如实注明。

## D-001 工具 Schema 校验采用 `jsonschema`（2026-08-23）

- 决定：使用 JSON Schema Draft 2020-12 的 `jsonschema` 校验工具定义和实际参数，禁用远程引用获取。
- 理由：审计 `jsonschema`、`fastjsonschema` 和手写子集后选定；这是项目第二个运行依赖。
- 事实源：`src/mica_agent/schema_validation.py`；证据见 `docs/log/phase4_build_log.md` P4-02。

## D-002 删除独立 `ls`，内置工具固定为五个（2026-08-30）

- 决定：只保留 `read`、`write`、`edit`、`bash`、`ask`；目录查看通过 `bash` 完成。模型虚构的 `ls`
  只得到 `unknown_tool`，不暗中改写为 Bash 调用。
- 理由：`ls` 与 `read`、`bash` 职责重叠，属于经确认的产品边界收敛。
- 事实源：`docs/phase4_plan.md`；证据见 `docs/log/phase4_build_log.md` P4-10。

## D-003 权限模式收敛为 `default` 与 `solo`（2026-09-05）

- 决定：删除 `accept_edits`，不保留兼容别名。`default` 对受保护资源、危险或无法可靠分类的命令请求
  一次确认；`solo` 跳过策略审批，但保留注册、Schema、文件能力限制、超时、取消和清洗。
- 理由：原始记录只写明「用户确认的两模式权限重构」，没有留存具体理由。两模式都不构成执行沙箱。
- 事实源：`README.md`、`docs/phase5_plan.md` §3.3；证据见 `docs/log/phase5_build_log.md` P5-07A/B。

## D-004 终端前端改用主缓冲区，Textual 全屏前端退役（2026-09-13）

- 决定：`mica cli` 默认使用终端主缓冲区和原生 scrollback，不进入 alternate screen；输入层基于
  prompt_toolkit，Markdown 排版使用 Rich；删除 Textual 模块、专属测试和依赖，保留 `--plain`。
- 理由：Textual 方案在真实 Windows Terminal 中反复出现焦点、滚动和选择所有权不稳定（P5-10F）。
- 事实源：`docs/tui_design.md`；证据见 `docs/log/phase5_build_log.md` P5-10F、P5-11A～C。

## D-005 中断进度独立保存为 `RunCheckpoint`，会话格式升级为 v4（2026-09-14）

- 决定：完整成功仍只提交 `TurnEntry`；取消或可恢复 Provider 失败另存一次 `RunCheckpoint`，
  `StoredSession.entries` 是两者唯一的有序时间线。v1～v3 保持只读兼容，首次业务写入时原子升级。
- 理由：中断后需要保留原始指令和真实进度，同时不伪造成功轮、工具结果或最终回答。
- 事实源：`docs/phase5_plan.md` §3.4；证据见 `docs/log/phase5_build_log.md` P5-10G1/G2。

## D-006 Phase 5 收口后先进入阶段间治理（2026-09-15）

- 决定：Phase 6 暂不启动，先完成文档治理、协作方式、代码规范和代码框架对齐，再制定 Phase 6 计划。
- 理由：worker 容易围绕局部任务形成局部最优实现，注释和测试随返工持续堆积，需要先建立全局规范
  （治理期开始后整理在 `docs/code_optimization_plan.md` §1）。
- 事实源：`docs/STATUS.md`、`docs/code_optimization_plan.md`。

## D-007 阶段重新编号（2026-09-23）

- 决定：原 Phase 4「仓库收口」不再单列；原 Phase 5、6 改为现行 Phase 4、5，未开始阶段顺延为
  Phase 6～9。历史提交不改写：提交信息中的 `P5-*`、`P6-*` 分别属于现行 Phase 4、Phase 5 的任务，
  `P4:` 提交属于原仓库收口工作。
- 理由：原始记录没有写明理由。
- 事实源：`docs/PROJECT_PLAN.md` §5；提交 `c6cc0e3`。

## D-008 agent 协作模式（2026-09-26）

- 决定：采用 1 个 master 加多个并行 worker 的 aTa 协作；worker 会话既有一任务一会话，也有跨任务、
  经历上下文压缩的长会话；偶尔由单个 agent 独立完成任务。任何 agent 都可以记录候选经验，晋升为
  正式规则必须经用户确认。
- 理由：用户在治理期讨论中确认，作为文档体系与协作规则的设计前提。
- 事实源：`docs/tasks/README.md`、`docs/lessons/inbox.md`。

## D-009 文档体系与编号规则（2026-09-26）（部分被 D-010 修订）

- 决定：新增 `docs/STATUS.md`（实时状态）、本文（决策记录）、`docs/tasks/`（任务卡）和
  `docs/lessons/inbox.md`（候选经验）；`AGENTS.md` 改为启动步骤、文档路由和红线。任务使用全局
  `T-###` 编号，与阶段号脱钩；阶段编号一经使用即冻结；已完成阶段计划冻结，不再随后续阶段补注，
  现行实现以 `docs/module_map.md` 为准。
- 理由：支撑新会话快速接手、多 worker 并行和经验沉淀闭环。用户批准按方案实施，审查后的修订另行追加。
- 事实源：`AGENTS.md`、`docs/tasks/README.md`。

## D-010 治理期整改期间冻结源码地图（2026-09-26）（结束条件被 D-014 修订）

- 决定：代码重构完成、code spec 定稿之前不修改 `docs/module_map.md`，保持提交 `5709479` 的版本；
  整改任务以 `docs/code_review_report.md` §4、任务卡和源码定位，重构收口时按 code spec 确定的结构
  重写。现行实现以源码和现行规格文档为准（修订 D-009 中「以 `docs/module_map.md` 为准」）。
- 理由：重构必然改变源码地图；逐文件、逐符号的地图结构与仍在变化的代码耦合过高。由用户决定。
- 事实源：`docs/code_optimization_plan.md` §4.3。

## D-011 未开始阶段维护需求简报（2026-09-26）

- 决定：`docs/PROJECT_PLAN.md` 为每个未开始阶段维护需求简报，条目分「已确认 / 倾向 / 留给阶段计划」
  三档；已确认条目是阶段计划的输入约束，推翻前须先追加决定；阶段计划创建时迁入简报。提交
  `91650fb` 删除的已确认内容，逐条对账后写回。
- 理由：`91650fb` 精简项目计划时删除了已确认的后续阶段决定，导致阶段开始时需要重新讨论，并可能
  偏离原规划。用户批准按方案实施。
- 事实源：`docs/PROJECT_PLAN.md` §5.2～§5.6。

## D-012 计划变更分级处理（2026-09-26）

- 决定：改变计划时按 L0 微调、L1 任务变更、L2 推翻已确认内容、L3 主线切换分级处理，探索性试验
  使用 spike；任务卡增加「挂起」「已取消」状态，STATUS 增加「暂停的主线」。
- 理由：中途改主意和中断计划是常态，原文档体系只支持按计划前进。用户批准按方案实施。
- 事实源：`docs/tasks/README.md` §6。

## D-013 旧计划对账结论（2026-09-27）

- 决定：恢复核心价值「可部署」和工程学习目标；旧规则「有文本时单击 `Ctrl+C` 清空输入」视为被
  `docs/tui_design.md` §6.5 取代；不恢复 `docs/system_prompt_design.md`，系统提示词与 AGENTS 装配
  的现行事实以源码为准；斜杠命令的阶段归属保持简报中的「倾向」。
- 理由：用户逐项确认旧计划的对账结果；`system_prompt_design.md` 维持 `91650fb` 删除它的决定。
- 事实源：`docs/PROJECT_PLAN.md` §1、§2；旧计划原文见 `git show f70fa30:PROJECT_PLAN.md`。

## D-014 v0.1.0 前不进行源码架构重构（2026-09-27）

- 决定：当前治理期只完成 code spec 定稿、源码注释与 docstring 修整和测试资产精简；现有源码架构
  在 v0.1.0 开发完成前保持不变，之后再根据实际维护证据讨论是否重构。
- 修订：D-010 中源码地图冻结至「代码重构完成」的条件，改为冻结至本轮注释、测试与 code spec
  治理收口；收口时只按现行源码校正地图事实，不要求先完成架构重构。
- 理由：当前优先保证后续开发受统一规范约束，并避免在版本功能尚未完成时同时承担架构迁移风险。
- 事实源：`docs/code_optimization_plan.md` §4.3～§4.5。
