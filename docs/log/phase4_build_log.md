# Mica 第四阶段构建日志

> 本文按任务记录初始目标、留存材料可核对的施工顺序、实际问题与当时的验收证据，不作为现行技术规格。没有返工记录的任务不推测 worker 曾犯的错误；预防性设计单独说明。
> 阶段交付见 `docs/phase4_plan.md`；当前代码位置见 `docs/module_map.md`。本阶段早期的 `ls`、三模式权限和会话 v2 均已被后续任务调整，阅读时应以任务顺序理解。

## P4-01：公共工具、交互与取消契约

- 初始目标：先固定供应商无关的工具、权限、用户交互、取消及事件对象，同时保持纯文本消息与会话 v1 兼容。
- 实施经过：
  1. 定义 `ToolSpec`、调用/请求/结果、权限与交互响应、工具/交互事件，并给 `Message` 追加可选工具字段。
  2. 在构造时把 JSON object/array 深层复制为只读快照；用一次性异步事件实现幂等 `CancellationToken`。
  3. 补非法类型、安全错误、旧 session/provider/engine/app 兼容及公共导入测试。
- 问题处理：留存记录未载明施工返工。此处预防的风险是调用方后续修改原始参数导致契约对象变化，以及取消前后等待者得出不同结果；校验和异常文案不回显原始输入。
- 结果与验证：公共目标组、旧核心兼容组、全量质量检查与离线构建通过；注册、执行和 Provider tool calling 留给后续任务。

## P4-02：工具注册、Schema 校验与权限策略

- 初始目标：在不执行真实工具的前提下建立 Registry、标准参数校验和只消费规范化事实的 Policy，并补齐不可变 JSON 快照的转换入口。
- 实施经过：
  1. 审计 `jsonschema`、`fastjsonschema` 和手写子集，选择 Draft 2020-12 的 `jsonschema`，禁用远程引用获取。
  2. 实现注册前 Schema/handler 检查、重复名称拒绝、确定性 `specs()`、参数验证及 `to_mutable_json()` 深拷贝。
  3. 固定 hard deny → 一次授权 → allow rule → 当时权限模式矩阵的优先级，并用公共与分发测试验证。
- 实际问题与修正：新增直接依赖使旧分发测试“运行依赖只有 httpx”和“空 uv cache 可离线装全套依赖”的假设失效；经用户授权，测试改为分别检查 wheel 元数据、无依赖基础入口和锁定依赖环境中的离线运行，不把未验证的空 cache 安装称为已通过。
- 结果与验证：未知工具、非法 Schema、模型参数错误和规则异常都给固定安全错误；目标/兼容/全量、Ruff、格式和离线构建通过。此时的 `ACCEPT_EDITS` 模式在第五阶段删除。

## P4-03：工作区路径与文件工具

- 初始目标：让四个当时的文件工具只通过一个工作区相对路径入口访问文件，明确容量、敏感路径和原子替换边界。
- 实施经过：
  1. 实现 `WorkspacePathResolver`，拒绝绝对/越界、Windows 设备名、尾随点空格、ADS 及已有路径的链接或重解析点。
  2. 集中敏感文件规则，原子注册 `read`、`ls`、`write`、`edit`；读取和目录扫描只请求上限加一项，不返回部分结果。
  3. 为 `write`/`edit` 使用同目录临时文件、`fsync` 和 `os.replace`，再做故障注入与真实 Windows 路径测试。
- 问题处理：留存记录未载明施工返工。路径组件规范化、敏感名变体和原子替换是设计时防护，不代表已经构成 OS 沙箱；普通 symlink 创建在当时的 Windows 权限下不可用，POSIX 分支未做原机验收。
- 结果与验证：敏感/设备/越界目标被安全拒绝，失败不留下半写入或部分读取；四个公开 handler、容量边界、原字节保持及重试测试通过。独立 `ls` 后在 P4-10 退役。

## P4-04：Bash、ask 与子进程取消

- 初始目标：补齐运行时 `bash`、`ask`，让用户等待和子进程服从同一取消语义，不把终端输入写进工具 handler。
- 实施经过：
  1. 原子注册两个工具；`ask` 只经注入的 requester 等待一次响应，并在取消或外部 Task 取消时回收等待任务。
  2. Windows 找到真实 Git Bash，以工作区 cwd 和独立进程组运行；POSIX 使用会话/进程组，输出按原始字节上限收集。
  3. 清理传给子进程的敏感环境变量，覆盖超时、输出超限、外部取消和已启动后代的进程树回收。
