# T-037 返工 1 报告

## 任务完成情况总览

针对 [review-01.md](review-01.md) 的三项问题完成修复，保留第一轮存储与安全写入主体，不推倒已有实现：

1. **区分目标缺失与显式无效值**：`ConfigStore.update_provider`/`delete_provider` 与 `CredentialStore._write` 改为按键是否存在判断；已存在的 null/非对象条目在写入前拒绝并保留原字节，真正缺失仍可创建，stale 仍冲突，其他记录与未知字段保留。
2. **严格 JSON 输入/输出闭环**：`serialize_json` 递归拒绝任意层级非字符串对象键（含数组/元组容器），编码后必须通过既有 `parse_json` 严格读回才允许原子替换；不通过键强转、剥离未知字段或放宽重复键修补。
3. **消除反向依赖**：新增最小内部模块 `_config_validation.py`（文本/URL、渠道、接口标识、保留前缀），`ConfigStore` 与 `ConfigService` 单向依赖它；删除 `_read_rules` 及 `ConfigStore → ConfigService` 导入。Provider 默认预算、思考映射、来源解析、连接冻结与权限行为仍在服务层。

第一轮报告与证据未修改；本卡存储主体、Windows 安全写入路径与实际 ACL 证据沿用。

## 修改/创建的文件

- `src/mica_agent/_config_validation.py`（新增）：`API_IDS`、`CHANNELS`、`RESERVED_PREFIXES`、`is_text`、`validate_url`；`validate_url` 内部延迟导入 httpx，保持轻量导入契约。
- `src/mica_agent/config_store.py`：删除 `_read_rules` 反向导入；`serialize_json` 增加非字符串键递归拒绝与严格读回；`update_provider`/`delete_provider` 使用存在性判断，拒绝非法已存在条目；共享规则改为导入 `_config_validation`。
- `src/mica_agent/credential_store.py`：同样按存在性区分缺失/null/非对象；拒绝非法已存在条目；复用 `RESERVED_PREFIXES`/`is_text`。
- `src/mica_agent/config_service.py`（窄改）：以导入别名引用共享 `_text`/`_url`/`_CHANNELS`/`_RESERVED`/`_API_IDS`，成员判断改用 `_API_IDS`；业务逻辑未改。
- `tests/test_config_store.py`：新增 null/非对象目标、非字符串嵌套键（含 tuple）、序列化严格读回、AST 依赖方向回归；更新非对象目标预期为 `ConfigError`。
- `tests/test_credential_store.py`：新增 null/非对象凭据目标回归。
- `tests/test_config_service.py`：新增共享接口标识与 Provider 默认映射键同步断言。
- `docs/module_map.md`：登记 `_config_validation.py` 职责。

## 验证结果

完整命令、失败反例与根因见 [validation-02.md](validation-02.md)，摘要：

- 修复前先补回归：**7 failed**（null 目标被当作缺失、非字符串键写入成功、反向依赖存在），修复后全部通过。
- 最终专项：`uv run pytest tests/test_config_store.py tests/test_credential_store.py tests/test_credential_permissions.py tests/test_config_service.py -q -W error` → **234 passed, 1 skipped**。
- 最终全量：`uv run pytest -q -W error` → **2823 passed, 3 skipped**。
- Ruff check、Ruff format --check、`git diff --check` 均通过。
- 第一次全量暴露裸安装契约失败（`_config_validation` 顶层导入 httpx，`mica --help` 在无依赖环境失败），改为 `validate_url` 内延迟导入后，裸安装入口点 4 passed，最终全量与专项重新通过。
- 静态核对：`config_store.py` 不再含 `config_service` 引用，两方均单向导入 `_config_validation`；`test_shared_api_ids_stay_in_sync_with_provider_defaults` 防共享接口标识漂移。
- 权限实现未改，第一轮真实 Windows ACL 证据继续适用；测试仍使用隔离目录与假 Key。

## 未完成事项或风险

- POSIX 权限分支仍未在本机实机执行（Windows 环境，对应条件用例跳过）；维持第一轮披露。
- `_config_validation.validate_url` 延迟导入 httpx 后，若在缺少运行依赖的环境里直接调用 Provider 保存路径，仍会按缺依赖报错；裸安装契约只覆盖 `mica_agent` 导入与 CLI 帮助/版本入口，已由 `test_distribution` 覆盖。
- 本轮未新增认证类型、删除业务或跨文件事务；权限、Provider、配置格式与公共导出未改。
- 未暂存、提交或推送；工作树含第一轮保留改动与本轮增量，等待 master 验收。

## 建议沉淀

- lessons.md：共享低层校验模块不能引入新的重量级顶层依赖，否则会破坏既有“轻量导入 / 裸安装入口”契约；抽取依赖时应在同一次全量中验证安装态契约。
- lessons.md：以 `dict.get(key)` 判断“目标不存在”会把 null 与缺失混为一谈；需要写前拒绝损坏记录的存储入口，应先判断键是否存在，再校验条目类型。
