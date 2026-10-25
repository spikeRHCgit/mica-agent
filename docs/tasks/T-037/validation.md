# T-037 验证证据

> 验证环境：Windows 10/11（`os.name == "nt"`）· Python 3.11.15（`uv sync --python 3.11`）· pytest 9.1.1 · Ruff 0.16.2
> worktree：`C:/Users/10601/Desktop/learning/mica-wt/T-037` · 分支 `task/T-037-connection-storage` · 开始 HEAD `cc6f0fd`（基线 `c5f9226` + 派工提交）
> 本轮只使用隔离临时目录与假 Key；未读取真实 `~/.mica`、真实凭据或渠道，未修改 Provider。未提交、未推送。

## 1. 命令与结果

| 阶段 | 命令 | 结果 |
|---|---|---|
| 环境 | `uv sync --python 3.11` | 成功，CPython 3.11.15，25 个包 |
| 改前基线 | `uv run pytest tests/test_config_store.py tests/test_credential_permissions.py tests/test_config_service.py -q -W error` | 178 passed |
| 存储专项（最终） | `uv run pytest tests/test_config_store.py tests/test_credential_store.py tests/test_credential_permissions.py tests/test_config_service.py -q -W error` | **225 passed, 1 skipped**（115.03s） |
| 全量（最终） | `uv run pytest -q -W error` | **2814 passed, 3 skipped**（339.37s） |
| 静态检查 | `uv run ruff check src/mica_agent tests` | All checks passed |
| 格式 | `uv run ruff format --check src/mica_agent tests` | 119 files already formatted |
| 空白 | `git diff --check` | 通过（无输出） |

新增收集项：`tests/test_credential_store.py` 与 `tests/test_config_store.py` 共采集 57 项（56 passed + 1 项 POSIX 平台跳过）。全量 3 项跳过中 1 项为本轮新增的 POSIX 权限位断言，其余为本机平台/符号链接条件用例；本机为 Windows，POSIX 分支未在本机实际执行。

## 2. 行为闭环证据

`tests/test_config_service.py::test_provider_and_credential_stores_feed_the_existing_parser`（隔离 `data/`、`workspace/.mica/`，真实读取已有 `ConfigService`）：

1. `ConfigStore.update_provider` 新建 `local`（名称/URL/接口/模型）与 `other`，`CredentialStore.save_api_key` 写入假 Key；
2. `service.load()` 解析成功，`local/a` 解析为 `ready`，连接 Key 来自受保护 `auth.json`；
3. `save_default_model` 保存默认模型后，更新显示名与 models，旧模型变为 `unresolved`、新模型 `ready`；
4. 更换 Key 后重新 `load`，连接取得新 Key；
5. 分别删除目录项与凭据：仅剩 `other`，`config.json` 默认选择保持原值，未创建 `sessions/` 目录。

## 3. 首次失败、根因与修正证据

按实际发生顺序记录，未通过重跑掩盖：

1. **首次真实失败（Windows 安全创建）**：`test_new_api_key_is_saved_with_protected_read` 在 `_security_descriptor(directory=False)` 抛 `CredentialProtectionError`。独立 Win32 探针确认 `ConvertStringSecurityDescriptorToSecurityDescriptorW` 返回 `ERROR_INVALID_PARAMETER (87)`；根因是文件 SDDL 被拼成 `A;FA;;;SY`（缺少 ACE flags 字段分隔符），目录 SDDL 因带 `OICI` 未触发。修正为 `A;<flags>;FA;;;SY`（文件 `A;;FA;;;`，目录 `A;OICI;FA;;;`），随后目录/文件实际 ACL 创建与读取通过。
2. **临时文件父目录未验证**：`create_protected_temp` 在已有不安全目录上未拒绝。根因是 Windows 临时文件路径只校验了新文件句柄，未校验父目录；修正为先打开并校验父目录句柄，再在持有父目录句柄期间独占创建临时文件。修正后 `test_unsafe_directory_is_rejected_without_repair` 通过。
3. **测试隔离环境导致的误报**：`%TEMP%` 下的 pytest 目录带 `OWNER RIGHTS`（`S-1-3-4`）继承 ACE，生产校验按既有规则拒绝。用 `icacls` 确认该 ACE 来源后，判定为测试目录配置问题而非产品缺陷；测试改用既有真实 ACL 助手 `protect()` 建立隔离目录，未放宽任何生产 ACL 检查。
4. **临时文件验证方式**：最初测试尝试在独占写句柄仍打开时用 `read_credentials` 复核，Windows 共享模式拒绝第二次打开。改为 `ProtectedTempFile.verify()`：直接对已持有的文件句柄重新执行 ACL/属性校验，验证发生在写入之前；写入关闭后再经既有 `read_credentials` 完整复核。

