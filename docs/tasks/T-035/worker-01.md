# 第 1 轮汇报 · 2026-10-23

依据：[T-035.md](T-035.md)。

- 结果：完成 —— 已分别交付官方核对、离线诊断和真实对照包；两种现象的真实根因仍待验证，未提交或推送。
- 改动：`worker-01.md`、`probe_cases.json`、`test_messages_probe.py`、`validation.log`，均在本卡目录。
- 验证：Messages 原专项 171 passed；补充诊断 3 passed；现有装配/切换专项 9 passed、55 deselected；诊断文件 Ruff check/format 通过。命令、初次失败和修正见 [validation.log](validation.log)。
- 偏离与疑点：实际 HEAD 为 `061110f`，相对卡片产品基线 `47b7ef1` 仅增加派工文档与 STATUS，无产品差异；当前 main、初始工作区干净，符合串行诊断位置。官方字段参考页两处抓取失败，空 text block 的精确服务端限制未核实，未把该项当作已确认缺陷。
- 建议：先进行下面 A 阶段的新会话对照，再决定是否进入 H 阶段；本卡不建议基于猜测修改编码、默认策略或删除历史。B 与 Phase 6 仍待真实验收。

### 明细

- [x] 官方协议核对及字段对照：见下文，未核实项明确保留。
- [x] 实际出站工具声明与本地真实 read 往返证据：原专项与新增 3 项诊断。
- [x] 两种现象独立证据及未决问题：分别列于现象 A、H。
- [x] 独立请求基准和真实对照方案：JSON 内含用例、门槛、上限及停止条件；worker 未执行。
- [x] 专项测试、静态检查、diff 与写入范围核对：测试/静态已通过；JSON、预算合计、四文件空白检查通过，git diff --check 无输出；产品/正式测试/依赖相对基线无差异，暂存区为空。

### 证据分类与结论

本轮没有确认与已知现象直接对应的产品缺陷，也没有复现用户原会话的远端拒绝。已排除的只是本文指定输入下的本地断点，不是所有可能历史与响应。全文标签含义：**官方核对**为本日实际读取的官方正文；**静态核对**为当前源码/测试；**离线复现**为本轮执行的假 transport 测试；**历史真实证据**仅引用任务卡与 STATUS 已记录摘要，未读取原始响应；**待真实验证**为下一轮才能裁决的问题。

#### 现象 A：请求接受后未产生工具调用

| 证据等级 | 观察及其边界 |
|---|---|
| 历史真实证据 | `docs/STATUS.md:20`：一次 Messages 出站带五工具和对象 schema，HTTP 200/SSE，仅文本、end_turn、message_stop，无 tool_use/工具执行。只证明那次普通文本完成；本轮没有重跑。 |
| 官方核对 | 有 tools 时默认 auto 允许直接回答，工具声明存在并不保证本次选工具。模型正文声称“没有工具”不能作为缺失声明证据。 |
| 静态核对 | `cli.py:430` 注册文件工具和 runtime 工具；`app.py:53` 选 Messages 适配器，`:595` 传递绑定工具；`providers/base.py:29` 请求携带冻结 ToolSpec；`anthropic_messages.py:691` 原样转为 name/description/input_schema，不发送 tool_choice，`:700` OFF 为 disabled。 |
| 离线复现 | 新诊断 `test_messages_probe.py:83` 两个参数分支，检查真实 HTTP 请求体后返回分片 tool_use SSE；Provider → Finish.tool_calls → Engine → ToolExecutor → 产品 read → 下一请求含匹配结果 → RunCompleted 全链路通过。 |
| 待真实验证 | 渠道是否完整转发工具、实际模型/版本是否按工具协议工作、系统指引及任务是否影响选择；当前无法从已有证据区分这些假设。不得宣称真实模型支持或不支持工具。 |

#### 现象 H：已有工具历史被远端 HTTP 400 拒绝

| 证据等级 | 观察及其边界 |
|---|---|
| 历史真实证据 | `docs/STATUS.md:28` 两次隔离切换：切换及编码成功，ID 匹配、结果紧随调用、无空 text/签名、disabled、五工具保留；随后 HTTP 400。错误摘要涉及 content/tool_use，不足以重建具体字段错误或确定用户原会话同因。 |
| 静态核对 | `anthropic_messages.py:603` 将通用工具事实编码为对象 input 的 assistant tool_use；`:655` 合并紧随的 tool 结果成 user tool_result，结果先于随后用户 text；非兼容续接只保留通用事实。`:723` 将 400 归为普通 ProviderError，`:737` 后的重试仅针对超时/临时错误，不因 400 剥离续接或重试。 |
| 离线复现 | 新诊断 `test_messages_probe.py:208` 用手写 JSON 后缀核对通用历史实际出站编码。无正文 assistant 只含 tool_use，不生成空 text；id、角色、input 对象、结果顺序正确。它与前一诊断原生 Messages 无思考的工具后缀结构一致。 |
| 离线复现 | 现有 `tests/test_app_interfaces.py:378` 六方向切换与切回，`:311` 三接口工具往返专项本轮合计 9 passed；证明装配保存通用事实/过滤私有续接。均为合成流和合成工具，不是渠道兼容性证明。 |
| 待真实验证 | 具体被拒字段、渠道工具历史转换、目标模型/版本的接受规则，以及原生 Messages 历史是否也拒绝。模拟 HTTP 400 仅能证明本地错误分支；不是复现历史 400 根因。 |

