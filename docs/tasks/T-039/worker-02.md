# T-039 worker-02 执行报告

## 结论

按 [review-01.md](review-01.md) 与 [rework-01-request.md](rework-01-request.md) 连续修复三项维护层缺陷：凭据保护异常不再被当作已提交、改 Key 成功后重新核对 models 基线、编辑/发现句柄的嵌套基线不可变。仅修改 `_connection_maintenance.py` 和 `tests/test_connection_maintenance.py`；未改 ConfigStore/CredentialStore、权限、公共 API/格式、依赖、Provider/App/TUI，也未新增跨文件事务或反向锁序。第一轮 `worker-01.md`、`validation.md` 未改动，本轮只新增 `worker-02.md`、`validation-02.md`。

产品仍未提交、未推送、未暂存；结论仍限于本地维护业务，不代表 `/login`、应用/终端接入、真实渠道认证或 C1 整体通过。

## 三项修复

### 1. 保护异常不再声明凭据已提交

- 删除了 `CredentialProtectionError` 分支中的 `completed=(credential,)`。该异常可能在目录/文件保护前检发生，也可能发生在替换后复核，仅凭类型无法确认提交。
- 新增连接：`status=unconfirmed`、`completed=()`、`remaining=("credential","models")`；凭据与连接记录均列为待确认/未完成，不声称旧状态仍存在。
- 改 Key：`status=unconfirmed`、`completed=()`、`remaining=("credential",)`；保留底层保护提示的真实文本（替换后仍包含 "replaced" 语义）。
- `MaintenanceOutcome` 文档明确：`completed` 只含已确认提交范围；`remaining` 在 unconfirmed 下是未完成或待确认范围。
- delete 原有语义保持：models 若已确认删除，仍留在 `completed=("models",)`，credential 进入 `remaining`。
- 新增写前保护失败回归：新增连接断言 auth.json 不存在；改 Key 断言原 auth 字节完全不变，并检查结果字段。保留并修正替换后保护未确认的真实字节、提示和结果回归。

### 2. 改 Key 提交后核对 models 并如实报告竞争

- 凭据正常提交后调用既有 `_require_current_models` 再核对一次目标完整基线：
  - 连接被并发改名/禁用/删除：`status=partial`、`completed=("credential",)`、`remaining=("models",)`、`connection=None`，提示说明 Key 已保存、连接已变化并要求刷新；不返回旧 `ConnectionSummary`。
  - 后验读取失败：`status=unconfirmed`、`completed=("credential",)`、`remaining=("models",)`、`connection=None`，明确凭据已保存但连接状态待确认。
- 不回滚凭据、不重建被删 models、不重试覆盖并发编辑；后验快照之后的新变化仍按既有快照失效规则处理。
- 回归在真实 auth 原子替换前用另一窗口的真实 `ConfigStore` 执行禁用或删除，再继续真实 `CredentialStore` 替换；检查磁盘 Key 已更新、连接已被改、返回值不是 complete。另以后验读取失败注入验证"凭据已提交"事实仍保留。

### 3. 深度隔离用于 CAS 的嵌套基线

- `begin_edit` 现在递归冻结完整 models/auth 基线：映射转为只读映射，列表/元组转为元组；嵌套模型字典、`input` 列表和 auth 未知扩展均不可原地修改。
- 所有传给既有存储的记录在边界处用 `_thaw` 还原为原生 dict/list，完整 JSON 类型和值与读入时一致，避免 tuple/list 差异造成恒定 CAS 冲突。
- `ConnectionEdit`/`CatalogDiscovery` 仍为短生命周期、repr 隐藏、无全局句柄表或凭据缓存；底层存储契约未改。
- 新增两个闭环回归：修改嵌套模型/列表/auth 扩展被拒绝后，原基线仍可完成真实 `rename` 和 `save_catalog_selection`，证明不是只冻结数据却破坏业务。

## 修改/创建的文件

- `src/mica_agent/_connection_maintenance.py`：保护异常结果、改 Key 后验核对、递归冻结/还原基线与结果语义文档。
- `tests/test_connection_maintenance.py`：新增/修正 9 个回归实例（含禁用/删除参数化），覆盖写前与替换后保护、并发 models 变化、后验读取失败、两个嵌套基线闭环。
- 新增 `docs/tasks/T-039/worker-02.md`、`validation-02.md`。
- 未改 `docs/module_map.md`：本轮没有新增模块或改变既有模块职责，只修 helper 内部行为。

## 验证结果

- 修前回归有效性：在隔离临时副本还原修前实现后运行本轮 9 个新增回归，结果为 **9 failed in 8.54s**；失败点分别为凭据被错误列入 completed、并发禁用/删除后仍返回 complete、后验读取失败仍返回 complete、嵌套 models 仍为可改 list。
- 当前新增回归：**9 passed in 8.45s**。
- 维护模块：`uv run --no-sync --offline pytest tests/test_connection_maintenance.py -q -W error` → **37 passed in 32.40s**。
- 最终专项：`uv run --no-sync --offline pytest tests/test_connection_maintenance.py tests/test_config_service.py tests/test_model_catalog.py -q -W error` → **267 passed in 146.16s**。
- 最终全量：`uv run --no-sync --offline pytest -q -W error` → **2927 passed, 3 skipped in 375.31s**；3 项为既有平台/条件跳过，本轮未新增跳过。
- `uv run --no-sync --offline ruff check .` → All checks passed。
- `uv run --no-sync --offline ruff format --check src/mica_agent tests` → 124 files already formatted。
- `git diff --check` 通过；本轮新增/修改文件无尾随空白。
- 未改依赖、锁文件、分发或打包配置；未构建。

说明：全仓 `ruff format --check .` 仍报告 7 个既有文档文件（T-024、T-034、T-036、T-037、T-038）可被重排，均非本轮改动且属于旧任务产物，按范围未修改；本轮涉及的 src/tests 124 个文件全部通过格式检查。完整命令与输出见 [validation-02.md](validation-02.md)。

## 风险与未完成

- 未实现 `/login` 菜单、应用/终端接入或真实用户验收；未验证真实 DeepSeek/OpenRouter/OpenCode Go 登录、账号资格、额度或推理可用。
- 本机为 Windows；POSIX 分支未实机运行。
- 逐文件原子/CAS 仍不是跨文件事务，崩溃不回滚；改 Key 的后验核对是提交后的快照检查，检查之后的新变化仍按既有快照失效规则处理。
- 保护异常采用保守结果：即使替换实际已发生，也不把 credential 放进 completed；调用方必须刷新并重新读取，不能把 `remaining` 当作"绝未写入"。
- 本轮不新增候选实践；review 已明确"编辑句柄一次操作有效"是原卡要求，不提升为新规则。

隔离说明：测试仅使用临时目录、假 `TEST_ONLY_*` Key、隔离 ACL 和 HTTPX 模拟 transport；修前对照只在系统临时目录的副本中运行，未改动工作区产品文件，未读取真实配置/凭据或访问真实渠道。