## 4. Windows 真实 ACL 证据

- 新建目录/临时文件使用显式 SDDL：`D:P`（保护 DACL，不继承）、owner 为当前用户、ACE 仅当前用户 / `S-1-5-18` / `S-1-5-32-544`，目录 ACE 带 `OICI` 供子项继承。
- 校验经 `GetFileInformationByHandleEx`（拒绝重解析点/类型不符）与 `GetSecurityInfo` + `GetAce`（owner、逐 ACE 掩码、受信 SID）；失败不打印路径或内容。
- 实际通过：`test_new_api_key_is_saved_with_protected_read`、`test_key_replacement_preserves_unknown_and_unrelated_records`、`test_delete_removes_only_the_target_record`（均以 `read_credentials` 读回新文件）；`test_protected_temp_is_verified_before_writing`（写入前句柄级 `verify()` 与写入后受保护读取）。
- 实际拒绝：`test_unsafe_directory_is_rejected_without_repair` 用真实 ACL 给 Everyone 添加 `CreateFiles`，目录未被静默改权限，`ensure_credential_directory` 与 `create_protected_temp` 均拒绝。
- 既有负向保护未放宽：`tests/test_credential_permissions.py` 的共享读、可替换父目录、目录联接、安全查询失败用例全部保持通过。

## 5. 并发、失败与冲突证据

- `tests/test_config_store.py`：同目标并发创建只一方成功、另一方 `ConfigConflictError`；不同目标并发创建在锁内重读合并，互不覆盖；stale 读取更新/删除均冲突且文件字节不变。
- `tests/test_credential_store.py`：同目标并发保存只一方成功；不同目标并发保存均保留；stale/预期缺失冲突不写文件。
- 失败保护：损坏 JSON、`providers` 非对象、目标记录非对象/非 API Key、未知字段、无 `baseUrl`、重复/非法模型字段、超 4 MiB 内容、无效 ID/Key 均在写入前拒绝；`os.replace` 故障、临时文件写入故障均保留旧字节并清理自身临时文件（`test_replace_failure_keeps_original_and_cleans_temp`、`test_temp_write_failure_keeps_original_and_cleans_temp`）。
- 替换已提交但后验失败：`test_failure_after_replace_reports_unconfirmed_protection` 确认返回“已替换、保护未确认”，且磁盘上确为新记录，不谎称旧记录仍在。
- 敏感信息：断言异常消息不含 Key、目标路径或注入的测试标记；`auth.json` 读取仍只经 `read_credentials`，未新增普通凭据读入口。

## 6. 边界与未覆盖

- 本卡只实现存储能力：未实现 `/login`、目录网络请求、跨文件连接事务、OAuth/token 刷新，未修改 Provider、配置格式、依赖或顶层公共导出。
- POSIX 分支（0700 目录、0600 文件、`O_NOFOLLOW`/`O_EXCL`、`read_credentials`）已实现并写有平台条件测试，但本机为 Windows，未在本机实际执行；`test_missing_directory_is_created_private_on_posix` 在本机跳过。
- 既有不安全数据目录不会被自动改权限；Windows 上含非受信写/控制 ACE（例如某些位置继承的 `OWNER RIGHTS`）的目录会按既有保护语义被拒绝，需要用户显式保护后重试。
