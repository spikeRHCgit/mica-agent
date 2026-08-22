# Mica 第三阶段构建日志

## P3-01：会话公共数据契约

### 任务目的

第三阶段后续的 JSONL Store、Session Service 和上下文重建都需要共享同一组会话数据含义。本任务先固定纯数据与单条记录转换边界，使文件可靠性和业务操作可以在后续任务中直接复用契约，而不重新解释一次成功对话应保存什么。

### 设计与结构

- `SessionMeta` 保存会话身份、当前格式版本、工作区身份与路径、可为空的新会话标题，以及创建和更新时间。
- `TurnEntry` 以一次成功用户指令为持久化单位，通过稳定的 Turn ID、Run ID、完整用户消息、完整助手消息和完成时间保留轮次边界。
- 两个对象组成 `SessionRecord` 强类型联合；wire 记录使用 `session_meta` 与 `turn` 显式区分，并在每条记录上携带当前格式版本。
- 序列化器只产生标准 JSON 可表示值；反序列化器按显式记录类型恢复公共对象，不根据字段形状推断。

### 关键校验与错误边界

公共对象使用不可变、带 slots 的数据类。ID、工作区字段和路径必须是非空且非纯空白文本；标题允许空字符串表示尚未命名，但拒绝非空的纯空白标题。时间必须是带时区的 UTC，元数据更新时间不得早于创建时间。格式版本集中为整数 `1`，明确排除 `bool` 和未知版本。

直接构造对象时继续使用 `TypeError` 与 `ValueError` 表达编程错误；解析不可信记录时统一转换为 `SessionFormatError`。解析严格拒绝缺失字段、额外字段、错误类型、非法角色和时间，同时诊断只指出安全的错误类别或已知字段，不回显整条记录或消息内容。

### 关键取舍

没有直接保存扁平 `Message` 列表，因为它会丢失一次用户指令与完整助手回复之间的业务轮次边界，后续回退、复制和上下文处理将不得不重新猜测。`TurnEntry` 仍复用现有 `Message` 与 `MessageRole`，避免形成第二套消息模型；当前只保存成功的文本用户/助手消息，不预留失败状态、工具事件、流式 delta、模型私有响应或配置框架。

### 交付与未来学习入口

本任务交付会话契约模块、稳定顶层导出、公开行为测试与纯导入副作用测试。后续整体学习建议先从 `SessionMeta` 和 `TurnEntry` 的构造校验开始，再沿 `SessionRecordType`、`serialize_session_record()`、`deserialize_session_record()` 理解强类型对象与单条 wire 记录之间的边界；P3-02 可在此基础上实现文件级 JSONL 顺序、损坏检测和可靠写入。

## P3-02：工作区识别与 JSONL Store

### 任务目的

为后续 Session Service 提供稳定的工作区隔离和可靠的单会话文件存储原语。本任务只处理工作区身份、JSONL 格式完整性、文件操作与会话级并发边界，不承担会话业务语义。

### 设计与结构

- `workspace.py` 从显式启动目录解析最近的 `.git` 目录或文件；非 Git 目录使用自身作为根，并将解析符号链接、相对段和当前平台大小写规则后的绝对路径哈希为完整 SHA-256 工作区 ID。slug 在 casefold 后再次按同一安全字符规则过滤，避免 Unicode 组合字符使合法工作区解析失败。
- `WorkspaceIdentity` 同时保存规范路径、完整 ID、安全 slug 和 `<slug>-<path-hash>` 会话目录名。目录名哈希而非单纯 basename 保证同名工作区隔离；数据根通过 Store 注入，默认 `~/.mica` 只在调用时计算。
- `jsonl_store.py` 使用完整 session ID SHA-256 生成文件名和锁名，避免斜杠、反斜杠、冒号及路径段进入文件系统路径。JSONL 首行固定为 `session_meta`，后续为 `turn`；writer 使用 UTF-8、紧凑分隔符、`ensure_ascii=False`、`allow_nan=False` 与 `sort_keys=True`，读取器严格检查 UTF-8、换行、重复 JSON key、版本、记录顺序和整份快照。
- `StoredSession` 对跨记录的 ID 唯一性、时间顺序、工作区元数据和不可变 Turn 序列执行完整校验；扫描以稳定文件名顺序分别返回健康快照与安全诊断，不因单个坏文件丢弃其他会话。

### 关键取舍

逻辑追加采用同目录临时文件、flush/fsync、关闭后原子替换的 copy-on-write，而不是裸 append，从而在中断或失败时保留旧完整文件。会话锁使用标准库条件平台边界：Windows 使用 `msvcrt` 文件区域锁，POSIX 使用 `fcntl.flock`，并以按锁路径的线程锁补足同进程竞争；锁文件释放后保留，避免 inode 竞态。未引入第三方依赖，也未加入全局跨会话锁。

