# T-039 验证记录

## 环境与隔离

- Windows worktree：C:/Users/10601/Desktop/learning/mica-wt/T-039；分支 task/T-039-connection-maintenance；HEAD 11d1722。
- 环境准备：uv sync --python 3.11，创建 CPython 3.11.15 虚拟环境并安装 25 个包；未改依赖。
- 测试只使用 pytest 临时目录、假 TEST_ONLY_* Key、临时文件、真实 Windows ACL（仅隔离临时文件/目录）和 httpx AsyncBaseTransport 模拟网络；未读取真实 ~/.mica、真实 Key、真实渠道，未发送真实模型/推理请求。
- 底层 ConfigStore/CredentialStore 正常路径不 fake；失败只在文件替换、受保护读取和网络 transport 边界注入。

## 首个失败、根因与修正

新测试文件首次运行：

    uv run --no-sync --offline pytest tests/test_connection_maintenance.py -q -W error
    3 failed, 24 passed in 23.73s

1. test_full_connection_maintenance_round_trip 在 edit.custom_url 处断言失败，实际值为内置 DeepSeek 固定地址；根因是 begin_edit 对所有渠道都暴露 baseUrl/api，而卡片要求自定义 URL 只作为自定义编辑的运行期值，内置不接受地址覆盖。修正为仅当基线渠道为 custom 时填充 custom_url/custom_api，内置编辑句柄保持 None；并让显示渠道回退与原始渠道分开，避免把无效渠道误当 custom 编辑。
2. test_change_key_rejects_disabled_or_removed_connection 收到未映射的 ConfigConflictError：change_api_key 的 models 基线预检在结果映射块之外抛出，调用方只能看到异常，不能报告「未提交任何内容」。修正为捕获该冲突并返回 status=conflict、remaining=(credential,)，不写凭据。
3. test_catalog_failure_keeps_connection_and_allows_manual_fill 的断言期望手填替换已有模型；实际实现按卡片保留既有模型并在其后追加，属于测试期望错误而非产品缺陷。修正断言为保留 anchor-model 并追加 fallback。

随后还修正了测试自身的基线问题：凭据文件在首次写入后会按 serialize_json 规范化，比较字节应从「首次添加后」取基线；目录保存提交后旧编辑句柄按设计过期，后续操作需重新 begin_edit；轮转测试结束前需删除同名的第二连接。这些都不改写产品结论。

修正后同一文件 27 passed；补充「删除后不复用 ID」和「改 Key 保护未确认」两项后为 30 passed in 23.57s。

## 专项验证

最终命令与结果：

    uv run --no-sync --offline pytest tests/test_connection_maintenance.py tests/test_config_service.py tests/test_model_catalog.py -q -W error
    260 passed in 112.14s

其中新增连接维护 30 项、既有 config_service 与 model_catalog 回归 230 项（含新增 1 项内置默认查表测试）。

## 全量验证

    uv run --no-sync --offline pytest -q -W error
    2920 passed, 3 skipped in 330.34s

3 项 skip 为既有平台/条件用例，本轮未新增 skip。全量在最终代码稳定后集中执行一次。

静态检查与工作区：

    uv run --no-sync --offline ruff check src/mica_agent tests
    All checks passed!
    uv run --no-sync --offline ruff format --check src/mica_agent tests
    124 files already formatted
    git diff --check
    （无输出，退出码 0）

未执行 uv lock --check 或 uv build：本轮未改依赖、锁文件、安装入口、打包规则或包资源，按卡不另构建。

## 关键行为证据

- **完整闭环**：真实临时存储上完成添加内置 DeepSeek、ConfigService.load/resolve、模拟目录发现（mock 200）、显式勾选保存、改名、禁用/启用、改 Key、创建同名自定义连接、改 URL/API、手填模型、无确认删除与确认删除。断言 32 位稳定 ID、同名共存、内置默认地址/接口、字段和未知字段保留、默认模型和 config.json 字节不变。
- **凭据安全**：列表只返回 available/missing/unsupported；编辑/发现对象 repr 为固定文本，不含假 Key、自定义 URL 或临时根路径；非法 Key/URL/名称/模型能力的 ConfigError 不包含输入秘密；auth 读取走 ConfigStore.read("auth")。
- **添加部分完成**：注入 auth.json 替换失败时无文件写入，status=failed；注入 models.json 替换失败时 status=partial、completed=(credential,)、remaining=(models,)，真实 auth.json 留有新凭据而 models 无记录；保护复核失败时 status=unconfirmed、hint 保留 "replaced" 且不出现 Key。
- **编辑冲突**：并发改名后旧句柄 rename/delete 均返回 conflict，models 字节保持并发后的状态；改 Key 前发现 models 已禁用/删除时返回 conflict 且 auth 不变；非 api_key 认证类型拒绝改写。
- **目录/凭据过期**：发现期间并发改 Key，save_catalog_selection 返回 conflict，models 字节不变；发现期间禁用，返回 conflict；发现期间删除后保存，返回 conflict 且 models/auth 均为空，未重建连接。保存路径在 auth.json.lock 内受保护重读并只比较目标记录，随后调用 T-038 save_selection；该路径未调用 CredentialStore。
- **锁与取消**：慢响应 handler 等待 Event 期间，用 0.2 秒超时成功取得 auth.json.lock 与 models.json.lock，证明网络等待不持存储锁；取消后得到 CancellationRequested，transport 已关闭、无响应遗留、asyncio.all_tasks 无残留请求/观察任务，锁仍可取得。
- **删除部分完成**：并发更换凭据后删除，status=partial、completed=(models,)、remaining=(credential,)，连接不可选而并发凭据保留；保护复核失败时 status=unconfirmed 且保存模型已删除；无凭据时不同步构造 auth 删除目标，auth.json 不被创建。
- **手填**：只写显式 id/api/name/contextWindow 等字段，未提供的 maxTokens、input、reasoning 不写入；ConfigService 解析显示 maxTokens 来源为 adapter default、input 为 program default，未伪装成发现值。既存手填记录的未知字段在更新时保留。
- **内置渠道边界**：connection_defaults 返回 DeepSeek/OpenRouter 的 Completions 和 Go 的 OpenAI 协议族基址；Go 目录请求不带 Authorization、带 x-opencode-session；Go 新模型无 api_choices 时 save_catalog_selection 抛出安全 ConfigError 且 models 不变，提供显式选择后保存成功。
- **非法结构/保护**：providers 根不是对象、目标条目不是对象、auth 目标不是对象、models JSON 损坏均明确抛 ConfigError；隔离 auth 文件被放宽为 shared 权限时抛 CredentialProtectionError，不把不可读凭据当缺失重建。

## 未验证与剩余边界

- 未连接真实 OpenRouter、DeepSeek、OpenCode Go，未验证账号资格、额度、Key 真实认证或推理；模拟目录成功和本地保存均不代表远端认证成功。
- 未实现 /login 菜单、应用/终端接入或真实用户验收；本轮结论不包含 C1 整体通过。
- 未在 POSIX 实机运行；Windows 已用真实临时 ACL 验证受保护存储路径，3 项全量 skip 与既有记录一致。
- 跨文件仍是逐文件原子/CAS，不是事务：进程在两次提交之间崩溃不会自动回滚。change_api_key 的 models 预检与 auth 写入之间无跨文件锁，极窄窗口内并发 models 变更可能留下孤立凭据；按卡未新增跨文件事务或反向锁序。
