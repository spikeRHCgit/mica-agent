# 第 1 轮汇报 · 2026-10-11

依据：[`T-026.md`](T-026.md)。

- 结果：完成 —— 13 项调研已覆盖，事实附来源与类型，未核实项已明确标注。
- 改动：仅新增 `docs/tasks/T-026/worker-01.md`。
- 验证：`git diff --check` → 退出 0；新文件 no-index diff 已核对（新增差异退出 1，check 无空白诊断），报告尾随空白 0 行；78 个来源标签全部有定义，问题 1～13 齐全；`git status --short --untracked-files=all` → 仅本汇报文件。
- 偏离与疑点：无范围偏离；协议未核实项与官方说明分歧已在第 3、4、5、9、11、12、13 条标明。本轮未运行推理/目录接口或产品测试。
- 建议：无；候选方案交由 master 审查。

### 明细

- [x] 问题 1～4：见第 1～4 条，已区分请求必需字段、流式终态与回放要求。
- [x] 问题 5～8：见第 5～8 条，包含当前思考模式与模型/账号/前缀绑定规则。
- [x] 问题 9～12：见第 9～12 条，未知 Go 扩展协议已列缺口；仅静态对照指定文件。
- [x] 问题 13：见第 13 条，最少字段、未核实体量与两种候选及代价齐全，未选 wire。
- [x] 每条事实附官方来源链接及来源类型；78 个引用标签全部有定义，未核实项列出查阅位置。
- [x] 已通读新文件并检查 diff/格式；最终 Git 状态仅有指定汇报文件。

### 调研口径

- 查阅日期：2026-10-11；仅公开官方文档和官方 SDK 源码，无 Key、无业务端点调用、无安装；本地仅读任务指定材料及角色规则。
- 标记：**官方文档**＝文档明示；**SDK 类型定义**＝字段/枚举声明，不能证明服务端全部校验；**推断**＝依据所附来源推导，不能当实测结论。
- SDK 链接指向本次读取的 `main`，并非版本锁定保证；GitHub 公共 commits API 遇匿名限流，改读公开 raw 源码，没有使用凭据。
- 表中的字段清单针对本卡的文本、客户端函数工具；未扩展到内置搜索、计算机、多模态、程序化工具等完整协议。

### 1. Responses 请求

- **官方文档 / SDK 类型定义**：顶层 `instructions` 是 system/developer 指引；也可用 input 的 `system`/`developer` message；使用 `previous_response_id` 时旧 `instructions` 不自动继承。[O-create]、[O-easy]
- **SDK 类型定义**：简易 message 必需 `role, content`，role 为 user/assistant/system/developer，content 为字符串或内容数组；`type="message"` 可省，assistant 可有 `phase`。[O-easy]
- **SDK 类型定义**：完整 assistant 输出 message 回放形状必需 `id,type="message",role="assistant",status,content`；content 包含 `output_text`/`refusal`；不能把简易输入字段要求当完整输出字段要求。[O-message]
- **SDK 类型定义**：`function_call` 必需 `type,call_id,name,arguments`，arguments 是 JSON 字符串；`id,status` 可选；`id` 是输出项身份，`call_id` 才是工具结果的关联身份。[O-call]
- **SDK 类型定义 / 官方文档**：`function_call_output` 类型必需 `type,output`，当前 SDK 的 `call_id` 可选；普通客户端函数往返的官方示例仍显式发送对应 `call_id`，output 可为字符串或内容列表。[O-input]、[O-function]
- **SDK 类型定义**：reasoning input 必需 `type="reasoning",id,summary`，summary 元素必需 `type="summary_text",text`；`encrypted_content,content,status` 可选，content 元素为 `reasoning_text,text`。[O-reason-item]
- **SDK 类型定义**：Responses 工具是扁平 `{type:"function",name,description?,parameters,strict}`，没有 Chat Completions 的外层 `function`；parameters/strict 在 SDK 标 Required 且可为 null。[O-tool]
- **官方文档**：strict=true 要求每层对象 `additionalProperties:false` 且 properties 全列 required（可用 null 表示可选）；未发送 strict 时 Responses 尝试规范化，不兼容时退为 best-effort 并返回 strict:false；显式 strict:false 可退出严格模式。[O-function]
- **SDK 类型定义**：`reasoning.effort` 当前全集是 none/minimal/low/medium/high/xhigh/max；summary 为 auto/concise/detailed，旧 generate_summary 已废弃；具体模型不一定支持全集。[O-reason-config]
- **官方文档 / SDK 类型定义**：`max_output_tokens` 同时约束可见输出与内部 reasoning tokens；可能预算耗尽而没有正文，数字上限依模型，未核实统一上限。[O-create]、[O-reason-guide]
- **SDK 类型定义 / 官方文档**：`store` 控制服务端响应保存/后续取回，缺省 true；false/ZDR 的无状态续接自行回放历史；`include` 请求额外输出字段，密文相关值为 `reasoning.encrypted_content`。[O-create]、[O-state]
- **官方文档**：当前 stateless 响应默认含密文，`include:["reasoning.encrypted_content"]` 保留兼容但不再必需；这是当前文档变化，不能写成所有网关已同步支持。[O-reason-guide]
- **官方文档 / SDK 类型定义**：`parallel_tool_calls` 控制是否允许并行工具调用；false 将一次输出约束为至多一个函数调用；内置工具不支持并行调用，具体模型还有限制。[O-create]、[O-function]
- **SDK 类型定义 / 官方文档**：reasoning 另有 `context=auto/current_turn/all_turns`；较新模型支持跨轮推理保留，旧模型默认 current_turn，不能仅按“有 reasoning 能力”推导该参数可用。[O-reason-config]、[O-reason-guide]

### 2. Responses 流式事件与终态

