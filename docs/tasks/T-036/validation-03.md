# T-036 第 3 轮验证证据 · 2026-10-25

依据 resume-02-request.md；沿用 resume-01-request.md 的受限访问与 H/A3 分支。起点 3/8；本轮最多新增 5 次，失败也计数。实际值只留有界内存，记录仅为白名单结构事实。

基线 `2bcd25532ee8521fea507407c996a6a142781838`；初始工作区干净。

## 运行记录与发送前计数

- `{"live_requests": 0, "passed": true, "phase": "selftest", "real_credentials_read": false}`

- `{"live_requests": 0, "passed": true, "phase": "selftest", "real_credentials_read": false}`

- `{"auth_read": false, "base_equal": true, "channel_equal": true, "messages_unique": true, "model_equal": true, "phase": "metadata", "providers_object": true, "specified_chat_unique": true}`

- `{"count": 2, "environment_override": false, "frozen": true, "phase": "connections", "protected_read": true}`

- `{"consumed_total": 4, "new_requests": 1, "phase": "H-completions"}`

- `{"consumed_total": 5, "new_requests": 2, "phase": "H-completions"}`

- `{"closed": true, "frames": 3, "gzip": true, "http_status": 200, "phase": "H-completions-response"}`

- `{"closed": true, "frames": 11, "gzip": true, "http_status": 200, "phase": "H-completions-response"}`

- `{"normal_completion": true, "phase": "H-completions-result", "read_count": 1, "roundtrip_established": true}`

- `{"empty_text": false, "linked_result": true, "object_input": true, "phase": "H-outbound", "private_replay": false, "single_schema": true, "tool_calls": 1}`

- `{"consumed_total": 6, "new_requests": 3, "phase": "H-messages"}`

- `{"closed": true, "frames": 10, "gzip": true, "http_status": 200, "message_stop": true, "phase": "H-messages-response", "stop_reason": "end_turn", "tool_use_blocks": 0}`

- `{"normal_completion": true, "phase": "H-messages-result", "run_failed": false}`

- `{"phase": "H-branch-boundary", "remaining": 2, "total_summary": 6}`

- `{"consumed_total": 7, "new_requests": 4, "phase": "A3"}`

- `{"arguments_mentioned": false, "closed": true, "error_type": "unknown", "format_mentioned": false, "function_mentioned": false, "gzip": true, "http_status": 400, "input_mentioned": true, "paths": [], "phase": "A3-response", "required_fields": [], "required_mentioned": false, "tool_result_mentioned": false, "tool_use_mentioned": false}`

- `{"new_requests": 4, "phase": "live-ended", "total_summary": 7}`

## 执行方法、无凭据自检及前置核对

本轮只使用自身临时目录中的一次性 probe.py，默认无真实入口。实际执行顺序：

1. `uv run --no-sync --offline python <本轮临时目录>/probe.py` → exit 0，自检通过，无真实配置或凭据读取，无真实请求。
2. `uv run --no-sync --offline python <本轮临时目录>/probe.py --authorized-live-resume-02` → 同一最终探针版本；再次通过自检，在禁网状态下读取 models 元数据并复核，再经受保护入口读取必要 auth；冻结两条连接后执行 H。H 结束时保持进程、冻结连接和真实历史在内存，按照已授权的无本地缺陷分支输入 `a3`，连续完成 A3 首请求后 exit 0。

所有运行在产品导入前隔离 HOME/USERPROFILE/APPDATA/LOCALAPPDATA/XDG 配置根；只按变量名移除匹配 KEY/TOKEN/SECRET/PASSWORD/CREDENTIAL/AUTH 的环境项，不打印值。没有初始化 ContextBuilder、读取工作区 AGENTS、用户会话或真实 config；不修改默认模型或配置文件。

无凭据自检仅验证本轮一次性观察器和守卫，不重复第一轮成功矩阵：

