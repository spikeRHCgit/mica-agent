# Mica 全局代码只读审查报告

> 审查日期：2026-09-25
>
> 审查性质：架构、实现、注释与测试资产的静态只读审查
>
> 关联计划：`docs/code_optimization_plan.md`
>
> 生命周期：本文是 2026-09-25 的审查快照，正文不随代码更新；§14 为 2026-09-26 的补充发现。
> 整改期间 `docs/module_map.md` 冻结，§4 是理解链路与状态所有者的参照，现行事实以源码为准；
> 治理收口后删除，原文从 Git 历史查看。

## 1. 审查结论

Mica 当前代码并不是整体失控，也不适合推倒重写。项目已经形成一批值得保留的稳定边界：内部模块
导入图无循环，Provider 契约与具体实现分离，JSONL 存储具有严格解析与原子替换语义，主缓冲区渲染
器维护了明确的物理终端不变量，composer 编辑模型和多项公共数据契约也具备较完整的边界校验。

当前最主要的问题是：随着阶段任务持续叠加，少数编排模块承担了越来越多的变化原因，而测试与注释
仍保留大量施工阶段痕迹。局部功能通常能通过测试，但新的实现容易继续向 `AgentEngine`、CLI、
`MainBufferScreen` 和会话编解码模块堆叠状态与分支；测试则倾向于为每次返工追加新场景，而不是按
稳定行为重新整理既有资产。这正是 worker 容易追求局部最优、却难以判断全局边界的直接环境因素。

本轮没有发现需要立即停止使用的确定性 P0 功能缺陷。代码优化应以“先确定 code spec 和目标边界，
再按链路小批迁移”为主，不应以文件行数、测试数量或注释语言为由进行机械拆分、批量删测或大规模
翻译。

## 2. 审查范围与证据边界

本轮读取并分析了：

- `src/mica_agent/**` 的 46 个 Python 源文件；
- `tests/**` 的 57 个 Python 文件，其中 56 个为 `test_*.py` 测试模块；
- `pyproject.toml`、公共导出、分发测试和现有模块地图；
- CLI、App、Engine、Provider、工具、持久化、恢复、TUI 输入与渲染等关键链路；
- 源码导入关系、函数复杂度候选、注释语言和阶段标记、测试收集、私有成员访问、固定等待与重复夹具。

执行了以下非破坏性验证：

- `uv run pytest --collect-only -q -p no:cacheprovider`：成功收集 1778 个测试；
- AST 与文本统计：用于定位长函数、分支密集区、注释分布、测试断言与私有访问；
- 内部模块导入图检查：未发现循环依赖；
- Git 状态和 diff 检查：用于确认审查期间没有修改产品代码或测试。

本轮没有运行全量测试、真实 Provider 或真实 Windows Terminal 冒烟。因此报告中的架构结论来自静态
证据；涉及真实时序、平台输入或运行性能的内容只列为需要后续动态验证的边界，不表述为已复现缺陷。

## 3. 可复核基线

| 指标 | 当前值 | 用途 |
| --- | ---: | --- |
| 产品 Python 文件 | 46 | 规模基线 |
| 产品 Python 物理行 | 15,424 | 仅用于定位候选热点 |
| tests Python 文件 | 57 | 包含 56 个测试模块和 1 个辅助模块 |
| tests Python 物理行 | 32,709 | 约为产品代码的 2.12 倍，不直接代表冗余率 |
| 收集到的测试 | 1,778 | pytest 实际收集结果 |
| AST 测试函数 | 1,072 | 参数化会使收集数高于函数数 |
| AST `assert` | 4,044 | 测试断言规模信号 |
| 测试中的私有属性引用 | 241 | 需要逐项区分产品私有状态与测试局部对象 |
| 内部导入循环 | 0 | 当前依赖图的重要优点 |

产品代码中行数较多的文件包括：

- `tui/main_buffer/screen.py`：1,009 行；
- `tui/main_buffer/renderer.py`：991 行；
- `session_contracts.py`：914 行；
- `cli.py`：786 行；
- `tui/main_buffer/tool_views.py`：745 行；
- `engine.py`：689 行；
- `tool_presentation.py`：658 行；
- `runtime_tools.py`：624 行；
- `jsonl_store.py`：614 行；
- `interactions.py`：587 行；
- `tui/main_buffer/terminal.py`：585 行。