| 来源类型 | 事件 | 本卡所需形状与用途 | 来源 |
|---|---|---|---|
| SDK 类型定义 | `response.output_text.delta` / `.done` | `item_id,output_index,content_index,sequence_number`；增量 `delta`，完成 `text`（另含 logprobs） | [O-text-delta]、[O-text-done] |
| SDK 类型定义 | `response.reasoning_summary_text.delta` / `.done` | `item_id,output_index,summary_index,sequence_number`；增量 `delta`，完成 `text` | [O-summary-delta]、[O-summary-done] |
| SDK 类型定义 | `response.reasoning_summary_part.added` / `.done` | `item_id,output_index,summary_index,sequence_number,part`；part 是 summary_text/text | [O-summary-part]、[O-events] |
| SDK 类型定义 | `response.reasoning_text.delta` / `.done` | `item_id,output_index,content_index,sequence_number`；增量 `delta`，完成 `text` | [O-think-delta]、[O-think-done] |
| SDK 类型定义 | `response.function_call_arguments.delta` / `.done` | `item_id,output_index,sequence_number`；增量 `delta`，完成 `arguments`；参数碎片不是完整 JSON | [O-args-delta]、[O-args-done] |
| SDK 类型定义 | `response.output_item.added` / `.done` | `output_index,sequence_number,item`；item 携带 type/id/call_id 等；item 完成不等于整响应成功 | [O-item-added]、[O-item-done] |
| SDK 类型定义 | `response.completed` | `type,sequence_number,response`；整响应完成，可含待执行 function_call | [O-completed] |
| SDK 类型定义 | `response.failed` | 同样是 `type,sequence_number,response`；检查 `response.error={code,message,...}` | [O-failed]、[O-response-error] |
| SDK 类型定义 | `response.incomplete` | 同样是 `type,sequence_number,response`；检查 `response.incomplete_details.reason` | [O-incomplete]、[O-response] |
| SDK 类型定义 | `error` | 顶层 `type="error",sequence_number,message,code?,param?`；不是 `{response:...}` 形状 | [O-error-event] |

- **SDK 类型定义**：`incomplete_details.reason` 当前为 max_output_tokens/max_messages/content_filter/steered；steered 对应 WebSocket steering 的安全边界停止，不能只保留旧的两个枚举。[O-response]、[O-incomplete]
- **推断**：failed/incomplete/error、缺少整响应终态的断流，都不足以证明正常完成；输出项 done 只能证明该项收口。[O-failed]、[O-incomplete]、[O-error-event]、[O-item-done]
- **推断 / 未核实**：SDK 存在 reasoning_text 事件，不代表每个模型或账号能取得内部思考正文；查过 reasoning guide 与事件定义，不能用密文/summary 冒充完整正文。[O-think-delta]、[O-reason-guide]

### 3. Responses 续接、phase、绑定与 ID

- **官方文档**：工具往返应回传上次工具调用伴随的 reasoning；连续工具调用时，保留最近 user 消息以来的 reasoning、function_call、function_call_output；裁剪时这一区间应原样保留。[O-reason-guide]、[O-function]
- **官方文档**：`store=false` 手工管理历史的通用做法是按原顺序保存并回放全部 output items，再追加工具结果/新用户消息；仅保存 assistant 正文不等价。[O-state]
- **SDK 类型定义**：回放 reasoning 至少保留 `id,type,summary` 及收到的完整 `encrypted_content`；不能只存密文再重建/清洗 summary；流中取 `response.output_item.done` 的密文，added 的值可能不完整。[O-reason-item]
- **官方文档 / SDK 类型定义**：密文请求值是 `reasoning.encrypted_content`；当前官方无状态默认返回，但其他 OpenAI 兼容渠道是否必须 include 未核实，查过官方 guide、create 参数与 item 定义。[O-reason-guide]、[O-create]、[O-reason-item]
- **官方文档**：assistant `phase` 为 commentary（中间更新）/final_answer（最终回答）；API 层可选，GPT-5.4/5.5 的长流程建议使用，手工回放须保留原值，丢失可导致把前言误当最终答案。[O-reason-guide]
- **SDK 类型定义 / 未核实**：SDK 注释点名 gpt-5.3-codex 及之后的模型需回传 phase；没有完整“哪些模型必定返回 phase”的穷尽表，查过 guide 与 easy/output message 定义；不要为 user 添加 phase。[O-easy]、[O-message]
- **官方文档**：persisted reasoning 仅同模型家族兼容，例如 GPT-5.6 sol/terra/luna 之间可复用；换到不同家族时服务端从模型上下文省略不兼容 reasoning，即使 context=all_turns。[O-reason-guide]
- **未核实（官方文档）**：另一账号/组织的有效 reasoning 密文会拒绝、丢弃还是兼容，官方入口未给出本轮可确认的行为/错误码；查过 reasoning、conversation-state、create 与 SDK item 定义。[O-reason-guide]、[O-state]、[O-create]、[O-reason-item]
- **官方文档 / 推断**：示例 response id 为 `resp_...`，message id 为 `msg_...`，reasoning id 为 `rs_...`，函数项 id 为 `fc_...`，工具关联 call_id 为 `call_...`；这是样例约定，不是已核实的全部允许格式。[O-state]、[O-reason-guide]、[O-function]
- **未核实（SDK 类型定义）**：上述 ID 的统一 maxLength、服务端必需前缀/字符集以及跨提供商是否接受未找到；类型只声明 str；不要把 fc_ 项 ID 当 call_id 或用 ID 前缀认定续接可用。[O-call]、[O-reason-item]、[O-message]

### 4. Responses 错误识别

- **推断（官方 SDK 实现）**：HTTP JSON 常见为 `{error:{message,type,param,code}}`；SDK 从 body.error（不存在则 body）提取，code/param/type 可为空；非 JSON 错误体也可能出现，不能硬假定所有失败同形。[O-client]、[O-exceptions]
- **官方文档 / SDK 类型定义**：401 对应认证失败；403 为权限/地区等拒绝；429 可是速率或额度不足，不应全部当短暂限流；流内 error 的 code 与 failed.response.error 分开解析。[O-errors]、[O-error-event]、[O-response-error]
- **官方文档 / 推断（SDK）**：连接/客户端超时对应 APITimeoutError；服务端超时类 5xx 与网络超时不同；不存在已核实的“所有超时都 HTTP 408”规则。[O-errors]、[O-exceptions]
- **SDK 类型定义**：truncation=disabled（默认）输入超上下文返回 400；auto 可丢掉开头 items；超输出预算则 incomplete.reason=max_output_tokens，两者不同。[O-create]、[O-response]
- **未核实（官方文档 / SDK 类型定义）**：Responses HTTP 上下文超限的稳定 error.code 全集未找到；查过 create、error codes、ResponseError；不把其他 API 的 context_length_exceeded 枚举直接当本接口保证。[O-create]、[O-errors]、[O-response-error]