### 交付与未来学习入口

本任务交付工作区解析模块、跨平台文件锁、JSONL Store、必要的顶层稳定导出，以及工作区、Unicode slug、确定性序列化、格式损坏、原子替换失败后恢复、扫描、删除和跨线程/跨进程并发测试。P3-03 可在 `JsonlSessionStore` 的 create/load/append/rewrite/delete/scan 原语之上实现重命名、复制、回退、清空、列表排序和清理等 Session Service 业务操作。

## P3-03：Session Service 会话操作

### 任务目标

在既有工作区身份、JSONL Store 与标准会话契约之上建立唯一的会话业务语义拥有者，提供会话创建、恢复、排序选择、完整轮提交、变更、只读上下文和过期清理能力，不接入 App、CLI、Engine 或 Provider。

### 设计与结构

- `SessionService` 通过注入的 `JsonlSessionStore`、UTC 时钟和 session ID 工厂构造元数据，并将创建、加载、列表、最后活跃选择、提交、重命名、分支、回退、清空、删除和上下文快照集中在业务层。
- `SessionListResult` 保留健康会话与 `ScanProblem`；健康会话统一按 `updated_at` 降序、`session_id` 升序排列。`CleanupResult` 分离已删除、受保护跳过、锁定跳过、并发变化跳过和诊断问题。
- 分支保留源会话的创建时间、标题和不可变历史轮次，只生成新 ID；新 `updated_at` 使用源值、保留 turn 的最新时间和当前时钟的安全较大值。回退删除指定轮及其后续内容，清空只移除轮次；三类元数据变更统一使用同一不可回拨更新时间策略。
- 上下文快照只从每个 `TurnEntry` 产生 user/assistant 两条标准 `Message`，按持久化顺序返回新的 tuple，不加载 AGENTS 或 Provider 私有对象。

### 关键取舍

为满足清理“不等待锁”的边界，Store 增加窄的 `SessionDeleteStatus` 与 `try_delete()` 文件操作 primitive：它先非阻塞取得同一 session 锁，再在锁内重载并校验 `expected_snapshot`，只有快照仍相等时才删除；因此可区分成功、锁定、文件不存在和并发变化，格式损坏与 I/O 继续作为诊断错误暴露。Store 扫描支持零等待锁超时，借助 JSONL 的 copy-on-write 在锁定时读取完整旧快照，随后由 Service 决定 30 天阈值、当前会话保护和变化跳过等业务策略。

### 交付与未来学习入口

本任务交付 `SessionService`、列表与清理结果对象、业务错误边界、非阻塞删除 primitive、稳定公共导出及完整服务测试，覆盖时间/ID 注入、排序诊断、成功轮、历史变更、分支时间契约、回退/清空活动时间、上下文不可变性、过期边界、保护会话、锁定会话、锁内快照重检、并发变化跳过和删除前损坏诊断。P3-04 可在 `context_snapshot()` 的标准消息边界上增加独立的 AGENTS 加载与诊断组合，不改变本服务的会话真相职责。

## P3-04：上下文重建与 AGENTS.md 加载

### 任务目标

建立供应商无关的 Context Builder，在不接入 App、CLI、Engine 或 Provider 的前提下，按固定顺序读取全局与当前工作区 `AGENTS.md`，并与 Session Service 提供的标准 `Message` 历史组合为新的不可变上下文。

### 设计与结构

- `ContextBuilder` 显式接收 `data_root`、`WorkspaceIdentity` 和可选的单文件字节上限；全局路径固定为 `<data_root>/AGENTS.md`，工作区路径固定为 `<workspace_path>/AGENTS.md`。
- 每次 `build()` 先验证输入历史，再按 global、workspace 顺序重新读取文件；有效非空内容各生成一条 `MessageRole.SYSTEM`，最后原序追加会话消息。
- `ContextBuildResult`、`ContextDiagnostic` 及两个枚举均为不可变标准对象；诊断只保留稳定来源、错误类型和固定安全描述，不携带路径、内容或原始异常。

### 关键取舍

使用集中常量 `MAX_AGENTS_FILE_BYTES = 64 * 1024`，二进制读取最多 `limit + 1` 字节；超过上限立即跳过，合格后才严格 UTF-8 解码，因此不会截断或部分使用超限内容。缺失文件正常跳过；目录、权限/I/O、无效 UTF-8 和超限分别生成结构化诊断，并继续处理另一来源。Builder 不缓存内容、不创建目录、不写入 Store 或 JSONL，也不导入运行时/Provider 模块。

### 交付与未来学习入口

本任务交付 Context Builder、稳定公共导出和覆盖顺序、缺失、空文件、错误诊断、大小边界、无缓存、输入与结果不变性、JSONL 不变性、显式路径注入及轻量导入边界的测试。P3-05 可在会话创建或恢复装配时将 `SessionService.context_snapshot()` 传入 `ContextBuilder.build()`，再负责应用层与 CLI 的持久化闭环，不改变本模块的读取和组合职责。

