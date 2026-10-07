# 速记本

> 讨论中已确认、还没整理进正式文档的内容。由 master 维护，用户也可以直接改。
> 整理进正式文档后，删掉对应的内容。
>
> 本轮：Phase 6 规划。输入：`docs/phase6_plan.md` 旧稿、`docs/configuration_spec.md`（用户决定不保留，计划写成后删除）、D-017、D-020～D-024。A1 已由 T-021 交付。

## 交付拆分

范围：A1 之后还剩哪些交付项、先后顺序和依赖、各项怎么验收、哪些需要用户真实验收。

待确认事项清单：
- [x] 剩余交付项的划分和顺序（旧稿：A2 → B → C → D）
- [x] 每项的验收方式和用户真实验收项目
- [x] 主线整体验收（端到端流程、最终验证命令）

讨论记录（已确认项）：
- Q：剩余交付项怎么划分？
  A：A2 → B → C1 API Key 登录、目录与连接维护 → C2 ChatGPT 官方订阅 → D。
  理由：订阅需要新依赖和真实账号验收，单独一项可以独立验收，失败也不拖住其他渠道。
- Q：各交付项和整条主线怎么验收？
  A：每项交付在最后一张卡通过后，由用户做真实验收：
    - A2：Windows Terminal 里的 `/` 候选和 `/model` 菜单；
    - B：三种接口各有一条真实渠道的工具对话，并跨接口切换一次；
    - C1：OpenRouter、DeepSeek、OpenCode Go 的登录和目录；
    - C2：真实 ChatGPT 授权和一次工具对话；
    - D：设置、重载和忙时提醒。
  主线收口时，跑全量 `uv run pytest -q -W error`、`ruff check`、`ruff format --check`；依赖变化后再跑 `uv lock --check` 和 `uv build`。账号或额度不足的项目标为未验收。
  理由：问题在最后才暴露会拖整个主线。

## 需求细化

范围：每个交付项的具体行为，包括正常流程、边界情况和失败表现。

待确认事项清单：
- [x] configuration_spec 中已确认的需求如何处理：哪些写进计划「要实现」，哪些删除
- [x] A2：统一命令入口、命令候选、`/model` 菜单与切换的行为
- [x] B：Responses、Messages 两个新接口和跨接口历史续接的行为
- [x] C1：`/login` 的 API Key 渠道、目录发现、连接维护的行为
- [x] C2：ChatGPT 官方订阅的登录、刷新和推理行为
- [x] D：`/settings`、`/reload` 和多窗口生效的行为

讨论记录（已确认项）：
- Q：configuration_spec 删除后，里面已确认的需求怎么处理？
  A：用户可见的行为和边界仍视为已确认，写进各交付项的「要实现」；锁、原子写、wire 字段这类实现细节不进计划，写任务卡时再定。
  理由：推荐时的理由——已确认的需求不丢，计划只保留到可派卡的粒度。
- Q：切换到上下文更小的模型时，A2 的容量提醒做到什么程度？
  A：只比较新旧模型的 contextWindow：新模型更小时提示「上下文会缩小，历史可能超限」并要求确认，不估算 token；真超限由服务端正常报错。精确用量统计随阶段 7 的上下文用量一起做。
  理由：现有代码没有历史 token 估算，A2 提前做一套可能在阶段 7 重做。

## 实现方案

范围：每项落在哪些模块、要不要新增模块，关键接口、数据结构和数据格式，要引入的依赖。

待确认事项清单：
- [x] 讨论所需的现有代码事实（T-023 已汇报，见卡末）
- [x] A2 的模块落点和关键接口
- [x] B 的模块落点、Provider 接口变化和续接数据格式
- [x] C1 的模块落点
- [x] C2 的模块落点、认证依赖
- [x] D 的模块落点

讨论记录（已确认项）：
- Q：讨论实现方式所需的代码事实怎么获取？
  A：派只读调研卡，worker 汇报现状、接口和可选落点，不改代码。
  理由：规划流程规定 master 不自己通读代码。
- Q：A2 的统一命令入口放在哪里？
  A：新建 `tui/main_buffer/commands.py`，放固定登记（名称、说明、是否可用、处理函数）及匹配和分派；候选列表和实际执行用同一份登记。screen 仍是唯一按键入口，只负责调用；running 状态的输入门禁要调整，让忙时可以编辑草稿并提醒。
  理由：screen.py 已承担编排、模式机和事件投影，后续命令进来会继续变胖。
- Q：`/model` 菜单组件放在哪里？
  A：新建 `tui/main_buffer/menus.py`，放本地菜单的状态、筛选和绘制，复用 interactions.py 的面板布局（`PanelView`、`fit_panel_rows`）。本地菜单与 Ask/Permission 语义分开：Esc 只关菜单，不取消 Run。C1 的多选、遮蔽输入和 D 的设置菜单以后也放这里。
  理由：Ask 单选会生成模型回答、Esc 会取消 Run，不能直接当配置菜单。