### 5. Messages 请求

- **官方文档 / SDK 类型定义**：系统提示放顶层 `system`，可字符串或 text block 数组；常规历史是 user/assistant 的 `role,content`，相邻同角色会合并，而非一律拒绝不严格交替。[A-create]、[A-create-sdk]
- **SDK 类型定义 / 官方文档**：MessageParam 已含 system；专项文档允许在若干新模型追加中途 system 消息且无需 beta，Sonnet 5 不支持；create 的“没有 system role”旧说明与专项页不一致，不能当全模型禁令。[A-message-param]、[A-create]、[A-mid-system]

| 来源类型 | 块 | 必需字段；主要可选字段 | 来源 |
|---|---|---|---|
| SDK 类型定义 | text | `type="text",text`；cache_control/citations 可选 | [A-text] |
| SDK 类型定义 | tool_use | `type="tool_use",id,name,input`（对象）；caller/cache_control/toolset_name 可选 | [A-tool-use] |
| SDK 类型定义 | tool_result | `type="tool_result",tool_use_id`；content（字符串或内容块数组）/is_error 可选 | [A-tool-result] |
| SDK 类型定义 | thinking | `type="thinking",thinking,signature`，即使 thinking 为空也保留 signature | [A-thinking] |
| SDK 类型定义 | redacted_thinking | `type="redacted_thinking",data`；data 不透明且加密，原样回传 | [A-redacted] |

- **SDK 类型定义**：客户端工具顶层 tools 元素必需 `name,input_schema`（JSON Schema 对象）；description 可选，type 可省或 custom，另可有 strict；并非 Responses 的 parameters 字段。[A-tool]
- **SDK 类型定义 / 官方文档**：`model,messages,max_tokens` 必需；max_tokens 当前 minimum=0，0 用于缓存预热，生成预算上限依模型；thinking 与最终文本共同消耗输出预算。[A-create-sdk]、[A-create]、[A-think-guide]
- **SDK 类型定义**：manual 配置 `thinking:{type:"enabled",budget_tokens:N}`，一般 N≥1024 且 N<max_tokens；不能把该规则无条件套在所有模式。[A-enabled]
- **官方文档**：manual interleaved 模式预算跨同一 assistant turn，budget_tokens 可超过单次 max_tokens；SDK enabled 注释未表达这个例外，应以专项文档区分。[A-extended]
- **SDK 类型定义 / 官方文档**：adaptive 为 `thinking:{type:"adaptive"}`，用 `output_config.effort=low/medium/high/xhigh/max` 控制努力程度；无须 budget_tokens，具体值/可关闭行为依模型。[A-adaptive]、[A-effort]、[A-think-guide]
- **官方文档**：4.6 的 manual 已废弃；4.7/4.8 及更新若干模型拒绝 enabled/budget_tokens；部分模型默认 adaptive；Sonnet 5.5 新增 between_tools（high 及以下）而拒绝 disabled，不能设计全模型统一开关。[A-think-guide]
- **官方文档**：display=summarized/omitted 控制是否返回可见摘要；omitted 仍有空 thinking+signature；updates 为 beta，需 `thinking-display-updates-2026-08-18`，不可将其当稳定 SDK 全集。[A-think-guide]
- **官方文档**：直连 API 要认证（当前推荐 `Authorization:Bearer <key/token>`，仍支持 x-api-key）、`anthropic-version:2023-06-01`、`Content-Type:application/json`；多 workspace Key 还需 anthropic-workspace-id。[A-overview]
- **官方文档 / 冲突记录**：overview 写 anthropic-version 必需，create 页 header 标 optional；本卡只能确认显式发送版本头是官方示例/SDK通路，未核实直连缺省版本头会否被接受。[A-overview]、[A-create]

### 6. Messages 流式事件与 stop_reason

| 来源类型 | 事件 | 本卡所需形状 | 来源 |
|---|---|---|---|
| SDK 类型定义 / 官方文档 | message_start | `{type,message}`，message 起初 content=[]、stop_reason=null，含 id/model/usage 等 | [A-start]、[A-stream] |
| SDK 类型定义 | content_block_start | `{type,index,content_block}`，块可为 text/thinking/tool_use/redacted_thinking 等 | [A-block-start] |
| SDK 类型定义 | content_block_delta | `{type,index,delta}`，按 index 组装；delta 类型见下表 | [A-block-delta] |
| SDK 类型定义 | content_block_stop | `{type,index}`，只关闭内容块 | [A-block-stop] |
| SDK 类型定义 | message_delta | `{type,delta,usage}`，delta 含 stop_reason/stop_sequence 等，usage 是累计用量更新 | [A-message-delta]、[A-stream] |
| SDK 类型定义 | message_stop | `{type:"message_stop"}`，无完整 message 载荷 | [A-stop] |
| 官方文档 | ping | `{type:"ping"}`，保活，可在流中多次出现 | [A-stream] |
| 官方文档 | error | `{type:"error",error:{type,message}}`，例如 overloaded_error；HTTP 200 建流后仍可能错误 | [A-stream] |

| 来源类型 | delta.type | 字段 / 组装要点 | 来源 |
|---|---|---|---|
| SDK 类型定义 | text_delta | text；正文碎片 | [A-text-delta] |
| SDK 类型定义 | thinking_delta | thinking；思考显示碎片 | [A-thinking-delta] |
| SDK 类型定义 / 官方文档 | signature_delta | signature；独立累积，通常在 block_stop 前发出，不能按显示文本清洗 | [A-signature-delta]、[A-stream] |
| SDK 类型定义 / 官方文档 | input_json_delta | partial_json；累积后解析 tool_use.input，start 可能 input={} | [A-json-delta]、[A-stream] |