- 问题处理：留存记录未载明施工返工。设计时特别避免“只取消父进程，后代仍写文件”的假关闭：真实 Windows 后代测试先确认启动，再触发三种终止路径并检查延迟副作用不存在。Bash 不提供操作系统沙箱，POSIX 清理当时未原机验收。
- 结果与验证：Unicode/空格 cwd、有界输出、失败结果、环境清理和真实后代回收通过公开 handler 测试；注册阶段不访问 Bash 路径或启动进程。

## P4-05：OpenAI-compatible 工具调用

- 初始目标：让 Provider 发送工具定义、收集流式调用碎片并只在完整结束后交给 Engine，不在协议层执行工具。
- 实施经过：
  1. 兼容性扩展 `ProviderRequest.tools` 与 `Finish.tool_calls`；无工具请求保持原载荷。
  2. 按 `delta.tool_calls[].index` 收集交错的 ID、名称、类型和 JSON 参数片段，到结束信号才构造公共 `ToolCall`。
  3. 对索引缺口、冲突字段、非法 JSON/类型、结束原因和敏感错误诊断建立 MockTransport 回归。
- 实际问题与修正：旧式结束 chunk 可能有 `finish_reason` 却没有 `delta`；适配器后来明确将这种缺失视为空对象，同时继续拒绝显式 `null` 或错误类型的 `delta`，避免把真实兼容输入当畸形响应，也不放宽一般坏包。
- 结果与验证：一旦见到正文或工具调用增量，传输中断不重试以免重复工具意图；纯文本兼容、混合流、一次终态和不回显参数/凭据的失败测试通过。

## P4-06：Engine 多步工具循环

- 初始目标：在一个 Run 内顺序执行模型给出的多个工具、把真实结果反馈模型继续生成，并统一权限等待、取消和上限。
- 实施经过：
  1. 建立 `ToolExecutor` 的注册查找 → 参数校验 → 权限事实 → 决策/一次确认 → handler → 安全结果链。
  2. 用 `InteractionBroker` 维护独立交互 ID 与一次响应；Engine 按模型顺序发事件并把 assistant tool calls/TOOL 结果写入本轮请求上下文。
  3. 对每次 Provider 流使用显式 `aclosing()`，再覆盖多工具顺序、权限/ask、取消、迟到响应、模型/工具上限和安装态回归。
- 实际问题与修正：严格警告验证暴露测试替身的 `TemporaryDirectory` 未显式关闭；清理替身后不再出现资源或异步生成器告警。提前退出 Provider 流也不能依赖 GC，必须在开始下一次模型请求或发终态前等待 `aclose()`。
- 结果与验证：普通工具失败作为 TOOL 结果回传模型，取消只产生一个终态；当时全量 `796 passed, 1 skipped`，严格警告组、分发与离线构建通过。

## P4-07：会话 v2 与完整工具轮提交

- 初始目标：让成功工具 Run 的完整消息顺序可持久化和恢复，同时兼容旧 v1，会话失败时仍不写半轮。
- 实施经过：
  1. `TurnEntry` 增加中间消息，v2 turn 改为有序 `messages`；Store 在单会话锁内对 v1 做首次业务写入升级。
  2. Engine 用完成转录 sink 提供真实 USER/assistant tool calls/TOOL/final assistant 序列；工具消息在 Provider 上下文和持久化前共用清洗副本。
  3. App 只在转录完整且 `RunCompleted` 后提交一次，测试替换失败、旧版读取、跨层恢复及工具副作用后的失败提醒。
- 实际问题与修正：不可信 wire 的缺失版本或把数组/对象放进版本字段，曾可能漏出 `KeyError`/`TypeError`；新增固定 `SessionFormatError` 回归并让扫描隔离坏文件。存储替换失败保留旧 v1 原字节、无临时文件，可重试升级。
- 结果与验证：成功工具轮完整恢复，失败/取消/提前关闭不伪造成功 Turn；当时全量 `823 passed, 1 skipped`，严格异步、分发和离线构建通过。工具副作用不承诺回滚。