- 普通/gzip × 200/400 共 4 个三字节分片响应。通过内层 HTTPX `aiter_bytes()` 解压后观察，移除交给外层 Response 的 Content-Encoding/Content-Length，防止二次解压；正常帧可解析且含 message_stop，错误体可安全提取，资源关闭。
- 合成错误中的 `messages.2.content.0.input` 归一为 `messages[].content[].input`；提取已知 input 必填字段；未知字段及假秘密均未进入安全摘要。
- 累计达到 8 时出站前拒绝；缺 message_stop 不算成功；临时目录产品 read 经真实 ToolExecutor 执行一次成功，非指定路径被拒绝。两个自检入口各执行一次自身文件读取，与真实实验 read 分开计数。
- 自检/元数据前检期间 socket connect/connect_ex 只允许 Windows asyncio 所需 IPv4 回环，DNS/create_connection 被禁用。只有冻结连接后真实 HTTP transport 才可建立；出站 Gate 核对完整 endpoint、方法、认证、版本、模型、预算、schema、正文结构、工具关系及允许的 choice 模式。

元数据前检只通过现有 ConfigStore.read('models') 读取，先要求 providers 为对象；Completions 精确限定在用户指定 provider 记录内选唯一启用模型，Messages 按唯一启用候选选择，两者基址、模型 ID、channel 均仅在内存比较相等。没有要求整个目录 Completions 全局唯一，也没有按顺序/默认值猜测。全部通过后，ConfigService 使用过滤到两条获准记录的内存目录及 ConfigStore 的受保护 auth 读取，环境覆盖关闭，冻结 ConnectionBinding。必要读取完整 auth 文件仅为产品受保护入口的文件边界，其余凭据记录不用于构造连接。

本轮初版最终自检即通过，没有新增失败测试、探针修正或产品补丁。A3 的 HTTP 400 是本轮唯一实际失败请求，发送前已消耗第 7 次累计计数；不是自检失败、观察器故障或预算守卫触发。

## 请求账本及真实结果

| 累计计数 | 本轮请求 | 观察 | 工具执行/后续 |
|---|---|---|---|
| 4 | H：Completions 首请求 | HTTP 200，gzip，解压后 3 个 SSE JSON 帧 | 唯一合法 read，经真实 Provider → Engine → ToolExecutor → 产品 read，指定临时文件读取成功一次 |
| 5 | H：Completions 结果续问 | HTTP 200，gzip，11 帧；RunCompleted | 出站含实际关联结果，正常完成，未再次执行工具 |
| 6 | H：Messages 历史请求 | HTTP 200，gzip，10 帧，end_turn/message_stop，RunCompleted | 保留本轮真实工具历史，零新 tool_use，未执行工具；本轮最小历史未遭 HTTP 400 拒绝 |
| 7 | A3：独立手写强制 read 首请求 | HTTP 400，gzip；错误白名单类型 unknown，paths=[]，required_fields=[]，只有 input_mentioned=true | 零 read；拒绝即结束该组，未发送结果续问，未换参数或重试 |

本轮新增 **4 次**，累计 **7/8**；失败已经计入，没有未知消耗。A3 首答被拒绝后按规则不用第二次请求；不为用完余额补请求，也不重启 live 入口。两组真实 Agent read 总计 **1 次**（H=1，A3=0），没有 write/edit/bash/ask、第二文件或其他路径的模型工具执行。

H 使用真正的 Completions Provider、Engine、ToolExecutor、产品 read；完成消息通过 Engine 的 completed_messages_sink 保留在内存，含实际调用和实际结果，并追加同一合成历史追问。Messages 出站在网络边界与独立编写的协议期望比较：单 read schema、assistant tool_use/input 对象、匹配 ID 的紧随 user/tool_result、随后用户正文、无空 text、无私有续接，全部相符。没有 fake 执行业务、伪造读取内容、由模型正文推断工具或剥离历史后重试。

可对齐项：同一轮冻结连接的基址/模型/channel、单一产品 read schema、合成首轮任务、1024 输出预算、OFF/disabled、零自动重试。协议必须不同的项：Completions 的 function 包装与工具角色/结果结构、Messages 的 content blocks、认证/版本头；Completions OFF 省略 reasoning_effort，Messages 显式 thinking disabled。H 还包含真实完成轮次及新追问，A3 是另一新会话，仅在独立手写基准首请求加指定 tool_choice；A3 结果续问恢复 auto 的分支因首答 400 没有执行。

这些真实结果不代表旧五工具请求、用户原会话、旧配置绑定或模型实际后端版本的严格复现；本轮只确认当前冻结目标、最小单 read 历史的行为。没有记录或断言最终正文是否复述随机文件值；H 成功的范围是实际读取、关联结果发送及正常终态。