- **SDK 类型定义**：当前稳定 StopReason 全集为 end_turn/max_tokens/stop_sequence/tool_use/pause_turn/refusal/model_context_window_exceeded；非流式非 null，流式 start 为空，终态再给出。[A-stop-reasons]、[A-message]
- **官方文档 / 推断**：end_turn 为自然完成，stop_sequence 是用户指定停止；tool_use 是正常工具交接；pause_turn 需原样续接服务端长工具流程，三者都不等价“用户任务最终成功”。[A-stop-guide]
- **官方文档 / 推断**：max_tokens/model_context_window_exceeded 为预算或上下文截断；refusal 是拒绝状态，虽是协议终态也不应冒充成功回答；应结合 message_stop 与 stop_reason 收口。[A-stop-guide]、[A-stop]
- **推断**：自然断流、只有 block_stop、或 error 后缺少正常 message_stop，不足以生成成功 Finish；累积碎片保留与成功完成需要分开。[A-stream]、[A-block-stop]、[A-stop]

### 7. Messages 续接、签名绑定、旧轮次与交错思考

- **官方文档 / SDK 类型定义**：同一工具使用 turn，完整保留 assistant 内容块与顺序，原样回传 thinking(thinking+signature)、redacted_thinking(data) 和关联 tool_use；删除/修改最新 assistant 思考块可报 400 invalid_request_error。[A-workflow]、[A-thinking]、[A-redacted]
- **官方文档**：工具结果虽放 user role，整个工具循环仍属同一 assistant turn；思考配置保持同轮一致，配置更改放到 turn 边界，不能把每次工具回包当新 turn。[A-workflow]
- **官方文档**：当前 signature 承载加密完整推理，可见 thinking 可能只是摘要或空串；修改/不能解密的签名始终 400，不受 prefix_mismatch_behavior 的 drop_block 选项救济。[A-think-guide]、[A-preserved]
- **官方文档**：新的 preserved thinking 先检查模型可读性；不兼容模型的块会被丢弃，请求可成功；例如 Fable 5.1→Opus 5.5 不继承，反向在 Claude API 可继承；不是“跨模型一律拒绝/一律兼容”。[A-preserved]
- **官方文档**：Sonnet 5.5/Haiku 5.5 的块另绑定产生账号或关联账号；其他账号回传会丢弃块而请求成功，较早模型不受此账号限制；网关是否复刻全部行为未核实。[A-preserved]
- **官方文档**：Fable 5.1、Opus 5.5、Sonnet 5.5、Haiku 5.5 检查前缀 system/tools/此前 messages；2026-08-31 00:00 UTC 起新账号默认执行前缀校验，旧账号由显式配置启用。[A-preserved]
- **官方文档**：前缀失配默认 error→400；可在 beta 配置 `thinking.block_binding.prefix_mismatch_behavior:"drop_block"` 选择丢弃失效块及后续块；需 `thinking-binding-controls-2026-08-01`，不是本卡替 master 决定的失败降级。[A-preserved]
- **官方文档**：同 beta 的 input_transformations 可报告 prefix_binding_mismatch/model_binding_mismatch/organization_binding_mismatch；无 beta 时模型/账号失配可静默丢弃；恢复成功不能证明内部思考继承。[A-preserved]
- **官方文档**：非工具使用的更早 turn 可以省略思考，推荐完整回传；但新的绑定链保留块须连续，可从头/尾/全部移除，不能挖中间洞，移除后再放回也会使期间产生的后续块失效。[A-think-guide]、[A-preserved]
- **官方文档**：Opus 4.5 与编号 4.6+ 模型保留此前思考并计入输入；Sonnet 4.5/Haiku 4.5 与更早模型会去除旧轮思考；合法保存不等于每个模型都使用所有旧思考。[A-extended]
- **官方文档**：adaptive 支持的模型自动 interleaved，无 beta；manual 的 Opus 4.5/Sonnet 4.5/较早 Claude 4 需 interleaved-thinking-2025-05-14；Sonnet 4.6 manual 仍支持但废弃，Opus 4.6 manual 不支持，Haiku 4.5 不支持。[A-extended]
- **官方文档**：interleaved 仅用于 Messages 工具流程，使思考出现在工具之间；不启用它仍可连续调用工具，不能把“能多次调用工具”当支持 interleaved 的证据。[A-workflow]、[A-extended]

### 8. Messages 工具 ID 与结果

- **官方文档**：输入 tool_use.id 与 tool_result.tool_use_id 的 schema pattern 是 `^[a-zA-Z0-9_-]+$`；样例通常 toolu_...，但客户端工具输入未要求 toolu_ 前缀；长度最大值未核实。[A-create]
- **SDK 类型定义**：tool_use.id 是 str，tool_result.tool_use_id 必需；Python 类型没有单独 regex 校验，不能据此说服务端任意字符串都接受。[A-tool-use]、[A-tool-result]
- **官方文档**：tool_use 放 assistant；下一条 user 内容数组先放每个对应 tool_result，后放普通文本；不能插入不相关消息，漏结果/前置正文会 400。[A-handle]、[A-tool-troubleshoot]
- **官方文档 / SDK 类型定义**：执行失败用对应 `tool_result` 的 `is_error:true`，content 说明真实错误；关联 ID 仍须匹配原 tool_use，不用伪造成功结果补洞。[A-handle]、[A-tool-result]
- **官方文档**：服务端工具的 srvtoolu_... 与客户端 tool_use 是不同路径；本卡研究客户端工具，不因见到 srvtoolu_ 就向它发送客户端 tool_result。[A-server]

### 9. Chat Completions 三个渠道的思考与续接

