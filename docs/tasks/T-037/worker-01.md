# T-037 worker 报告

## 任务完成情况总览

C1 第 1 步的配置与凭据存储能力已实现并完成回归：

- `ConfigStore` 新增 `update_provider` / `delete_provider`：按稳定供应商 ID 新增、部分更新、删除 `models.json` 目标条目；锁内重读并以完整目标记录比较冲突，保留其他供应商、根未知字段与目标未修改字段；删除不触碰 `config.json` 默认选择和会话。
- 新增 `CredentialStore`：对 `auth.json` 的 API Key 记录执行安全新增、更换和删除；固定 `auth.json.lock`，锁内经既有 `read_credentials` + `parse_json` 重读合并，保留其他供应商、根与目标未知字段；写入前拒绝非法 ID/Key/认证类型/结构。
- 扩充 `_credential_permissions`：新增安全目录创建/校验与凭据临时文件创建；Windows 用显式安全描述符创建并校验实际 ACL，POSIX 新目录 0700、临时文件 0600；写入前校验保护，flush/fsync 后经受保护入口复核，再原子替换，替换后再经受保护入口复核。
- `config.json` 与 `models.json` 共用模块内最小原子替换步骤；凭据不进入普通 `mkstemp` 路径。
- 现有默认模型保存、严格 JSON、凭据受保护读取、无凭据启动与既有防护行为保持不变。

本卡存储能力完成；**不代表** `/login`、真实渠道登录或 C1 整体通过。

## 修改/创建的文件

- `src/mica_agent/config_store.py`：严格 JSON 读取保留；新增 `serialize_json`、非凭据原子替换、Provider 记录校验、`update_provider`、`delete_provider`；`save_default_model` 改用共用原子替换，语义不变。字段类型/URL/渠道/接口校验通过局部导入复用 `config_service` 的既有读取规则（避免循环依赖），不复制常量。
- `src/mica_agent/credential_store.py`（新增）：`CredentialStore.save_api_key` / `delete`、固定锁、冲突比较、受保护临时文件写入、替换后复核；不新增普通凭据读取入口。
- `src/mica_agent/_credential_permissions.py`：保留 `read_credentials` 语义；抽出可复用的 Windows 安全边界校验，新增 `ensure_credential_directory`、`create_protected_temp`（`ProtectedTempFile.verify`/`write`/`close`）。
- `tests/test_config_store.py`：目录创建/部分更新/删除、冲突、并发合并、非法输入、超限、损坏结构、replace 失败保留字节。
- `tests/test_credential_store.py`（新增）：新增/更换/删除、未知字段保留、并发、非法输入、密钥不泄露、损坏结构、写入/replace/后验失败、临时文件保护与不安全目录拒绝；POSIX 权限位用例按平台跳过。
- `tests/test_config_service.py`：存储结果接入现有 `ConfigStore`/`ConfigService` 读取解析的窄集成。
- `docs/module_map.md`：仅更新配置与选择组的新增文件及实际职责。
- `docs/tasks/T-037/validation.md`：本轮证据、首次失败与根因、平台限制。

## 验证结果

完整命令、平台与首次失败记录见 `docs/tasks/T-037/validation.md`，摘要：

- `uv run pytest tests/test_config_store.py tests/test_credential_store.py tests/test_credential_permissions.py tests/test_config_service.py -q -W error`：**225 passed, 1 skipped**。
- `uv run pytest -q -W error`：**2814 passed, 3 skipped**。
- `uv run ruff check src/mica_agent tests`：通过；`uv run ruff format --check src/mica_agent tests`：119 files already formatted；`git diff --check`：通过。
- Windows 实际 ACL：新建目录/凭据文件通过真实 `GetSecurityInfo` 校验与 `read_credentials` 读回；给父目录真实添加 Everyone `CreateFiles` 后被拒绝且未被静默修复；既有负向权限用例全部保持通过。
- 并发与失败：同目标写入只一方成功、不同目标锁内合并；损坏 JSON、错误结构、超限输入、写入/replace 故障保留旧字节并清理临时文件；已替换但后验失败时如实报告“已替换、保护未确认”。
- 测试只使用隔离临时目录和假 Key，未访问真实 `~/.mica`、凭据或渠道。

## 未完成事项或风险

- 本机为 Windows，POSIX 权限分支未在本机实际执行（条件用例在 Windows 跳过）；需在 POSIX 环境补跑对应条件用例。
- `/login` 界面、目录网络请求、OAuth/token 刷新、跨文件连接事务属于后续 C1 卡，未实现。
- 既有数据目录若含非受信写/控制 ACL（如某些位置继承的 `OWNER RIGHTS`），会按既有受保护读取语义被拒绝，不会自动改权限；用户需显式保护目录后重试。这一点与已验收的读取保护一致，但可能影响首次在特殊目录下保存的体验，后续 C1 登录维护闭环需要给出提示路径。
- 未提交、未推送；工作区改动由 master 验收与统一 Git 管理。

## 建议沉淀

- lessons.md：Windows 凭据 ACL 测试要区分“产品校验过严”与“测试目录继承 ACE”两类失败，先用 `icacls`/`GetSecurityInfo` 确认真实 ACE 再改代码；SDDL 的 ACE 字段分隔符（`A;;FA;;;`）容易拼错，建议保留一次独立探针证据。
- lessons.md：临时敏感文件的安全验证应在持有写句柄时做句柄级 ACL 校验，关闭后再用正式读取入口复核；不要把“关闭后能读”当作写入前保护证据。
