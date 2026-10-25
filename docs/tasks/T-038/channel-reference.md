# T-038 渠道目录依据 · 2026-10-25

master 派工前公开只读核对。只访问官方文档及文档直接公布的公开 Go 模型目录，不读取本机配置/Key、不上传项目数据、不发送推理请求。以下是接口和结构依据，不承诺实时模型、价格、额度或账号资格。

## DeepSeek

- 官方入口：https://api-docs.deepseek.com/ 。正文给出 OpenAI 兼容基址 `https://api.deepseek.com` 和 `Authorization: Bearer <key>`；也单列 Anthropic 兼容地址，但本卡不扩展推理协议。
- 目录规范：https://api-docs.deepseek.com/api/list-models 。操作为 `GET /models`；结合官方基址使用 `https://api.deepseek.com/models`。
- 响应为 `{object: "list", data: [...]}`，条目 id/object/owned_by 必需；name、context_window、max_output_tokens、input_modalities、effort.supported_levels/default_level 等为可选元数据。只映射明确且能对应现有配置的字段；不靠 ID 猜能力。
- 该规范页本身未重复完整基址和认证，以上分别由官方首页补齐，不把目录页缺说明写成无需认证。

## OpenRouter

- 旧参考路径本次返回 HTTP 404。通过官方索引 https://openrouter.ai/docs/llms.txt 找到现行规范，不重复失败旧路径。
- 现行目录规范：https://openrouter.ai/docs/api/api-reference/models/list-all-models-and-their-properties.md 。完整 URL `GET https://openrouter.ai/api/v1/models`，规范全局 Bearer 认证未被操作覆盖。
- 认证说明：https://openrouter.ai/docs/api_reference/authentication.md 。通用 API 基址 `https://openrouter.ai/api/v1`，Authorization Bearer；该认证页单独不说明 models 是否可匿名，不据此外推匿名能力。
- JSON 根有 data、total_count、links.next；不传 offset/limit 时规范说明返回完整匹配列表，默认筛选文本输出。本卡不扩大到不支持的纯音频/图片输出推理。
- 条目 id/name、context_length、top_provider.max_completion_tokens、architecture.input_modalities、supported_parameters 等有明确字段。nullable/缺失不是无限或零；参数支持不等于支持所有取值，reasoning 元数据/effort 只按明确值映射，不从名称或价格推断。

## OpenCode Go

- 官方文档：https://opencode.ai/docs/go/ 。目录地址为 `https://opencode.ai/inference/go/v1/models`，不是旧 Zen 地址。文档直接给出未携带 Key 的 curl 示例。
- master 对该公开目录做了一次无凭据读取，得到 JSON：根 object 字符串和 data 数组，条目 id/object/created/owned_by；未取得名称、上下文/输出预算、模态、reasoning 或逐模型接口字段。不保存原始目录、当前模型 ID 或价格。
- 本卡目录读取使用该固定公开 URL，不发送用户 Key。不把公开目录成功当作认证或推理资格验证。
- 官方列出的推理 URL 分属 `https://opencode.ai/inference/go/openai/v1/chat/completions`、同族 `/responses`、`https://opencode.ai/inference/go/anthropic/v1/messages` 以及 Google 原生路径。Mica 只支持既有三类协议；本卡不新增 Google Provider。
- 当前公开目录缺逐模型接口字段。本卡不用模型名猜协议或把目录全部默认成 Completions；新选中的 Go 模型需调用方显式指定现有支持的 API。后续完整登录装配按官方协议族地址处理，不能把不同族路径当成一个可通用 baseUrl。
- 文档要求客户端使用自己的 User-Agent，并用 `x-opencode-session` 发送每对话稳定 ID。使用 Mica 标识及调用方提供的不含工作区路径的会话标识，不冒充其他客户端；本卡不新增身份持久化格式。

## 本卡产品约束，不冒充官方限制

目录发现每次最多一次 GET、总计 30 秒、零自动重试、不跟随重定向、解压后最多 16 MiB、最多 10000 条。OpenRouter 不传分页参数；若 next 非空，只标明结果不完整，不跟随服务端任意地址。失败、空目录和需要手填分别表达，不自动发推理测试。

自定义供应商没有统一、已核实的渠道目录约定。调用方明确提供运行期目录 URL 时才按严格同源约束尝试；没有 URL 则返回需要手填，不猜路径或新增配置字段。这个能力边界必须由后续 `/login` 清楚呈现，不伪装成远端空列表。