- **官方文档**：OpenRouter 可见思考放 `choices[].delta.reasoning`，非流式放 message.reasoning；reasoning_content 可作回传 reasoning 的别名；结构化续接放 delta/message.reasoning_details。[R-reason]
- **官方文档**：OpenRouter details 有 reasoning.summary(summary)、reasoning.encrypted(data)、reasoning.text(text,signature?)，并携带 id/format/index；片段按返回顺序累积，密文流中可能标为 `[REDACTED]`。[R-reason]
- **官方文档**：OpenRouter 工具往返要求保留整段连续 reasoning_details，内容和顺序不可修改；只有原始字符串模型可回传较简单的 assistant.reasoning，summary/密文模型应回传完整 details。[R-reason]
- **官方文档 / 未核实**：OpenRouter 明说保留可维持工具暂停后的思考连续性，但未给所有模型统一的“缺字段→固定错误码”；查过 Preserving Reasoning 与 details 章节，不把 Claude 上游 400 保证推广到整个路由器。[R-reason]、[A-workflow]
- **官方文档**：DeepSeek 流式使用 delta.reasoning_content，与 content 同层；当前文档规定请求带 tools 时应回传**所有之前轮次**的 reasoning_content，并加入上下文，要求不限于最近一次工具调用。[D-think]
- **官方文档**：DeepSeek 请求不带 tools 时，旧 reasoning_content 无须回传，发送了也忽略；thinking 的工具支持从 V3.2 开始，不能据更早纯推理模型的用法下结论。[D-think]、[D-tools]
- **未核实（官方文档）**：本次读取的 DeepSeek 当前 Thinking Mode/Tool Calls 页没有确认“不回传必定 400”的稳定错误规则；未做 Key 调用，不能报告已复现拒绝。[D-think]、[D-tools]
- **未核实（官方文档）**：OpenCode Go 的 Completions 各模型究竟返回 reasoning_content/reasoning/reasoning_details，以及工具回传字段/省略后果，Go/Zen/Providers 文档未给完整协议；原厂规则不足以证明网关行为。[G-go]、[G-zen]、[G-providers]
- **推断**：Go 的 endpoint 表只证明 API 分配，不证明该接口内所有扩展字段统一；在取得 Go 官方模型级依据前，应把“支持 Chat Completions”与“支持某种思考续接”分开登记。[G-go]

### 10. 与现有 openai_compatible.py 的差异（只列差异）

| 来源类型 | 已核实的官方要求 | 本地静态差异 | 证据 |
|---|---|---|---|
| 推断：本地静态对照 | OpenRouter 流 reasoning + reasoning_details | `_parse_chat_chunk` 仅取 reasoning_content，未解析 reasoning/details；OpenRouter 原生思考与结构化签名/密文信息丢失 | [R-reason]；[本地解析](C:/Users/10601/Desktop/learning/mica_agent/src/mica_agent/providers/openai_compatible.py:271) |
| 推断：本地静态对照 | OpenRouter 工具续接回传 assistant.reasoning/details | `_build_payload` 只编码 role/content/tool_calls/tool_call_id，没有思考回传；展示了别名 reasoning_content 也不等价保存 details | [R-reason]；[本地编码](C:/Users/10601/Desktop/learning/mica_agent/src/mica_agent/providers/openai_compatible.py:155) |
| 推断：本地静态对照 | DeepSeek 带 tools 时回传旧 reasoning_content | 流式 reasoning_content 已识别，但只 yield ProviderReasoningDelta；Finish 只携带 tool_calls，payload 不发送 reasoning_content，因此展示通路具备、续接通路缺失 | [D-think]；[本地事件](C:/Users/10601/Desktop/learning/mica_agent/src/mica_agent/providers/openai_compatible.py:450)；[本地 Finish](C:/Users/10601/Desktop/learning/mica_agent/src/mica_agent/providers/openai_compatible.py:387) |
| 未核实 / 推断 | Go 模型级思考续接要求未知 | 只能确认现编码/解析没有 reasoning/details；因第 9 条官方规则未核实，不能宣称缺失了某个 Go 强制字段 | [G-go]；[本地解析](C:/Users/10601/Desktop/learning/mica_agent/src/mica_agent/providers/openai_compatible.py:271) |
| 推断：本地静态对照 | Go 自定义 User-Agent 与稳定 x-opencode-session | stream 显式 headers 仅 Authorization 和 Content-Type，没有这两项装配；本文件内未找到会话头传入通路 | [G-go]；[本地头](C:/Users/10601/Desktop/learning/mica_agent/src/mica_agent/providers/openai_compatible.py:403) |