## P4-08：基础 CLI 集成与阶段闭环

- 初始目标：把六个当时的内置工具、权限与 ask 接入基础文字 CLI，完成安装态与真实 Provider 的工具会话恢复。
- 实施经过：
  1. CLI 在 Context/AGENTS 预检后创建共享 Broker、Registry、Executor，再创建 Provider 与 App；交互输入从前端走 `respond_interaction()`。
  2. 加入权限询问、ask 文本回答、工具状态的安全 stderr 输出；提前 EOF/取消/`aclose()` 时显式收敛 Broker、Provider 和工具子流。
  3. 仓库外 wheel 用 loopback 验证 write → 完整 v2 Turn → `--continue` 不重做副作用，再以受控真实 Provider 和 Windows Git Bash 冒烟。
- 问题处理：留存记录未明确标记一次 worker 返工；生命周期检查要求消费者在 Permission/Ask 等待处主动 `aclose()` 时同步清理，而不是等事件循环终结器。安装态证据与真实 Provider 证据分开记录，未把支持差异视为已经解决。
- 结果与验证：两次工具 Run 保存在同一隔离会话，Key 不进入 CLI 输出或 JSONL；当时全量 `850 passed, 1 skipped`，Ruff、格式、离线构建及受控真实服务验收通过。

## P4-09：内置系统提示词与启动快照

- 初始目标：把随包提示词及本次加载的 AGENTS 规则组织成启动上下文，而不是每轮重复读文件或写进会话消息。
- 实施经过：
  1. 将内置提示词作为 wheel 资源分发。
  2. 启动或恢复时读取允许位置的 AGENTS，组成一次性环境、规则和工具快照。
  3. 后续 Run 复用冻结快照，缺失或受限资源只给固定安全诊断。
- 问题处理：留存记录没有可核实的施工返工；预防性边界是系统提示词与 AGENTS 只做启动头部，不混入普通 USER/TOOL 或思考正文，也不把路径、凭据和原始异常写入会话。
- 结果与验证：阶段记录确认资源随包、快照冻结和诊断边界；没有留下独立测试计数，不补写推测数字。

## P4-10：内置工具收口

- 初始目标：删除职责与 `read`/`bash` 重叠的独立 `ls`，最终只向 Provider 暴露 `ask`、`bash`、`edit`、`read`、`write` 五个内置工具。
- 实施经过：
  1. 先将公共测试改为预期五工具，让旧实现因仍含 `ls` 出现预期红灯。
  2. 删除 `ls` Spec/handler、列表限制常量、执行映射及 Policy 的 LIST 操作，不留隐藏别名。
  3. 用未知 `ls` 的安全结果、五工具 Provider 快照、安装态 write/continue/Bash 回归确认闭环。
- 问题处理：这是经确认的产品边界收敛，不是工具“坏了”的修复。模型若虚构 `ls`，只收到 `unknown_tool`，不会被 Mica 暗中改写成 Bash；目录查看仍按当时的 Bash 权限规则处理。
- 结果与验证：文件工具保留 read/write/edit 的安全边界；当时全量 `867 passed, 1 skipped`，wheel 不再含 `ls` 注册入口或 LIST 成员。

## P4-11：思考后端契约

- 初始目标：独立表达模型思考档位和增量，保证思考不混入正式回复、Provider 后续上下文或成功会话。
- 实施经过：
  1. 给请求增加默认 OFF 的 `ReasoningEffort`，让 Provider 按默认/自定义映射产生协议字段；OFF 完全不新增字段。
  2. 将 `reasoning_content` 解析为独立 Provider 事件，再由 Engine 转为严格递增序号的 `ReasoningDelta`。
  3. App 允许按 Run 指定档位，基础 CLI 显式忽略思考正文，并跑多步工具、坏流与安装态回归。
- 实际错误与修正：测试先红灯属于预定过程；畸形自定义档位映射曾暴露未捕获的 `KeyError`，随后改为发 HTTP 前返回固定安全 `ProviderError`。错误类型的 reasoning 字段同样只形成一次安全畸形响应。
- 结果与验证：思考不进入工具参数、正式文本、完成转录或 JSONL；当时全量 `919 passed, 1 skipped`，分发、静态检查及 wheel 导入通过。