这些数字只能说明“值得检查”。例如 `renderer.py` 虽然接近千行，但其物理行、单元格、 retained
screen 与差分重绘不变量较集中；相反，`engine.py` 的 `AgentEngine.run()` 虽然所在文件更短，却
同时负责 Provider、工具、进度、取消、展示和终态，是更高优先级的架构热点。

## 4. 当前系统链路与状态所有者

### 4.1 启动与装配

```text
CLI 参数与环境
  -> 工作区 / 会话选择
  -> Provider、Registry、Policy、Permission、ToolExecutor 装配
  -> MicaApp
  -> MainBufferRuntime 或 plain frontend
```

当前 CLI 是实际 composition root，但同时还实现配置读取、会话交互选择、plain 前端、stdin 线程和
资源关闭。装配职责存在，却没有被明确隔离成可命名边界。

### 4.2 单轮运行

```text
用户消息
  -> MicaApp.run()
  -> AgentEngine.run()
  -> Provider.stream()
  -> ToolExecutor.execute()
  -> 公共事件流
  -> MainBufferScreen / plain frontend
```

- Engine 拥有单次 Run 的循环、工具批次与唯一终态；
- MicaApp 拥有成功轮或检查点的提交时机；
- ToolExecutor 拥有单次工具调用的 schema、策略、权限、handler 与结果转换；
- 前端只应投影公共事件，不应重新决定业务终态。

这些目标边界总体存在，但 Engine 与 MicaApp 之间同时使用事件流和多个 sink 回调传递结果，导致
“谁拥有完整 Run 结果”仍然隐含在调用顺序和列表长度断言中。

### 4.3 会话与恢复

```text
MicaApp
  -> SessionService
  -> JsonlSessionStore
  -> v1-v4 wire codec / 原子 JSONL

StoredSession.entries
  -> 恢复上下文投影
  -> Provider request
  -> TUI 历史投影
```

`SessionService` 是业务语义边界，`JsonlSessionStore` 是持久化与原子替换边界，
`session_contracts.py` 同时承担领域模型、多个版本 wire codec 和部分展示快照解析。

### 4.4 工具链路

```text
ToolCall
  -> ToolRegistry
  -> schema validation
  -> Policy / Permission 或 Ask
  -> handler
  -> ToolMessage + ToolPresentationSnapshot
```

注册、校验、策略、权限和执行已经分层；主要问题在于中立执行契约仍从具体的内置工具模块反向导入，
而工具展示的 schema 校验分别散落在展示构造与会话反序列化中。

### 4.5 主缓冲区终端链路

```text
Win32 / prompt_toolkit 输入
  -> MainBufferScreen 模式与按键路由
  -> ComposerEditor / AskInteraction / PermissionInteraction

公共事件
  -> 语义块与工具视图
  -> MainBufferRenderer retained document
  -> ANSI 差分输出
  -> Terminal
```

`Terminal` 持有物理 IO，`MainBufferRenderer` 持有 retained screen 和重绘状态，这两个所有权应
保留。`MainBufferScreen` 目前同时承担输入、模式、事件准入、时间线投影、历史恢复、工具动态输出、
状态计时和 chrome 组装，已经成为前端侧的主要编排热点。

### 4.6 取消与关闭

取消信号跨越 CLI、Runtime、MicaApp、Engine、Provider、ToolExecutor 和 Bash 子进程。现有代码已经
显式处理 `CancelledError`、`GeneratorExit`、`aclose()` 和子进程清理；后续重构必须先保留这些可
观察时序，不能把“减少分支”误解为合并不同终止语义。

## 5. 值得保留的设计

### 5.1 依赖图整体可控

内部模块没有形成循环依赖。`tool_contracts`、`models`、`interactions` 和 workspace 相关模块已经
成为较稳定的共享边界。优化应修正少量反向依赖，而不是重新组织整个包结构。

### 5.2 Provider 契约具有真正的扩展性