A 是请求接受后的工具选择观察，H 是远端请求校验拒绝；现有证据不支持合并归因。H 中“无空 text”已有历史摘要支持，因此不能将空 text 假设反推为该次 400 的解释。

### 官方核对与编码对照（2026-10-23）

实际读取以下公开官方正文；不依赖搜索摘要。以下简称对应准确链接：

- O：[Tool use overview](https://platform.claude.com/docs/en/agents-and-tools/tool-use/overview)。
- D：[Define tools](https://platform.claude.com/docs/en/agents-and-tools/tool-use/define-tools)。
- H：[Handle tool calls](https://platform.claude.com/docs/en/agents-and-tools/tool-use/handle-tool-calls)。
- T：[Thinking in tool and multi-turn workflows](https://platform.claude.com/docs/en/build-with-claude/thinking-tool-workflows)。
- S：[Streaming messages](https://platform.claude.com/docs/en/build-with-claude/streaming)。
- M：[Create a Message](https://platform.claude.com/docs/en/api/messages/create) 与 [Using Messages](https://platform.claude.com/docs/en/build-with-claude/working-with-messages)。
- A：[API overview](https://platform.claude.com/docs/en/api/overview) 与 [Versions](https://platform.claude.com/docs/en/api/versioning)。

| 字段/规则（官方依据） | Mica 实际位置 | 本轮/既有证据 | 尚待真实确认 |
|---|---|---|---|
| D：name 满足字母数字/下划线/连字符、1–128 字符；description 说明行为；input_schema 定义参数 | `anthropic_messages.py:691` | 原测试 `:211`；新诊断 `:83` 直接检查 HTTP body，而非仅 ToolSpec | 实际渠道是否转发全部定义；未声称本地 schema 校验等于远端接受 |
| D：tools 存在时默认 auto；any/tool 为强制；manual thinking enabled 不允许强制。当前正文另列部分模型不论思考配置均不允许强制 | `anthropic_messages.py:683` 无 tool_choice；`:700` OFF → disabled | 原测试 `:211` 和新手写请求全体比较 | 必须核对目标确切模型与渠道能力，不能仅依据 disabled 启用强制 |
| H：tool_use 的 id 唯一、name 指向工具、input 为对象；结果在紧随的 user 消息，tool_use_id 对应；结果块在该消息其他 text 之前 | `anthropic_messages.py:603`、`:655` | 原测试 `:502` ID 映射、`:624` 缺失/重复/孤立结果前置拒绝；新 `:208` 独立期望 | 用户历史具体字段不在本卡；不作重建 |
| H：result content 可为字符串/支持的块数组，可为空；is_error 可选 | `anthropic_messages.py:665` 使用真实结果字符串；`engine.py:729` 生成状态/消息/data JSON 字符串 | 新诊断检查实际随机 data 与匹配 ID；原测试 `:1229` 不从文字推断失败 | 渠道若要求非标准封装需另外举证；JSON 字符串本身不是错误 |
| M：content 可为字符串或类型化 block 数组，system 单列；工具调用不要求额外文本说明 | `anthropic_messages.py:590`、`:603`；空正文且有工具时省略 text | 新诊断 native/generic 均为仅 tool_use 的 assistant；原 `:211` system 分离 | 本次可访问页未核实空 text block 的精确拒绝规则。源码对普通空 user/system/无工具 assistant 仍可能编码空 text，不能由此认定历史 400 根因 |
| M：max_tokens 为上限，模型可提前停止；请求必须给预算 | `anthropic_messages.py:683` 未提供时 4096；base 请求显式值要求正整数 | 原 `:211`，新基准固定 1024 | 实际目标的预算范围；当前文档允许 0 用于仅缓存，但本卡生成实验不使用 0，也不扩展产品契约 |
| A：JSON、版本头与认证；现文档 x-api-key 为仍支持的认证后备，Bearer 为另一方式，部分凭据需 workspace 头；版本历史含 2023-06-01 | `anthropic_messages.py:753` 发 x-api-key、2023-06-01、JSON；URL userinfo 被清除 | 原 `:211`；新诊断在 transport 检查假认证和版本头；没有打印认证值 | 特定真实渠道及凭据的头要求不在本卡；不能因文档出现新版本就认定旧版本无效 |
| S：message_start → block start/delta/stop → message_delta → message_stop；input_json_delta 是 JSON 片段，最终 input 为对象 | `anthropic_messages.py:284`、`:375`、`:399`、`:514` | 原 `:391` 分片、`:960` 禁止不完整工具执行；新诊断跨 JSON 分片真实读取 | 真实无调用那次没有 tool_use，不能归咎解析器漏掉本轮从未见到的帧 |
| T：工具往返保留完整未改 thinking/redacted_thinking；同一 assistant turn 保持思考配置 | `anthropic_messages.py:615` 绑定匹配才回放，带签名需要改 ID 时拒绝 | 原 `:543`、`:578`、`:611`、`:1364` | 本卡 live 方案关闭思考；不扩大为签名/恢复已真实验收 |

抓取限制：`/docs/en/api/messages` 响应过大；`/docs/en/api/messages/text-block-param` 工具不可访问。没有绕过限制；核心要求可由上表其他已读正文核实，空 text 的精确限制仍标未核实。当前官方 D 页面列举了不支持强制选择的模型（含 Opus 5.5、Sonnet 5.5、Fable 5.1、Mythos 5.1），这是官方现时规则，不能推定用户实际模型属于任一项。

### 离线实验与五工具审查

隔离在 pytest 初始化前设置进程 HOME/USERPROFILE 和配置相关目录，移除凭据环境项，不读取真实配置/会话；假 Key、Mock transport、不访问渠道。新增测试禁止 DNS 和外部 socket connect，仅允许 Windows asyncio 所需 IPv4 回环。执行器仅注册 read，路径精确等于新建临时工作区内的 `probe.txt`，内容为本轮 UUID 随机串；计数守卫要求只调用一次，然后转交原产品 read handler，未 fake 文件读取、编码、解析、Engine 或结果封装。临时目录由 pytest/OS 管理，不作递归清理。

原 `test_real_engine_two_step_multi_tool_roundtrip_and_original_order`（`:856`）覆盖两步、多工具、续接及顺序，但执行的是合成 lookup，所以没有重复重写该测试；新增诊断补其产品 read 和独立基准断点。五工具分支仅在请求中声明五工具，实际执行器仍仅能执行指定 read。

| 工具 | 实际定义位置与核查 | 观察 |
|---|---|---|
| read | `file_tools.py:31`：path 非空 string，required path，禁止额外字段 | 与手写基准完整 schema、description 一致 |
| write | `file_tools.py:41`：path/content string，均 required，禁止额外字段 | 对象 schema，无执行 |
| edit | `file_tools.py:54`：path/old_text/new_text string，均 required | 对象 schema，无执行 |
| bash | `runtime_tools.py:63`：command 非空 string，required command | 对象 schema，无执行 |
| ask | `runtime_tools.py:73`：required question；可选 title/options/allow_custom，options 为对象数组 | 对象 schema，无执行 |

实际五工具请求均具有 name/description/input_schema 三字段，非 Completions 的 function 包装，名称合法；新测试在网络边界逐个对比当前注册定义。描述均较短，官方建议更充分描述用途，但这是工具选择质量的待测因素，不是协议必填字段错误，也不能解释已观察的 400。没有发现五工具之间足以判定当前现象根因的可疑结构差异。

### 真实对照准备（worker 未执行）

[probe_cases.json](probe_cases.json) 是手工写定的独立最小请求及合成历史期望；未调用 Mica 编码器生成期望。新测试与其逐字段比较。文件没有认证值、真实地址、真实模型配置或用户会话。`synthetic_history_suffix` 仅用于离线字段比较，不能在真实实验中冒充执行结果。

前置：由用户参与并另行确认真实凭据/渠道边界，核对目标协议、模型、disabled 支持和工具选项；新建隔离 HOME/配置/会话/工作区与一个指定随机内容文件。所有真实执行限制为该文件一次 read，其他工具及路径拦截。请求统一 30 秒、自动重试 0。每个编号只运行一次；任何错误或未知字段、额外调用、超预算、终态缺失均停止留白名单摘要，不盲重试。

| 用例/变量 | 请求预算与停止条件 | 信息增益 |
|---|---|---|
| A1 独立协议基准、auto（省略 tool_choice）；A2 Mica 同模型/任务/单 read/schema/1024/disabled/版本 | 各最多 2 请求：调用与结果续问；首答无 tool_use 则 1 次停止，不伪造结果；异常立即停 | 独立侧与本地链路对照。对齐不全时不能仅凭结果差异判 Mica bug |
| A3 只改 tool_choice 为指定 read | 仅 A1/A2 无调用且精确目标支持强制时运行，最多 2；选项拒绝即停止 | 区分 auto 选择与可被明确请求的工具能力；只用于独立诊断，不改产品默认 |
| H0 离线对比既有编码 | 0 个真实请求；同一通用事实与原生 slot 输入比较 id/input/角色/块顺序/结果 | 无需消耗真实预算先发现转换差异；本卡最小样本已相符 |
| H1 原生 Messages 新实验历史 | 仅新会话工具往返明确后，用 A1/A2 新获真实历史，最多 1 个续问；禁止额外工具执行 | 确定原生历史也拒绝还是仅转换情形拒绝；无可复用历史则停止重新确认预算 |
| H2 新建 Completions → Messages | 最多 3：Completions 两步 read + 一次 Messages；Completions 无工具事实即停，不以文字补 tool fact；400 不重试 | 对照跨接口转换后的接受结果；只读本次隔离实验历史，绝不读取用户原会话 |

总预算上限 10 次（A1 2 + A2 2 + 条件 A3 2 + H1 1 + H2 3），不是要求全部花完；模型最大输出预算各 1024。每次只改表中一个变量。H1/H2 必须先离线列出差异：真实模型生成的工具 ID/正文/结果封装可能不同，不能自然当成严格受控同一输入；必要时先止于结构比较，再由用户/master决定进一步实验，不增加预算。真实结果续问使用实际观察的 assistant blocks 与真实 read 结果，不能使用 JSON 的合成成功后缀。

A3 仅第一请求指定 read；真实 read 后的结果请求恢复默认 auto（省略 tool_choice），允许文本结束，不能把强制选择继续带入而要求再次调用 read。H1/H2 在实际完成的新轮次历史之后追加 JSON 中相同的 `history_followup_prompt`，禁止额外工具执行。生命周期规则在运行前固定，不以结果好坏临时调整。

A1/A2 剩余差异必须登记：默认系统指引、工具顺序（单工具已消除）、完整历史中的包装、返回内容、客户端附加头、实际模型版本；Mica 最小 Provider 链路可在无 system 时与基准完全一致，但实际 CLI 会有应用系统指引。不能静默删产品指引来制造相同条件。保持所选旧版版本头先不变，新文档版本不是本轮变量；如渠道不支持已选字段，停止并重新形成单变量方案。

白名单观察：出站字段名/类型/块类型序列与数量、schema 一致布尔值、ID 关联是否匹配（不用保存 ID 值）、预算/disabled/choice 模式、HTTP 状态、tool_use 是否出现、是否形成 Finish.tool_calls、是否进入 Engine 工具事件、唯一获准 read 是否成功、是否发含关联结果的第二请求、stop_reason/message_stop/RunCompleted 是否正常。仅记录已允许的枚举/计数/布尔值；错误只记录已核对白名单字段类别，不能保存原始错误、参数、正文、随机文件内容、凭据、服务地址或本地绝对路径。边界拦截、HTTP 非成功、不完整终态或异常工具都立即停止。未观察项目留为未知，不能把模型文字当工具事实。

### 验证、失败与未执行项

Messages 专项 171 passed；补证 3 passed；现有装配/跨接口 9 passed、55 deselected（按用例名选择，非跳过失败）。补证文件 Ruff check/format 通过。测试版本同本卡当前产品基线；没有修改产品、正式测试或依赖，不要求全量/构建/安装复验。

初稿静态检查发现 18 个无效 E402 noqa（规则未启用），移除注解后检查通过，没有禁用规则。补证首跑 3 failed 的第一错误为网络守卫在 `asyncio.run → socket.socketpair` 处挡住 Windows 事件循环本地唤醒，Provider 尚未执行；其后出现 coroutine/unraisable 清理告警。依据调用栈和卡片允许回环的边界修正守卫后，3 项在 `-W error` 下通过，保留外网拒绝。该失败属于探针环境，不是产品缺陷；日志保留首错和修正原因，未吞错/跳过/改变产品断言。

未执行：任何真实 Messages/Completions/Responses 请求、真实凭据或用户会话读取、真实终端/恢复/取消/思考续接验收、全量/构建/安装、暂存/提交/推送。现有装配测试包含 Responses 的离线分支，未触及暂缓的真实 Responses 渠道。

交接注意：`validation.log` 已落盘，但被既有 `.gitignore:27` 的 `*.log` 规则忽略，因此普通 git status 仅显示另外三个新增文件。没有修改忽略规则或执行强制暂存；master 可直接读取此日志，后续是否纳入 Git 由 master 按授权决定。四个交付文件均通读核对；本轮没有范围外改动。
