# T-039 worker-01 执行报告

## 结论

按 [T-039.md](T-039.md) 完成 C1 第 3 步「连接维护业务」：在 ConfigService 提供一个内部控制维护入口，由新模块 _connection_maintenance.py 单一实现安全列表、编辑基线、添加、改名、启停、改 Key、自定义地址/接口、目录发现、勾选保存、手填模型和确认删除；多文件失败按已完成/未完成范围报告，迟到目录结果不覆盖新凭据或重建已删除连接。

本轮交付的是**本地配置与凭据业务闭环**，不是 /login、应用/终端菜单、真实渠道认证或 C1 整体验收。未提交、未推送、未暂存；未读取真实配置/凭据，未访问真实渠道，未发送真实模型请求。

## 完成情况总览

- **内部入口**：ConfigService._connection_maintenance() 延迟构造 helper，复用同一个 ConfigStore；helper 不反向导入 ConfigService，未增加全局句柄表或长期凭据缓存。
- **安全视图**：列表/操作结果只包含稳定 ID、显示名、渠道、enabled、凭据可用状态（available/missing/unsupported）、固定状态类别和必要提示。原始 Key、完整供应商记录、自定义 URL 只存在于 repr 隐藏的短生命周期编辑/发现对象中，CAS 基线使用只读映射。
- **添加**：业务生成 32 位小写 UUID；同名连接合法，删除后不复用旧 ID。内置渠道从 model_catalog.connection_defaults() 取 T-038 固定表的地址和默认接口；自定义要求显式名称/URL/Key/三种接口之一。先校验 Key、完整供应商记录和 ID，再保存凭据，最后保存 models 记录。
- **编辑**：改名保持 ID；启停保留模型与凭据；自定义可改 URL/API，内置拒绝覆盖地址和接口；改 Key 先校验并保留旧记录直到受保护原子替换提交；其他认证类型不被改写成 api_key。所有 models 写入使用完整目标记录 CAS。
- **目录**：发现前由编辑句柄取得 models/auth 基线，网络等待不持任何存储锁；只把受保护读取的 Key、渠道事实和调用时的 token/transport/自定义 catalog URL/会话 ID 交给 T-038。保存仍调用 save_selection：先在既有 auth.json.lock 内受保护重读并拒绝过期 auth 基线，再执行 models CAS；该路径不调用 CredentialStore，锁序固定为 auth → models，未新增反向锁序或通用事务。
- **手填**：独立入口只写显式 ID/API/能力，未知能力保持缺失；不改默认、根字段、其他供应商或凭据。
- **删除**：必须显式确认；先删 models 连接，再以 auth 完整目标基线删凭据；原本没有凭据时不构造删除目标。第一文件提交后第二文件失败时返回 partial 及残留范围；保护未确认保留 CredentialProtectionError 的真实含义并返回 unconfirmed，不猜测旧记录仍在。
- **迟到结果**：目录结果绑定发现时的编辑基线；发现期间改 Key、禁用或删除后保存会冲突，删除后迟到保存不会重建连接。

## 修改/创建的文件

- 新增 src/mica_agent/_connection_maintenance.py：连接维护业务、安全结果值、编辑/发现句柄、CAS 与部分完成编排。
- src/mica_agent/config_service.py：仅新增 TYPE_CHECKING 类型引用和延迟构造的 _connection_maintenance() 内部入口；load/resolve/for_run、公共快照和顶层导出未改。
- src/mica_agent/model_catalog.py：新增内置 Go 默认基址常量和 connection_defaults() 纯查表入口；未改请求、解析、取消或勾选合并语义。
- 新增 tests/test_connection_maintenance.py：30 项隔离测试，覆盖闭环、失败/竞争、取消/锁和敏感值清洗。
- tests/test_model_catalog.py：新增 1 项测试保护内置默认查表来源；其余目录测试未改。
- docs/module_map.md：登记新模块、测试及既有模块职责变化。
- 新增 docs/tasks/T-039/worker-01.md、validation.md。

## 验证结果

- 新 worktree 执行 uv sync --python 3.11，使用 CPython 3.11.15 虚拟环境。
- 最终专项：uv run --no-sync --offline pytest tests/test_connection_maintenance.py tests/test_config_service.py tests/test_model_catalog.py -q -W error → **260 passed in 112.14s**。
- 最终全量：uv run --no-sync --offline pytest -q -W error → **2920 passed, 3 skipped in 330.34s**；3 项为既有平台/条件跳过，本轮未新增跳过。
- uv run --no-sync --offline ruff check src/mica_agent tests → All checks passed；ruff format --check → 124 files already formatted；git diff --check 通过。
- 未改依赖、锁文件、分发、Provider/Engine、App/CLI/TUI、配置或会话格式；未构建。

失败、根因、修正和关键边界证据见 [validation.md](validation.md)。

## 危险操作与隔离

- 未执行提交、推送、stash、还原或清理他人文件；未删除任务范围外文件。
- 测试只使用 pytest 临时目录、假 TEST_ONLY_* Key 和 httpx 模拟 transport；Windows 受保护存储只对隔离临时文件/目录设置真实 ACL，没有放宽真实用户配置的保护。
- 未访问真实 ~/.mica、真实 Key、真实渠道或 T-036 预算；未发送推理请求。

## 未完成事项或风险

- 未实现应用/终端 /login 菜单和真实用户验收；OpenRouter、DeepSeek、OpenCode Go 的真实登录、账号资格、额度和推理可用均未验证。目录成功或本地保存不等于远端认证成功。
- 本机为 Windows；POSIX 分支未实机运行，虽然测试已按真实权限分支设计。
- 逐文件原子/CAS 不是跨文件事务；进程在两次文件提交之间崩溃不会自动回滚，本轮按卡不承诺。确认删除也不代表远端撤销或历史删除。
- change_api_key 在无锁状态下先比较 models 基线再写凭据；若恰好在比较后、凭据写入前发生并发 models 变更，可能留下孤立凭据。按卡不新增跨文件锁或事务，这里如实记录为该边界的剩余风险。
- 编辑/发现句柄提交后即过期；后续菜单需要重新 begin_edit()，不能在旧句柄上继续操作。迟到结果丢弃和菜单后台同步磁盘操作归后续 App/TUI 接入。

## 建议沉淀

- 候选经验：连接编辑句柄应视为一次操作有效的短生命周期基线；任何已提交操作后都应重新取基线，避免把正常并发冲突误报为产品失败。可交 master 判断是否进入角色实践。