Provider 使用独立协议与供应商无关事件模型，OpenAI-compatible 实现在传输边界完成解析。这里的
扩展点是结构化的，不应因为其他模块存在具体类型检查就笼统认定项目“无法扩展”。

### 5.3 持久化安全边界较成熟

`jsonl_store.py` 的严格 JSON 读取、重复键拒绝、格式验证、锁与原子替换属于高价值复杂度；旧版本
迁移失败时保留原字节的契约也有明确测试。这里可以消除局部重复，但不应为了缩短文件而拆散事务
语义。

### 5.4 主缓冲区渲染有明确物理不变量

`MainBufferRenderer` 区分语义文档、物理帧、pending 区域、cursor-only 更新与差分重绘。它虽然较
长，但复杂度围绕同一状态机形成。除非后续证据显示修改经常跨越无关区域，否则不应把它列为第一批
拆分对象。

### 5.5 Composer 与公共数据模型边界清晰

`ComposerEditor` 对粘贴片段原子性、Unicode 单元格、逻辑光标和编辑动作有集中约束；多项 dataclass
也会在构造时冻结或验证数据。后续规范应推广这种“边界完成校验、内部使用强类型值”的方式。

## 6. 高优先级发现

### A1. 单轮运行编排过度集中

- **证据**：`engine.py:155` 的 `AgentEngine.run()` 跨约 377 行，静态分支节点约 51 个；同一函数
  同时处理 Provider 流、reasoning/text 投影、Run 限制、工具批次、进度快照、展示快照、交互、
  取消、错误映射与唯一终态。
- **影响**：任何局部功能都容易触碰整个运行循环；worker 很难判断状态应加入哪个层级，倾向继续
  添加并行变量和条件分支。取消、持久化和事件顺序的正确性依赖跨越整段函数的隐含时序。
- **优先级 / 置信度**：高 / 高。
- **建议方向**：保持 `AgentEngine.run()` 的公共事件 API，先引入内部的 Run 状态/进度记录器，再
  分离 Provider step 消费和工具批次驱动。每次只迁移一类状态，并用现有生命周期测试保护事件顺序。
- **禁止误修**：不要把 Engine 改成多个互相回调、共享可变状态的小对象；拆分目标是单一状态所有
  者和单向数据流，不是降低单个函数行数。

### A2. MicaApp 与 Engine 之间存在并行结果通道

- **证据**：`app.py:163` 的 `MicaApp.run()` 在消费公共事件流的同时，把多个 list 的 `append`
  作为 completed messages、tool presentations 和 progress snapshots 的 sink 传入 Engine，再
  在终态前重组持久化对象。
- **影响**：完整 Run 结果不属于单一值或单一对象；正确性依赖 sink 调用次数、调用顺序和事件终态
  一致。新增一种持久化信息时很容易再增加第四条旁路。
- **优先级 / 置信度**：高 / 高。
- **建议方向**：定义一个仅供内部使用的 Run recorder/result contract，由它拥有进度与完成结果；
  公共事件仍按当前顺序 yield，MicaApp 只从一个内部对象取得提交快照。
- **需共同决定**：该 recorder 由 Engine 内部创建并返回，还是由 MicaApp 注入；code spec 应先
  确认状态所有者，避免形成新的双向依赖。

### A3. CLI 同时承担装配、交互前端和输入适配

- **证据**：`cli.py` 约 786 行，包含 argparse、环境配置、同步/异步入口、依赖装配、会话选择、
  TUI 生命周期、plain 事件渲染、Ask/Permission 交互和 stdin daemon thread；`_interact()` 本身
  仍是分支密集函数。
- **影响**：业务依赖变化、CLI 参数变化和 plain UI 变化会落入同一模块；分发测试需要通过大量
  monkeypatch 才能隔离入口。未来增加配置文件或其他前端时，容易复制装配逻辑。
- **优先级 / 置信度**：高 / 高。
- **建议方向**：保留现有 `main`、`_main_cli` 和兼容入口，内部依次抽出 application composition、
  session selection、plain frontend、stdin adapter。CLI 文件只保留参数到用例的转换和退出码。
- **禁止误修**：不要在拆分时引入依赖注入框架；普通构造函数和少量明确工厂已经足够。

