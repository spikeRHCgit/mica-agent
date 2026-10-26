# T-039 第 1 轮审查 · 2026-10-26

依据：[任务卡](T-039.md)、[worker-01.md](worker-01.md)、[validation.md](validation.md)。

## 范围与已采信证据

- Git 状态与报告一致：新增维护 helper/测试/报告，ConfigService 仅内部延迟构造入口，model_catalog 仅默认地址查表，module_map 只对应职责更新；未越界修改底层存储、权限、公共格式、依赖或 App/TUI。
- 审查了安全视图与编辑基线、跨文件保存及结果映射、目录委托、auth → models 保存锁序、迟到结果和手填路径。现有目录/删除主体实现可保留，没有建立第二套配置缓存或 Agent Loop。
- 采信最终专项 **260 passed**、全量 **2920 passed / 3 skipped**、全仓 Ruff/格式及 diff check 证据；不重复全量。Windows 隔离 ACL 证据有效，POSIX 未实机。
- master 关键抽查实际命令：

```text
uv --directory C:/Users/10601/Desktop/learning/mica-wt/T-039 run --no-sync --offline pytest tests/test_connection_maintenance.py::test_models_failure_after_credential_reports_partial_commit tests/test_connection_maintenance.py::test_discovery_uses_baseline_key_without_holding_store_locks tests/test_connection_maintenance.py::test_key_change_during_discovery_rejects_stale_selection tests/test_connection_maintenance.py::test_delete_partial_when_credential_changed_after_edit -q -W error
```

实际 **4 passed in 3.58s**。

## 独立复核确认的缺陷

### 1. 保护异常并不能证明凭据已提交

`_connection_maintenance.py` 242～248、343～348 行捕获任意 CredentialProtectionError 后，把 credential 放入 completed。底层该异常也可能来自写入前的目录/文件保护检查，并不只表示替换后复核失败。

master 在临时根内复用真实 ConfigStore/CredentialStore，只在 `ensure_credential_directory` 权限边界注入保护失败：新增连接的 auth.json 根本未创建，却返回 unconfirmed / completed=[credential]；改 Key 的 auth 原字节完整保留，也返回相同 completed。status=unconfirmed 不能抵消 completed 中的确定提交声明。当前测试只覆盖替换后的保护失败，未保护替换前分支。

### 2. 改 Key 的 models 竞争窗口被报告为完整成功

324～369 行只在凭据写入前检查 models，成功后直接返回 complete 和旧基线摘要。报告已披露窗口，但原卡 §3 要求发生部分提交后的冲突如实报告；不要求消除所有跨文件竞争或新增事务。

master 在实际 auth 文件原子替换之前插入另一窗口的真实 ConfigStore 禁用/删除操作，然后继续原 CredentialStore 替换。两例 Key 均已提交，但返回 complete，并携带旧 ConnectionSummary；禁用例摘要仍 enabled=true，删除例仍声称连接存在。

需要修复的是可观测竞争后的后验核对及结果，不是要求跨文件回滚：提交后发现连接变化，应报告 Key 已提交且连接已变化/待刷新，不返回旧摘要或完整成功。不能通过事后回滚覆盖并发编辑。

### 3. 编辑基线不是深度不可变

216～217 行仅对顶层 dict 包 MappingProxyType。`edit._expected_provider["models"][0]["name"]` 仍可直接改写；内部嵌套列表、模型字典和未知扩展未被隔离。磁盘未变化，但句柄用于后续 CAS 的基线已能被消费者改变，违反任务卡不暴露可修改原始基线的要求和 ConnectionEdit 的 immutable 说明。

应保证通过约定的内部访问方式取得嵌套值时，不能改变句柄真正用于 CAS 的事实；可采用内部不可变表示或防御性副本，不要求防御反射强行修改 Python 对象。传给既有存储的 JSON 类型与完整记录比较必须保持兼容，不能直接把 list 变 tuple 后造成恒定冲突。

## 复核证据

一个 uv run --no-sync --offline python 复核脚本在全新临时 HOME/配置根中运行，使用假 Key、真实隔离文件/ACL及现有测试构造，仅在权限检查和文件替换边界注入失败/确定性交错。没有 fake 维护逻辑或存储 CAS，没有真实网络或凭据读取。输出仅布尔、计数和固定枚举：

| 输入/交错 | 实际结果 |
|---|---|
| 新增：写前保护失败 | unconfirmed；completed=[credential]；auth_exists=false |
| 改 Key：写前保护失败 | unconfirmed；completed=[credential]；auth_bytes_unchanged=true |
| auth 替换前并发禁用 | complete；credential_committed=true；provider_enabled=false；仍返回连接摘要 |
| auth 替换前并发删除 | complete；credential_committed=true；provider_exists=false；仍返回连接摘要 |
| 修改编辑基线内模型字典 | nested_mapping_mutable=true；disk_bytes_unchanged=true |

最终“结果/基线不变量应成立”的断言失败，命令 exit 1；这是单个脚本中的五种输入，不是宣称已有 pytest 五项失败。真实请求 **0**、真实凭据读取 **false**。

## 结论

**针对性返工，产品暂不验收。** 按 [rework-01-request.md](rework-01-request.md) 修三项维护层问题，不引入跨文件事务、不改底层异常/存储契约，不推翻已有实现或重做渠道调研。

worker 提议的“编辑句柄一次操作有效”已是原卡的明确要求，不重复加入通用实践或提升为新规则。第一轮报告/证据冻结；下一轮只写 worker-02.md、validation-02.md。产品继续未提交，master 仅保存审查交接文档。