- **推断**：本卡没有重新调研其他承载模块；第 5～7 条既有 Provider/Message/JSONL 缺口以 T-023 指定汇报为背景，当前文件差异只来自本次读取。[本地编码](C:/Users/10601/Desktop/learning/mica_agent/src/mica_agent/providers/openai_compatible.py:155)；[T-023](../T-023.md#5-provider-协议与-chat-completions-映射)

### 11. OpenCode Go 接口分配与请求头

下表为本次 Go 官方 endpoint 表的模型 ID 快照；每行 **官方文档**，共同来源 [G-go]，端点前缀均为 `https://opencode.ai/zen/go/v1`。

| 模型 ID（按官方表） | API / 后缀 | 官方列出的 SDK |
|---|---|---|
| grok-4.7、grok-4.6、gpt-6-luna、gpt-5.6-luna、muse-spark-1.3-contributor、muse-spark-1.2-contributor | Responses / `/responses` | @ai-sdk/openai |
| claude-haiku-5-5、minimax-m3、minimax-m2.7、qwen3.8-max、qwen3.8-flash、qwen3.7-plus | Messages / `/messages` | @ai-sdk/anthropic |
| glm-5.3-flash、glm-5.3、glm-5.2、kimi-k3、kimi-k2.7-code、kimi-k2.6 | Chat Completions / `/chat/completions` | @ai-sdk/openai-compatible |
| longcat-2.0、longcat-2.5-preview-free、step-5-preview-free、mimo-v2.6-flash、mimo-v2.6-pro、mimo-v2.5、mimo-v2.5-pro | Chat Completions / `/chat/completions` | @ai-sdk/openai-compatible |
| deepseek-v4.1-flash、deepseek-v4-pro、deepseek-v4-flash、deepseek-v4-flash-vision-exp、hy4-preview、hy3、space-bunny | Chat Completions / `/chat/completions` | @ai-sdk/openai-compatible |

- **官方文档**：Go Models 小节公布 `https://opencode.ai/zen/go/v1/models`，用于模型清单及 metadata；本轮只读说明，未调用目录端点。[G-go]
- **未核实（官方文档）**：目录响应是否含可机读的模型→协议/SDK映射，官方 Go 页没有给字段 schema/样例；目前可确认的是 endpoint 表，而非 `models.api` 等自创字段。[G-go]
- **官方文档**：Go 希望自有 User-Agent（如 my-coding-agent/1.0）与每个对话稳定的 x-opencode-session；部分已验证客户端的原生 session header 也被识别，不需要冒用这些客户端身份。[G-go]
- **推断（官方 SDK 实现）**：Go 指定的 openai/openai-compatible SDK 默认用 Authorization:Bearer <apiKey>；anthropic SDK 默认用 x-api-key 加 anthropic-version:2023-06-01，也可显式用 Bearer authToken。[V-openai]、[V-compatible]、[V-anthropic]、[G-go]
- **未核实 / 推断**：上述是 Go 推荐 SDK 的默认头，不是 Go 对所有 auth 变体的接受保证；Go/Zen 文档未给三端点完整认证头矩阵；JSON Content-Type 应按各接口发送，是否需其他 Go 专用头未核实。[G-go]、[G-zen]、[A-overview]
- **推断**：候选①手工维护已核实的模型/API表，代价是目录变更需复核；候选②目录发现+明确人工 api 声明，代价是未知模型仍需用户配置，不能按名称/家族猜协议。[G-go]

### 12. 跨接口工具 ID 限制与拒绝条件

| 来源类型 | 接口 | 已核实约束 / 未核实边界 | 来源 |
|---|---|---|---|
| SDK 类型定义 / 未核实 | OpenAI Chat Completions | function tool_call.id 与 tool message.tool_call_id 是必需 str；SDK 没给统一 ID regex/maxLength；call_... 为常见样例，不是前缀校验保证 | [O-chat-call]、[O-chat-result]、[O-function] |
| SDK 类型定义 / 官方文档 | Responses | 区分 item.id 与工具 call_id，function_call_output 匹配工具 call_id；不能把 message/reasoning/function 项 ID 互换 | [O-call]、[O-input]、[O-function] |
| 官方文档 / 未核实 | Messages | 客户端 tool_use.id/tool_use_id 均符合 `^[a-zA-Z0-9_-]+$`；toolu_ 非输入强制前缀；统一最大长度未核实 | [A-create] |
| 未核实（官方文档） | OpenRouter/DeepSeek/Go Completions | 未找到三个网关共同的额外工具 ID 格式限制；不把官方 OpenAI 类型当网关服务端验证全集 | [R-reason]、[D-tools]、[G-go] |

- **推断（schema）**：来源接口的 ID 含 `.`、`:`、空格、斜杠或 Unicode 时直接进入 Messages 不符合 pattern，应被 schema 拒绝；call_abc/toolu_abc 则均匹配字符集，不能声称前缀不同必然拒绝。[A-create]
- **官方文档 / 推断**：Messages 即使字符合法，缺对应结果、关联错 ID 或没有紧接 assistant 的 user tool_result 仍会失败；跨接口只改 ID 而不维护配对和顺序不足以合法续接。[A-handle]、[A-tool-troubleshoot]
- **推断 / 未核实**：Responses 用 fc_ 项 ID 充当 call_id 会找不到原调用关联；服务端精确 HTTP/code 文案未核实，来源仅字段语义与工具回传示例，未实测。[O-call]、[O-input]、[O-function]
- **推断**：一种可选策略是请求内一一映射 ID 并同步调用/结果，保持保存的原身份；另一种是将不合法组合转为普通历史事实，代价是目标接口失去原生工具续接；均不能生成目标签名来补兼容。[A-create]、[O-call]、[A-thinking]

### 13. 至少保存的字段、体量与粒度候选

以下是“合法构造回放请求所需信息”的候选清单，不是 wire 设计；通用正文/工具事实可由已有对象保存，但必须能还原同一模型步骤的顺序和关联。

| 来源类型 | 接口/渠道 | 至少需要的信息（按本卡文本/函数范围） | 依据 |
|---|---|---|---|
| SDK 类型定义 / 官方文档 | Responses reasoning | 原顺序；type/id/summary(type,text)；收到的完整 encrypted_content；有 content/status 则原样保留，不用摘要替代密文 | [O-reason-item]、[O-state] |
| SDK 类型定义 / 官方文档 | Responses assistant/函数 | message 的 type/id/role/status/content 与原 phase；function_call 的 type/id(若有)/call_id/name/原 arguments；结果的 type/call_id/output | [O-message]、[O-call]、[O-input]、[O-function] |
| SDK 类型定义 / 官方文档 | Messages | assistant 块顺序；thinking 的 type/thinking/signature；redacted 的 type/data；text 的 type/text；tool_use 的 type/id/name/input；user tool_result 的 type/tool_use_id/content/is_error(若有) | [A-thinking]、[A-redacted]、[A-tool-use]、[A-tool-result]、[A-workflow] |
| 官方文档 / 推断 | Messages 绑定上下文 | 仅存思考块还不充分：须能保持原 system/tools/此前 messages 及连续思考链；来源模型与账号兼容信息用于回放判断，账号绑定不能从 signature 外观推导 | [A-preserved] |
| 官方文档 | OpenRouter Completions | 纯字符串模型保存 assistant.reasoning；details 模型保存有序 type/id/format/index 与对应 summary/data/text/signature，连同通用工具调用和结果 | [R-reason] |
| 官方文档 | DeepSeek Completions | assistant.reasoning_content 原字符串与通用正文/工具调用/结果；带 tools 的当前规则需覆盖此前所有轮次，不能仅存活跃工具轮 | [D-think] |
| 未核实（官方文档） | Go Completions | 专属最少字段清单未核实；只可确认按具体模型 API 选适配器，不能写出 Go 全模型共用 reasoning 白名单 | [G-go] |

- **SDK 类型定义 / 推断**：上表是必需信息基线；实际收到 caller/toolset_name/citations 等语义字段时不能假定删除无影响；完整回放需涵盖收到的受支持字段，否则应判该步骤不可原样回放。[A-tool-use]、[A-text]、[A-preserved]、[O-message]

| 来源类型 | 字段类别 | 长度/体量结论（没有给出产品上限） | 依据 |
|---|---|---|---|
| SDK 类型定义 / 未核实 | Responses 各 id/call_id | str；SDK 无统一长度上限；官方示例为几十字符量级，示例长度不是契约，最大值未核实 | [O-call]、[O-reason-item]、[O-function] |
| 官方文档 / 未核实 | Messages tool_use_id | 非空 ASCII 字母/数字/下划线/连字符；最大字符数未核实，不套用工具 name 的 128 字符上限 | [A-create] |
| SDK 类型定义 / 推断 | type/role/status/phase 等枚举 | 枚举标签本身是几十字节以内的量级；字段总数/项数仍需单独界定，没有“整条续接固定大小”的依据 | [O-message]、[O-call]、[A-thinking] |
| SDK 类型定义 / 未核实 | reasoning summary/content、Message text、函数 arguments/output/input、DeepSeek reasoning_content | 可变字符串/数组/JSON；没有统一字段 byte 上限；token 预算不能精确换算成固定 UTF-8 字节上限 | [O-reason-item]、[O-call]、[A-tool-use]、[D-think] |
| SDK 类型定义 / 未核实 | Responses encrypted_content | 不透明字符串，具体长度、编码倍率与最大体量未核实；不能按示例前缀或摘要长短估算上限 | [O-reason-item] |
| 官方文档 / 推断 / 未核实 | Messages signature/redacted.data | signature 承载加密完整 reasoning，不能假定为固定几十字节哈希；体量可能随推理增长，准确倍率/最大字符数未核实 | [A-think-guide]、[A-redacted] |
| 官方文档 / 未核实 | OpenRouter details 的 id/format/index 与 summary/data/text/signature | 类型与可选字段已公开，没有统一字符串长度/数组总数上限；体量取决于上游和完整连续块，最大值未核实 | [R-reason] |
| 官方文档 / SDK 类型定义 | 预算量级 | manual thinking 常规最小 1024 tokens，示例用 16000 输出预算；Responses max_output_tokens 同含推理；这是 token 量级证据，不能直接转为密文字节限额 | [A-enabled]、[A-workflow]、[O-create] |

**候选一：按模型步骤保留完整、有序、已支持类型的输出项/assistant 内容块。**

- **推断**：活跃上下文保留原项、步骤、配对关系、来源供应商/模型/API及原请求前缀关联；从完成事件取可回放值，天然覆盖纯思考/无正文步骤。[O-item-done]、[O-reason-item]、[A-workflow]、[A-preserved]
- **推断**：代价是原内容体量较大、敏感判定和前缀一致性更复杂；用于持久化仍须有字段/类型/总量白名单，不能保存 SDK 对象、任意整响应或任意字典。[O-reason-item]、[A-preserved]；[本项目 B 边界](../../phase6_plan.md#b--responses-与-messages-接口)

**候选二：按模型步骤只保留明确白名单字段，和通用历史关联重建请求。**

- **推断**：从上述字段清单提取显式类型记录，单独保存原 summary/signature/data/encrypted_content/arguments 与顺序；优点是易校验、定上限和审查，代价是未知类型/新字段不能自动回放。[O-reason-item]、[O-call]、[A-thinking]、[R-reason]
- **推断**：重建通用正文/参数/结果一旦改变 Messages 原前缀，就可能使思考失效；需保存可证明未变的关联信息，或明确标不可回放，不能只保留“签名字段齐全”就承诺合法续接。[A-preserved]
- **推断**：两种候选都需 master 确认字段总量上限、来源/账号/前缀绑定、未知值处理和不可回放降级；本轮不选择方案、不规定 v6 wire。[O-state]、[A-preserved]；[本项目 B 边界](../../phase6_plan.md#b--responses-与-messages-接口)

### 来源链接索引

每个短标签都是可点击的 Markdown 引用链接；来源类型在对应事实/表格行标明。SDK 类型声明与官方实现已分开标记。

[O-create]: https://github.com/openai/openai-python/blob/main/src/openai/types/responses/response_create_params.py
[A-mid-system]: https://platform.claude.com/docs/en/build-with-claude/mid-conversation-system-messages
[A-server]: https://platform.claude.com/docs/en/agents-and-tools/tool-use/server-tools
[O-easy]: https://github.com/openai/openai-python/blob/main/src/openai/types/responses/easy_input_message_param.py
[O-message]: https://github.com/openai/openai-python/blob/main/src/openai/types/responses/response_output_message_param.py
[O-call]: https://github.com/openai/openai-python/blob/main/src/openai/types/responses/response_function_tool_call_param.py
[O-input]: https://github.com/openai/openai-python/blob/main/src/openai/types/responses/response_input_item_param.py
[O-reason-item]: https://github.com/openai/openai-python/blob/main/src/openai/types/responses/response_reasoning_item_param.py
[O-tool]: https://github.com/openai/openai-python/blob/main/src/openai/types/responses/function_tool_param.py
[O-reason-config]: https://github.com/openai/openai-python/blob/main/src/openai/types/shared/reasoning.py
[O-reason-guide]: https://developers.openai.com/api/docs/guides/reasoning
[O-state]: https://developers.openai.com/api/docs/guides/conversation-state
[O-function]: https://developers.openai.com/api/docs/guides/function-calling
[O-errors]: https://developers.openai.com/api/docs/guides/error-codes
[O-client]: https://github.com/openai/openai-python/blob/main/src/openai/_client.py
[O-exceptions]: https://github.com/openai/openai-python/blob/main/src/openai/_exceptions.py
[O-chat-call]: https://github.com/openai/openai-python/blob/main/src/openai/types/chat/chat_completion_message_function_tool_call_param.py
[O-chat-result]: https://github.com/openai/openai-python/blob/main/src/openai/types/chat/chat_completion_tool_message_param.py
[A-create]: https://platform.claude.com/docs/en/api/messages/create
[A-think-guide]: https://platform.claude.com/docs/en/build-with-claude/thinking
[A-workflow]: https://platform.claude.com/docs/en/build-with-claude/thinking-tool-workflows
[A-preserved]: https://platform.claude.com/docs/en/build-with-claude/preserved-thinking
[A-extended]: https://platform.claude.com/docs/en/build-with-claude/extended-thinking
[A-overview]: https://platform.claude.com/docs/en/api/overview
[A-stream]: https://platform.claude.com/docs/en/build-with-claude/streaming
[A-stop-guide]: https://platform.claude.com/docs/en/build-with-claude/handling-stop-reasons
[A-handle]: https://platform.claude.com/docs/en/agents-and-tools/tool-use/handle-tool-calls
[A-tool-troubleshoot]: https://platform.claude.com/docs/en/agents-and-tools/tool-use/troubleshooting-tool-use
[R-reason]: https://openrouter.ai/docs/guides/best-practices/reasoning-tokens
[D-think]: https://api-docs.deepseek.com/guides/thinking_mode/
[D-tools]: https://api-docs.deepseek.com/guides/tool_calls/
[G-go]: https://opencode.ai/docs/go/
[G-zen]: https://opencode.ai/docs/zen/
[G-providers]: https://docs.opencode.ai/docs/providers/
[V-openai]: https://github.com/vercel/ai/blob/main/packages/openai/src/openai-provider.ts
[V-compatible]: https://github.com/vercel/ai/blob/main/packages/openai-compatible/src/openai-compatible-provider.ts
[V-anthropic]: https://github.com/vercel/ai/blob/main/packages/anthropic/src/anthropic-provider.ts
[O-text-delta]: https://github.com/openai/openai-python/blob/main/src/openai/types/responses/response_text_delta_event.py
[O-text-done]: https://github.com/openai/openai-python/blob/main/src/openai/types/responses/response_text_done_event.py
[O-summary-delta]: https://github.com/openai/openai-python/blob/main/src/openai/types/responses/response_reasoning_summary_text_delta_event.py
[O-summary-done]: https://github.com/openai/openai-python/blob/main/src/openai/types/responses/response_reasoning_summary_text_done_event.py
[O-summary-part]: https://github.com/openai/openai-python/blob/main/src/openai/types/responses/response_reasoning_summary_part_added_event.py
[O-events]: https://github.com/openai/openai-python/blob/main/src/openai/types/responses/response_stream_event.py
[O-think-delta]: https://github.com/openai/openai-python/blob/main/src/openai/types/responses/response_reasoning_text_delta_event.py
[O-think-done]: https://github.com/openai/openai-python/blob/main/src/openai/types/responses/response_reasoning_text_done_event.py
[O-args-delta]: https://github.com/openai/openai-python/blob/main/src/openai/types/responses/response_function_call_arguments_delta_event.py
[O-args-done]: https://github.com/openai/openai-python/blob/main/src/openai/types/responses/response_function_call_arguments_done_event.py
[O-item-added]: https://github.com/openai/openai-python/blob/main/src/openai/types/responses/response_output_item_added_event.py
[O-item-done]: https://github.com/openai/openai-python/blob/main/src/openai/types/responses/response_output_item_done_event.py
[O-completed]: https://github.com/openai/openai-python/blob/main/src/openai/types/responses/response_completed_event.py
[O-failed]: https://github.com/openai/openai-python/blob/main/src/openai/types/responses/response_failed_event.py
[O-response-error]: https://github.com/openai/openai-python/blob/main/src/openai/types/responses/response_error.py
[O-incomplete]: https://github.com/openai/openai-python/blob/main/src/openai/types/responses/response_incomplete_event.py
[O-response]: https://github.com/openai/openai-python/blob/main/src/openai/types/responses/response.py
[O-error-event]: https://github.com/openai/openai-python/blob/main/src/openai/types/responses/response_error_event.py
[A-create-sdk]: https://github.com/anthropics/anthropic-sdk-python/blob/main/src/anthropic/types/message_create_params.py
[A-message-param]: https://github.com/anthropics/anthropic-sdk-python/blob/main/src/anthropic/types/message_param.py
[A-text]: https://github.com/anthropics/anthropic-sdk-python/blob/main/src/anthropic/types/text_block_param.py
[A-tool-use]: https://github.com/anthropics/anthropic-sdk-python/blob/main/src/anthropic/types/tool_use_block_param.py
[A-tool-result]: https://github.com/anthropics/anthropic-sdk-python/blob/main/src/anthropic/types/tool_result_block_param.py
[A-thinking]: https://github.com/anthropics/anthropic-sdk-python/blob/main/src/anthropic/types/thinking_block_param.py
[A-redacted]: https://github.com/anthropics/anthropic-sdk-python/blob/main/src/anthropic/types/redacted_thinking_block_param.py
[A-tool]: https://github.com/anthropics/anthropic-sdk-python/blob/main/src/anthropic/types/tool_param.py
[A-enabled]: https://github.com/anthropics/anthropic-sdk-python/blob/main/src/anthropic/types/thinking_config_enabled_param.py
[A-adaptive]: https://github.com/anthropics/anthropic-sdk-python/blob/main/src/anthropic/types/thinking_config_adaptive_param.py
[A-effort]: https://github.com/anthropics/anthropic-sdk-python/blob/main/src/anthropic/types/output_config_param.py
[A-start]: https://github.com/anthropics/anthropic-sdk-python/blob/main/src/anthropic/types/raw_message_start_event.py
[A-block-start]: https://github.com/anthropics/anthropic-sdk-python/blob/main/src/anthropic/types/raw_content_block_start_event.py
[A-block-delta]: https://github.com/anthropics/anthropic-sdk-python/blob/main/src/anthropic/types/raw_content_block_delta_event.py
[A-block-stop]: https://github.com/anthropics/anthropic-sdk-python/blob/main/src/anthropic/types/raw_content_block_stop_event.py
[A-message-delta]: https://github.com/anthropics/anthropic-sdk-python/blob/main/src/anthropic/types/raw_message_delta_event.py
[A-stop]: https://github.com/anthropics/anthropic-sdk-python/blob/main/src/anthropic/types/raw_message_stop_event.py
[A-text-delta]: https://github.com/anthropics/anthropic-sdk-python/blob/main/src/anthropic/types/text_delta.py
[A-thinking-delta]: https://github.com/anthropics/anthropic-sdk-python/blob/main/src/anthropic/types/thinking_delta.py
[A-signature-delta]: https://github.com/anthropics/anthropic-sdk-python/blob/main/src/anthropic/types/signature_delta.py
[A-json-delta]: https://github.com/anthropics/anthropic-sdk-python/blob/main/src/anthropic/types/input_json_delta.py
[A-stop-reasons]: https://github.com/anthropics/anthropic-sdk-python/blob/main/src/anthropic/types/stop_reason.py
[A-message]: https://github.com/anthropics/anthropic-sdk-python/blob/main/src/anthropic/types/message.py