### A4. MainBufferScreen 成为前端侧总控制器

- **证据**：`tui/main_buffer/screen.py` 约 1,009 行；`MainBufferScreen` 同时持有终端生命周期、
  escape flush、composer、全局按键、Ctrl+C 状态、Ask/Permission、事件准入、时间线、历史投影、
  工具输出、状态计时、分支刷新和 chrome 渲染。`handle_event()` 从 `screen.py:605` 开始集中分派
  多类公共事件。
- **影响**：终端输入修复、业务事件新增和视觉改动容易互相影响；这与此前焦点、滚动、交互卡片和
  composer 返工频繁互相回归的历史现象一致，但本轮静态审查不把该相关性表述为已复现因果。
- **优先级 / 置信度**：高 / 高（结构问题）；运行时因果为中等。
- **建议方向**：保留 Screen 为生命周期外壳，分别抽出输入/模式控制器、事件/时间线 projector、
  chrome/status presenter。`Terminal` 和 `MainBufferRenderer` 的物理状态所有权保持不变。
- **验证重点**：交互模式按键所有权、动态工具输出原地更新、Ctrl+C 双击语义、物理光标即时刷新和
  resize 重绘。

### A5. 契约、codec 与展示校验边界发生泄漏

- **证据**：`session_contracts.py:21-25` 从 `tool_presentation.py` 导入私有
  `_safe_relative_path`；`tool_presentation.py:12-19` 从 `tool_contracts.py` 导入多个私有校验/
  冻结函数。`session_contracts.py` 同时包含领域模型、v1-v4 serializer/deserializer、checkpoint
  wire 和展示快照解析；`tool_presentation.py:210` 的 `_validate_snapshot_payload()` 分支密集，
  会话反序列化又维护一部分相同 schema 规则。
- **影响**：以下划线标记的实现细节事实上已经成为跨模块依赖，维护者无法判断它们能否安全修改；
  同一 wire 约束存在漂移风险。
- **优先级 / 置信度**：高 / 高。
- **建议方向**：先确定 public/internal API 规则，再把共享校验移到有明确名称的内部 schema 模块；
  领域类型与版本 codec 分离，但第一批重构不改变 JSONL 格式、迁移语义或顶层公共类型。
- **禁止误修**：不要用复制私有函数来消除导入，也不要在结构重构中顺便升级 wire 版本。

### A6. 测试资产按阶段追加，缺少替换与合并机制

- **证据**：测试代码 32,709 行、收集 1,778 项；仍存在 `test_p3_05_integration.py`、多份
  `test_p4_*`、`test_p5_09_integration.py` 和 `test_p5_10g_integration.py`。项目阶段已经重新编号，
  这些文件名和大量测试 docstring 仍以旧任务号组织。至少 9 个测试模块各自定义
  `ScriptedProvider`，多处重复 `make_app`/事件收集 helper，且 `tests/` 没有统一 `conftest.py`。
  `test_app.py` 与 `test_p5_10g_integration.py` 对取消检查点、可恢复失败和后续恢复存在明显场景重叠。
- **影响**：同一行为的旧断言与新断言并存，修改者无法快速判断哪个是稳定契约；测试修复成本和
  运行时间持续增长，worker 更倾向继续追加而不敢整理。
- **优先级 / 置信度**：高 / 高。
- **建议方向**：按稳定行为层重新编目：纯契约、组件行为、跨层生命周期、CLI/分发、真实平台验收。
  先建立共享 provider/app harness，再逐个合并重复场景；语义替换时修改或替换旧测试，不新增一份
  带新任务号的同义测试。
- **禁止误修**：不得按测试/源码行数比例设删除配额，不得因为测试文件旧就删除兼容迁移、安全负向
  或分发安装证据。

## 7. 中优先级发现

### B1. 中立执行层反向依赖具体内置工具模块

- **证据**：`tool_executor.py:14` 从 `runtime_tools.py` 导入 `ToolOutputReporter`；该类型实际只是
  一个中立的 callable alias。`runtime_tools.py` 又同时实现 output reporter、Bash 进程控制、Ask
  handler 和工具注册。