## P3-05：MicaApp 与基础 CLI 持久化闭环

### 任务目标

把应用层的会话真相迁移到 `SessionService`，让成功 Run 在退出后可由新 App/CLI 恢复；同时提供当前工作区的新建、继续和编号恢复入口，不提前接入最终 TUI、斜杠命令或配置系统。

### 设计与实现结构

- `MicaApp` 显式绑定 `SessionService`、session ID 和 `ContextBuilder`，不再维护可写的 `_history`。每轮请求通过 `context_snapshot()` 和 `ContextBuilder.build()` 重新组合上下文；兼容用的 `system_prompt` 仅作为非持久化前缀。
- Engine 的 `TextDelta` 只进入本轮缓冲区；App 在收到 `RunCompleted` 后才以注入的 turn ID 工厂和 UTC 时钟构造完整 `TurnEntry`，并通过 `SessionService.commit_turn()` 一次提交。
- App 对写入失败拦截待转发的 `RunCompleted`，改发同 run ID、同序号的 `RunFailed(ErrorCategory.STORAGE)`；失败、取消和事件流提前关闭均不写入半轮。Engine 和 Provider 契约保持不变。
- CLI 在环境变量校验后解析当前 `Path.cwd()` 工作区，装配注入式 Store/Service/Context Builder；默认立即创建并持久化空会话（即使随后空白输入 EOF），`--continue` 选择最后活跃健康会话，`--resume` 仅展示健康会话并读取 1-based 编号。选定后执行受保护的过期清理和上下文预检，再创建 Provider，不保留提前创建的 Provider 生命周期。

### 关键取舍

App 的 `history` 只是从 Service 派生的不可变快照，不参与状态写入；AGENTS 只进入每次 ProviderRequest，不进入 JSONL。CLI 的选择、Provider 工厂、输入输出、工作区、数据根、Service、Context Builder、turn ID 工厂和时钟均保留测试注入边界，正式入口才读取默认数据根；Provider 只在会话选定且上下文预检完成后创建，后续 App 启动失败由统一关闭路径收口。会话扫描、清理和 AGENTS 诊断只输出固定安全文案，避免传播路径、原始异常、凭据或上下文内容。

### 交付成果与未来学习入口

交付了 App/真实 JSONL Store/Context Builder 的成功恢复链路、两轮历史、失败/取消/提前关闭、写入失败当下 JSONL 字节与 turns 不变、恢复轮单独持久化的测试，以及 CLI 新建、默认空白 EOF 空会话、继续、编号恢复、非法选择、无健康会话、互斥参数和 AGENTS 预检测试。P3-06 可在此基础上完成仓库外 wheel 验证与阶段收口；最终 TUI 和配置系统继续位于后续阶段。

## P3-06：集成验证、README 与阶段收口

### 任务目标

为已安装 wheel 补齐仓库外的真实持久化恢复证据，并让 README 与第三阶段实际能力一致；不扩展到工具、TUI、配置或最终会话命令界面。

### 设计与测试结构

- 在既有完整 wheel/venv fixture 上增加标准库 loopback HTTP/SSE 服务，记录已安装 CLI 发出的两次 Chat Completions 请求并返回确定的成功流；测试通过已安装的 `mica` 入口，不导入仓库源码或 monkeypatch App、CLI、Store 私有实现。
- 测试在仓库外创建包含 Unicode 与空格的工作区，并将 HOME、USERPROFILE、数据根和 Python 导入环境隔离到临时目录；通过公开的 `JsonlSessionStore`、`SessionService` 和 workspace resolver 检查 JSONL 位置、工作区元数据、轮次完整性与会话复用。
- 首次成功轮退出后，第二次 `mica cli --continue` 必须复用同一健康会话；loopback 请求只包含既有 user/assistant 历史和新的 user 消息，工作区没有 `AGENTS.md` 时不凭空加入额外上下文。

### 关键取舍

使用本机 loopback 而非真实网络或凭据，既验证了安装态 Provider、SSE、CLI、App、Service 和 Store 的真实边界，又避免外部服务不确定性。测试只断言稳定的用户可观察行为和公开持久化结果，不把测试输入、完整响应、密钥或 JSONL 内容写入构建日志。异常写入可靠性继续由既有 Store 失败恢复测试覆盖，不新增依赖平台权限行为的脆弱测试。

### 交付成果与未来学习入口

交付仓库外 wheel 的成功对话、JSONL 落盘、同工作区 `--continue` 恢复和完整两轮上下文自动化证据；README 已说明新建、继续、选择、成功轮提交边界、默认数据位置与未包含范围。后续可从 `docs/phase3_plan.md` 的阶段达标标准继续学习，并由 master 统一更新阶段计划状态。
