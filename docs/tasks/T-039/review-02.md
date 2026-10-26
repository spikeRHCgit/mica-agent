# T-039 第 2 轮审查 · 2026-10-26

依据：[任务卡](T-039.md)、[返工 1](rework-01-request.md)、[worker-02.md](worker-02.md)、[validation-02.md](validation-02.md)。

## 范围与判断

- 工作树与交付清单一致：保留第一轮产品实现，本轮修复集中于维护 helper 与对应测试，新增本轮报告/证据；第一轮已提交产物未变化，未越界修改底层存储、权限、公共格式、依赖或 App/TUI。
- 保护异常不再推断提交阶段：create/change_key 的 credential 不进入确定 completed，未完成/待确认范围明确；保留底层替换后保护未确认提示，delete 已确认的 models 提交事实未丢失。
- change_api_key 保留前检和 auth CAS，正常保存后重读完整 models 目标；可观测并发变化返回 partial，不可读返回 unconfirmed，两者均保留 credential 已提交事实且不返回旧摘要。不回滚、不重建、不增加跨文件事务或锁序。
- begin_edit 递归复制并冻结 models/auth 基线，目录句柄沿用同一不可变基线；各存储/CAS 边界递归还原原生 dict/list，完整比较、手填、删除和目录保存不因 tuple/list 差异失效。auth → models 同步保存锁序与网络等待无锁的边界保留。
- ConfigService 仍仅延迟构造内部维护入口，目录仅增加内置默认查表，无第二套配置状态或网络实现；轻量入口、load/resolve/for_run 和顶层导出未改。

## 证据与补充抽查

采信本轮修前 9 failed / 修后 9 passed、维护模块 37 passed、专项 267 passed、全量 **2927 passed / 3 skipped**，以及全仓 Ruff check、src/tests 124 文件格式检查和 diff check。全仓 `ruff format --check .` 实际仍有 7 个既有旧任务文档可重排；未判为全仓格式通过，不要求越界重排历史产物。无依赖或分发变化，不另构建。

本卡高风险，master 仅执行一条定点抽查命令，不重复全量：

```text
uv --directory C:/Users/10601/Desktop/learning/mica-wt/T-039 run --no-sync --offline pytest tests/test_connection_maintenance.py::test_create_pre_write_protection_failure_does_not_claim_a_commit tests/test_connection_maintenance.py::test_unconfirmed_protection_on_create_preserves_meaning tests/test_connection_maintenance.py::test_key_change_pre_write_protection_failure_keeps_original_credential tests/test_connection_maintenance.py::test_key_change_unconfirmed_protection_preserves_meaning tests/test_connection_maintenance.py::test_key_change_reports_concurrent_models_change_after_credential_commit tests/test_connection_maintenance.py::test_key_change_after_credential_read_failure_keeps_confirmed_commit tests/test_connection_maintenance.py::test_nested_edit_baseline_is_immutable_and_still_saves tests/test_connection_maintenance.py::test_nested_discovery_baselines_are_immutable_and_still_save -q -W error
```

实际 **9 passed in 8.36s**。覆盖写前/替换后保护失败、auth 替换前真实并发禁用/删除、后验读取失败，以及两类嵌套基线修改被拒绝后的真实保存闭环。仅临时目录、假 Key、隔离 Windows ACL 与模拟网络，无真实配置/凭据或渠道访问。

## 结论与边界

**验收通过。** 三项返工要求均完成，接下来进入应用装配与终端 `/login` 接入，不继续扩展渠道诊断。不新增候选实践或工程规则。

本结论仅覆盖内部维护业务；`/login`、真实认证/额度/推理、Go 按 API 选择推理地址与自有头、POSIX 实机和 C1 整体验收仍未完成。跨文件原子性与崩溃回滚不作承诺；后验快照之后的新变化仍由既有快照失效/for_run 规则处理。