- **影响**：核心执行器在类型层依赖 Bash/Ask 的具体实现容器，依赖方向与职责名称不一致；继续增加
  内置工具会使该模块更杂乱。
- **建议方向**：把 output callback 移到执行契约模块；把 Bash 与 Ask 分成独立 built-in tool
  模块，保留一个小型注册入口。

### B2. Terminal 文件聚合了三个可独立变化的边界

- **证据**：`tui/main_buffer/terminal.py` 同时实现 prompt_toolkit Windows VT fallback、ANSI
  禁止序列扫描和物理 Terminal IO/resource owner。
- **影响**：依赖版本兼容、输出安全规则和终端资源管理的变化原因不同；Windows reader 使用动态
  子类适配具体 prompt_toolkit reader，对上游版本较敏感。
- **建议方向**：在不改变 `Terminal` IO 所有权的前提下，考虑拆成 windows input、ANSI guard 和
  terminal IO 三个内部模块。只有当相关测试能够独立保护这些边界时再执行。

### B3. OpenAI-compatible parser 与 transport 状态耦合较密

- **证据**：`providers/openai_compatible.py:365` 的 `stream()` 约 168 行、分支节点约 36 个；
  `_parse_chat_chunk()` 也承担多种 chunk 校验与转换。
- **影响**：SSE transport、协议错误映射、工具调用累积和事件输出在同一控制流内，后续兼容不同
  provider 方言时可能继续增加条件。
- **建议方向**：先通过当前 provider 测试固定协议，再把 SSE/chunk 解码和 tool-call accumulator
  抽成内部纯状态组件。优先级低于 Engine、CLI 和 Screen。

### B4. 顶层公共 API 的范围大于文件自述

- **证据**：`mica_agent/__init__.py` 自述只暴露跨模块公共契约，但 `__all__` 还包含存储/服务、
  workspace helper、具体工具注册、展示构造和若干运行入口；同时通过 `__getattr__` 维持惰性导入。
  `test_public_api.py` 对这些符号及导入副作用有大量精确断言。
- **影响**：无法仅凭代码判断这些导出都是长期兼容面，还是为历史测试形成的便利入口；内部重构前
  缺少公共/内部 API 的裁决标准。
- **建议方向**：code spec 先定义三层：稳定公共契约、项目内部跨模块契约、模块私有实现。然后逐项
  审核顶层导出；惰性导入和安装后行为是现有契约，不能在没有迁移方案时删除。

### B5. 注释与 docstring 混入施工历史和失效未来时

- **证据**：有效 prose 注释中约有 29 条英文、62 条中文和 55 条中英混合；docstring 以中文或
  中文加英文标识符为主，但存在少量纯英文。更重要的是，活跃源码仍出现 `P2-02`、`P2-03`、
  `P2-04`、`P4-03`、`P4-04`、“阶段计划 4.x”、“未来 ToolExecutor”、“后续 Provider 实现”和
  “后续策略引擎”等已经失去当前含义的说明。
- **影响**：注释既不能稳定解释现有不变量，也会把历史阶段编号误当成当前架构事实；worker 会继续
  模仿这种写法。
- **建议方向**：所有源码注释与 docstring 统一使用英文，代码标识符和稳定错误文本继续保持英文；
  注释只解释原因、不变量、外部限制、兼容条件和非显然风险。阶段号与施工记录只属于计划/构建日志。
- **执行时机**：先完成主要模块移动，再清理注释，避免同一内容重复修改。

### B6. 少量测试仍依赖固定时间等待

- **证据**：多个 CLI、Provider、runtime tool 和 TUI 测试仍使用 `asyncio.sleep(0.05)`；
  `test_runtime_tools.py` 有 4 次 `time.sleep(2.3)` 用于真实后代进程清理证明。大量 `sleep(30)` 或
  `sleep(3600)` 是被取消的 hanging fake，不能与真实等待混为一谈。
- **影响**：短固定等待在负载变化时可能产生不稳定测试；真实 2.3 秒等待会显著拖慢套件，但它也
  可能承担跨进程平台行为证据。
- **建议方向**：短等待优先改成可观察事件或有界 `wait_for`；后代进程测试先调查能否暴露确定性
  cleanup signal，若不能则保留为独立的慢速平台测试并明确原因。