- Q：两轮 Run 之间切换连接由谁负责？
  A：扩充 `MicaApp`：Provider 创建从 CLI 挪到 App，App 新增空闲切换接口，顺序为校验 → 准备新连接 → 保存会话选择 → 安装 → 关闭旧连接；准备或保存失败关闭候选、保留旧连接。SessionService 新增更新会话选择的方法，复用 `JsonlSessionStore.update`。
  理由：App 已持有 Provider 和忙状态，不出现第二个所有者。
- Q：B 的供应商续接数据（Responses reasoning item、Messages thinking signature）放在哪里？
  A：新建独立的「模型步骤续接记录」契约：强类型、有长度上限，绑定供应商 + 模型 + 接口 + 模型步骤，和 Message/ToolCall 分开。ProviderRequest 传入、Finish 返回；Engine 在本轮工具往返中携带；TurnEntry 和中断检查点只保存通过清洗判定的记录；SessionService 恢复时返回消息加续接。通用对话事实不变。
  理由：挂在 Message 上会改变公共 API，纯思考、无正文的步骤也塞不进「中间消息只允许工具往返」的约束。
- Q：B 的两个新适配器和续接契约放在哪里？
  A：新建 `providers/openai_responses.py`、`providers/anthropic_messages.py`，各自负责请求编码和 SSE 解析，都用 httpx；续接契约放新建的 `continuation_contracts.py`（公共契约组）。App 按模型的 api 字段从一张固定表里选适配器。三个适配器共用的 SSE 读取和错误分类，只在确实重复时抽到 `providers/base.py`。
  理由：不先改已验收的 Chat Completions 适配器。
- Q：C1 写 models.json 和 auth.json 的能力放在哪里？
  A：models.json 的写入放在 ConfigStore，和 config.json 共用一个内部原子替换步骤。新建 `credential_store.py` 管 auth.json：安全创建目录和文件、先设权限再写明文、锁内重读合并、删除和刷新共用归属锁；C2 的 OAuth 整组刷新也用它。
  理由：普通配置和凭据安全分开归属。
- Q：C1 的模型目录发现放在哪里？
  A：新建 `model_catalog.py`（配置与选择组），按渠道用 httpx 请求模型列表，解析 ID、名称和明确的能力，有界、可取消。内置渠道的基址、目录地址和接口映射写成这里的一张固定表。目录请求和推理请求分开，不改 Provider 协议。
  理由：目录格式按渠道而不是按协议区分（OpenRouter 和 DeepSeek 都用 Chat Completions，目录格式不同）。
- Q：C2 的 ChatGPT 订阅登录放在哪里，本地回调怎么做？
  A：新建 `chatgpt_auth.py`（配置与选择组），管授权流程、token 交换和刷新。本地回调用标准库 `ThreadingHTTPServer`，在受控线程里运行，只绑 127.0.0.1，有超时和取消，结果安全交回事件循环；用 webbrowser 打开浏览器；state、nonce、PKCE 用标准库生成。
  理由：自写 asyncio 接收器要自己做 HTTP 解析，维护面更大。
- Q：ID token 签名验证用哪个库？
  A：引入 `PyJWT[crypto]`（MIT；后端 cryptography 为 Apache-2.0 或 BSD），只管 JWT/JWK 验签，OAuth 流程本身用 httpx。派 C2 的卡之前，master 先核实当前版本、许可证和传递依赖。
  理由：范围窄；Authlib 体量大，容易和 httpx 链路重复。
- Q：ChatGPT 订阅推理用哪个适配器？
  A：复用 B 的 Responses 适配器，由连接装配时传入订阅选项：OAuth token 认证、store=false、不发 max_output_tokens、系统指引转成 instructions、工具用允许的形式。
  理由：只维护一套 Responses 编解码。
- Q：D 里运行中快捷键切换权限时，待生效值由谁持有？
  A：App 持有待生效的设置意图，ConfigStore 新增按目标项比较保存工作区和全局设置的方法。运行中快捷权限切换只更新意图；等消费任务结束、检查点提交、App 忙状态释放后再保存，下一轮 `prepare_run()` 读到新值。
  理由：和 A2 连接切换归 App 一致。

## 契约与兼容

范围：交付项之间、模块之间的约定；会话格式、配置文件等旧数据的兼容。

待确认事项清单：
- [x] 会话 JSONL 格式升级方式（选择字段已由 A1 落地，续接字段的版本与兼容）
- [x] App / Runtime / TUI 之间连接切换的所有权约定（见实现方案板块：App 负责）

讨论记录（已确认项）：
- Q：会话 JSONL 怎么升级以容纳续接数据？
  A：新增 v6。v1～v5 继续只读可用，读入时续接为空；第一次业务写入时在锁内整文件升级到 v6。和 v4、v5 的升级方式一致。
  理由：不出现第二个事实来源；代价是旧版 Mica 读不了升级后的会话。

## 范围边界

范围：本阶段明确不做什么。

待确认事项清单：
- [x] 不做清单（旧稿：架构重构、Phase 7 命令、图片输入、自动路由与故障转移、文件监听、推理连接测试、内置工作模式）

讨论记录（已确认项）：
- Q：Phase 6 明确不做哪些？
  A：保留旧稿清单并新增三条：历史 token 估算和上下文用量（阶段 7）、命令插件框架、未实现命令的空壳入口。
  理由：不在 A2 提前引入一套可能在阶段 7 重做的估算。