## P4-12：工具呈现与历史快照

- 初始目标：把前端安全展示与工具执行参数、模型结果分离，并让完成工具轮能恢复原样展示而不重新运行工具。
- 实施经过：
  1. 定义深层冻结且清洗后的 `ToolPresentationSnapshot`，Engine 在执行前后分别发 `ToolPrepared` 与带终态快照的 `ToolFinished`。
  2. 为 read/write/edit/bash/ask 建安全展示：相对路径、保留格式的写入正文、真实逐行差异、有限 Bash 输出与回答。
  3. 在 v3 Turn 独立保存有序快照；旧 v1/v2 首次业务写入原子升级，App 仍只提交完整成功轮。
- 实际返工：探针分别复现了写入正文经解析重排后格式丢失、引号凭据后缀残留、未引号且含空格的值残留、`ToolResult.presentation` 可变，以及 edit 长度变化后的 old/new 行号错位；逐项修复并补回归，不把原始参数或结果塞进展示快照。
- 结果与验证：安全快照、原始布局、diff 行号、旧版原字节保持和安装态分发均通过；当时全量 `958 passed, 2 skipped`。普通 Windows symlink 测试仍因权限跳过。

## P4-13：Bash 动态输出

- 初始目标：在不改最终 ToolResult/会话语义的前提下，分阶段把有界安全 Bash 输出送到 CLI，并保留取消与进程树清理。
- 实施经过：
  1. P4-13A 定义不在 `repr` 暴露正文的 `ToolOutputDelta` 公共事件；P4-13B 让 Bash 按完整 LF/CRLF 记录或 EOF 尾部增量解码、清洗后报告。
  2. P4-13C1 由 Executor 仅在当前 handler 生命周期路由 `(call_id, tool_name)` 输出，Engine 借既有信号队列给增量分配 `run_id` 和严格递增 `seq`，不建第二套任务系统。
  3. P4-13C2 在 CLI 装配同一个 Executor/reporter，把动态安全文本按序写 stderr；仓库外 wheel 与真实 Windows Git Bash 验证状态行之间的输出和最终结果。
- 实际返工：Bash cleanup 开始后曾可能迟到报告 EOF 尾部，遂在清理前关闭报告 gate；同步 reporter 意外返回 awaitable 时不再隐式等待，而是安全关闭/取消并给固定失败。CLI 对无换行尾部只补一个分隔换行，避免与后续状态粘连或多出空行。
- 结果与验证：动态事件不进 Provider 消息、最终结果或 JSONL；取消/超限/关闭时路由解除、后代回收、无半轮提交。C2 当时全量 `1024 passed, 2 skipped`，分发、Ruff、格式、离线构建通过。

## P4-14：结构化 ask

- 初始目标：在保留旧自由文本 ask 的同时，加入 2–4 个结构化选项、推荐项、自定义回答与备注，公共契约先于 UI 接入。
- 实施经过：
  1. P4-14A 建不可变 `AskOption`/`AskPrompt`，兼容扩展 `UserInputResponse` 的选择序号和备注；P4-14B 让 runtime、Broker 与 Engine 传递完整 Prompt，并把结果固定为三字段数据。
  2. P4-14C1 在 Broker 响应入口核对 Prompt：自由文本无序号，固定选项序号有效，自定义项仅在允许时接受；合法选择把答案规范化为对应 label。
  3. P4-14C2 扩展 Provider 可见 ask Schema 和基础 CLI 菜单/自定义输入，最后用 wheel + loopback 验证 TOOL 结果、v3 保存及 `--continue`。
- 实际错误与修正：C1 在旧实现上复现“自由文本带非法序号仍返回 True 并消耗 pending”，修正为拒绝但保留等待以便重试。B 切片把 ask `data` 升为三字段后，旧 P4-08 测试仍断言纯字符串；经 master 授权只更新该断言，保留 status、call ID、历史和安全检查。
- 结果与验证：有选项时仅 ASCII 编号可定位，非法输入固定提示后重试；安装态证明第二次继续不会重做首轮 ask。C2 当时全量 `1105 passed, 2 skipped`，严格警告、分发、静态检查及离线构建通过。