## 8. 测试体系判断与目标结构

用户提出的“边开发边写大量小单元测试导致测试膨胀”判断，证据支持其中的“持续追加且缺少整理”，
但不支持停止近改动测试或简单减少单元测试。更合适的修正是让测试成本与行为风险成比例，并要求
新测试替换过时资产，而不是永久叠加。

建议把测试分成以下层级：

1. **纯契约测试**：dataclass、解析、schema、安全清洗、路径和不可变性；允许细粒度，速度必须快。
2. **组件行为测试**：Engine step、ToolExecutor、Store、Renderer、Editor 等单一状态所有者；验证
   公共或稳定内部契约，不重复每条实现分支。
3. **跨层生命周期测试**：只保留少量高价值链路，覆盖成功、取消、可恢复失败、权限/Ask、持久化
   顺序和资源关闭。
4. **CLI 与分发测试**：保护 wheel/sdist、安装后导入、入口和少量端到端行为；不要把所有业务组合
   都复制到安装测试。
5. **平台行为与人工验收**：Windows VT、真实终端视觉、子进程树等无法由普通单元测试充分证明的
   内容，单独标识并控制运行频率。

测试清理时采用以下判断：

- 两个测试保护同一输入、同一输出和同一失败模式时，保留表达稳定契约更清晰的一份；
- 旧测试保护兼容读取、安全边界或历史数据迁移时，即使文件名过时也不能直接删除；
- 测试直接读取产品私有字段时，先判断是否存在可观察结果或可注入协作者；确实需要验证内部算法
  不变量时可以保留，但应集中在对应组件测试；
- 共享 fake/harness 只抽取稳定协议和构造，不建立隐藏大量默认行为的万能 fixture；
- 任务号不得作为长期测试分类，测试名称应说明产品行为。

## 9. 建议进入 code spec 的候选规则

以下内容是根据本轮证据提出的候选规则，尚需与用户共同讨论后才能写入正式 code spec：

- **先定状态所有者**：每项可变运行状态只能有一个明确所有者，其他层通过不可变值、事件或窄接口
  使用，不以多个 list、dict 或 callback 旁路共同拼装事实。
- **依赖只指向契约**：核心编排层不得为类型或工具函数反向依赖具体内置工具、前端或存储实现。
- **边界完成校验**：外部输入在 Provider、CLI、JSONL、tool schema 和终端边界完成校验与转换，
  内部逻辑使用已经验证的强类型值。
- **模块按变化原因拆分**：文件长度只触发审查；只有模块存在多个独立变化原因、状态所有权或依赖
  方向时才拆分。
- **公共层级显式**：稳定公共 API、项目内部跨模块契约和模块私有实现必须可区分，跨模块不得导入
  下划线私有符号。
- **取消必须显式传播**：异步任务、Provider 流、工具、子进程和前端关闭必须定义取消与资源释放
  责任，禁止吞掉 `CancelledError` 或用无界重试掩盖终止。
- **注释解释原因**：源码注释只记录非显然原因、不变量、外部限制、兼容条件和风险，不记录阶段号、
  施工过程或代码逐句翻译。
- **测试保护行为**：测试优先验证稳定行为和安全边界；语义被替换时同步修改或删除旧测试，不为每次
  返工永久追加一套同义断言。
- **真实边界单独验收**：安装包、Windows 终端、子进程和外部服务等真实边界必须与模拟测试的结论
  分开陈述。

## 10. 建议整改顺序

### 第 0 批：定稿 code spec 与行为保护面

- 明确 public/internal/private API；
- 明确模块职责、依赖方向、注释语言与测试分层；
- 从现有测试中选出 Engine、MicaApp、Screen、JSONL 和终端的核心 characterization tests；
- 不新增产品功能。

### 第 1 批：收敛内部契约

- 建立单一 Run recorder/progress contract；
- 移动 `ToolOutputReporter` 到中立执行契约；
- 建立展示与会话 codec 共用的公开内部 schema helper；
- 不改变公共事件、JSONL wire、顶层 API 和终端行为。

### 第 2 批：拆解 Engine 与 MicaApp 的并行状态通道