## 观察器与预算的可审查片段（无凭据）

发送前的核心顺序如下；实际累计记录保存在上方，不因进程重启清零：

```python
# live 入口若已有 consumed_total 标记即拒绝重新初始化。
check(not re.search(r'"consumed_total": \d+', evidence.read_text("utf-8")))
# 在 endpoint/模型/schema/身份/历史等全部校验后，调用真正 transport 之前：
check(total < 8)
total += 1
with evidence.open("a", encoding="utf-8") as output:
    output.write(safe_count_record(total))
    output.flush()
    os.fsync(output.fileno())
# 请求建立 + 整个流消费共用同一个 deadline，最多 30 秒。
deadline = asyncio.get_running_loop().time() + 30
```

解压后观察位置及内存边界：

```python
async with asyncio.timeout_at(deadline):
    async for part in original_httpx_response.aiter_bytes():
        check(len(observed) + len(part) <= limit)
        observed.extend(part)
        # 成功流把同一解压后字节交给产品解析器，外层不再解压。
        yield part
# 正常观察 limit=524288；HTTP 错误体 limit=65536。
# finally 关闭原响应；Provider/client 在各自 finally/async with 中关闭。
```

协议字段路径提取只接受固定字段集合及合法路径语法：

```python
FIELDS = set("messages tools content input id name type tool_use_id function arguments role input_schema properties required tool_choice thinking max_tokens model stream".split())
def norm_path(value):
    if not isinstance(value, str) or not re.fullmatch(
        r"[a-z_]+(?:(?:\.[a-z_0-9]+)|(?:\[\d+\]))*", value
    ):
        return None
    tokens = re.findall(r"[a-z_]+|\d+", value)
    if any(not t.isdigit() and t not in FIELDS for t in tokens):
        return None
    return re.sub(r"\[\d+\]|\.\d+", "[]", value)
```

错误类型只接受固定枚举，未知类型记 unknown；固定关键词仅转布尔值，不输出原 message、未知值或原路径。A3 的 input_mentioned=true 只是词出现的事实，不能解释成 input 字段损坏、tool_choice 不支持或渠道无工具能力。未取得字段路径，不为获取更详细错误重复请求。

## 裁决及未执行项

- **H：本轮最小历史被接受。** 此前带五工具/旧历史的 400 未在本轮单 read 样本复现，没有确认本地编码缺陷；不能把这次成功解释成旧问题已修复。
- **A：仍未取得真实 Messages 工具往返。** 旧 auto 无调用与本轮 A3 的 400 是不同现象；A3 没有完成工具调用/结果往返，不能判通过，也不能据其拒绝断定目标完全不支持工具。
- 没有本地失败反例或产品修复，所以未进入补丁后追加一次 Messages 的分支。剩余 1 次不足以重新开启完整两步 A3，也没有授权重试；本轮停止，不延伸更多成功矩阵或无限后续实验。
- 无产品/正式测试/依赖变化，因此没有重跑 Messages/Engine/App 专项、全量 pytest、全仓 Ruff、构建或安装态。没有真实 TUI/用户会话恢复/取消/思考续接验收，不访问 Responses。
- 没有读取用户会话、其他客户端凭据或私有控制台，没有修改真实配置、默认值、权限、提示词、工具定义或协议契约；原始请求/响应/地址/模型/正文/ID/参数/文件值均未持久化或打印。

## 最终核对与清理

- 在删除前验证自身临时目录的解析绝对路径位于系统临时目录中，且内容只剩自身创建的 probe.py；删除该文件及随后空目录，没有递归清理或碰触他人产物。两次运行的隔离目录与用例文件均由 TemporaryDirectory 创建者关闭清理，真实入口已正常退出。
- 独立检查两份文档无尾随空白、本地 Markdown 链接均存在；从本文件运行记录提取的 consumed_total 严格为 4、5、6、7，计数连续且不超过 8。
- `git diff --check` exit 0；`git diff --name-only` 与 `git diff --cached --name-only` 均为空。最终 `git status --short` 仅列本轮 worker-03.md、validation-03.md 两份未跟踪文档，所有旧报告及受保护文件保持原样。
- 最终 HEAD 为 `2bcd25532ee8521fea507407c996a6a142781838`，与初始一致；无暂存、提交、推送、stash、还原或其他 Git 写操作。