- 分离 Provider step、工具批次和进度记录；
- 保持唯一终态、persist-before-terminal、取消与恢复语义；
- 每次迁移只改一个状态所有者。

### 第 3 批：整理 CLI composition root

- 分离装配、会话选择、plain frontend 和 stdin adapter；
- 保持现有 CLI 参数、退出码、`--plain` 和默认 TUI 行为；
- 简化分发测试中的入口 monkeypatch。

### 第 4 批：拆解 MainBufferScreen 的控制职责

- 抽出输入模式、事件 projector 和 chrome/status；
- 不迁移 `Terminal` 与 `MainBufferRenderer` 的物理状态；
- 用真实 Windows Terminal 验收键盘、光标、Ask/Permission 和 resize。

### 第 5 批：测试资产重编目与合并

- 把阶段编号测试迁移到行为命名；
- 建立少量共享 fake/harness；
- 合并 App、Engine 与 P5-10G 等重复生命周期场景；
- 识别并隔离慢速平台测试；
- 删除前逐项证明行为仍被其他测试覆盖。

### 第 6 批：注释、docstring 与局部风格统一

- 删除阶段号、施工记录和失效未来时；
- 为真正的公共契约和关键不变量补充简洁英文 docstring；
- 不给显然代码添加解释性噪声；
- 最后同步 `module_map.md` 和正式 code spec。

## 11. 不建议采取的做法

- 不进行全仓一次性重写；
- 不以“单文件不得超过 500 行”作为机械门禁，行数只能触发职责审查；
- 不以测试代码必须少于产品代码为目标；
- 不在缺少覆盖证明时批量删除旧阶段测试；
- 不在同一任务中同时重构 Engine、改变事件协议和升级会话 wire；
- 不为减少 diff 而增加 adapter、deprecated shim 或新旧双轨逻辑；
- 不在结构仍会移动时先做全量注释翻译；
- 不把 Renderer、Store 等合理复杂度仅因文件较长就列为首批拆分对象。

## 12. 需要共同决定的问题

进入实际整改前，需要在 code spec 讨论中确定：

1. `AgentEngine` 的内部 Run recorder 应由 Engine 创建还是由 MicaApp 注入；
2. 顶层 `mica_agent` 包哪些符号承诺长期兼容，哪些只属于项目内部；
3. 领域模型与 v1-v4 codec 的目标模块边界，以及是否允许内部子包；
4. TUI 是否采用 controller/projector/presenter 命名，还是使用更贴近终端领域的名称；
5. 测试目录按领域、层级或两者结合组织，哪些平台测试从默认全量中独立出来；
6. 文件或函数复杂度达到什么条件时必须触发人工设计审查，而不是设硬性行数上限。

## 13. 最终判断

Mica 目前最需要的不是更多局部补丁，也不是为了整洁而大规模拆文件，而是一套能让 agent 在动手前
判断“状态属于谁、依赖应指向哪里、这个测试在保护哪一层行为”的共同规范。第一轮整改应从内部契约
和编排边界开始，测试清理与注释统一跟随结构稳定后执行。这样既能降低当前维护成本，也能避免新的
worker 在看似通过局部测试的前提下继续扩大整体耦合。

本报告完成后，下一步应先讨论并定稿 `docs/code_spec.md`，再把上述整改顺序拆成可独立审查、可回滚
且不改变已验收产品行为的小批任务。

## 14. 补充发现（2026-09-26）

整理文档时通过导入关系静态检查发现，原报告未列出：

- **B7. Provider 契约包导出具体实现**：`providers/__init__.py` 的 docstring 声明只暴露供应商无关
  的接口，但同时导出 `OpenAICompatibleProvider`；`engine.py`、`app.py` 从 `providers` 包导入契约
  时会连带加载具体实现。与 A5、B1 同属“依赖指向契约”问题，建议在第 1 批一并收敛。
- **A5 补充**：`events.py` 也从 `tool_contracts.py` 导入私有 `_validate_tool_name`。
- **观察项**：`tui/models.py` 从 `context_builder.py` 导入 `AgentFileSource` 枚举，前端直接依赖
  能力实现模块；是否把该枚举提升为契约，随 public/internal API 分层一起决定。
