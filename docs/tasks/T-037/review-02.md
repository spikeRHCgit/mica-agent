# T-037 第 2 轮审查 · 2026-10-25

依据：[任务卡](T-037.md)、[返工要求](rework-01-request.md)、[worker-02.md](worker-02.md)、[validation-02.md](validation-02.md)。第一轮权限实现与实际 ACL 证据按报告引用，不重建已结束探针。

## 结论

**验收通过，C1 第 1 步安全连接存储完成。** 第一轮发现的两项保存漏洞及反向依赖均已修复，保留现有存储和 Windows 保护主体，不再返工或重做同类诊断。后续进入 C1 目录业务；本卡不代表 `/login`、真实渠道登录或 C1 整体通过。

## 判断审查

- Git 基线 `f910d79`；跟踪 diff 与未跟踪的新实现/测试/报告合并后符合原卡及返工授权。worker 未改任务卡、STATUS、计划、旧报告、依赖、Provider、公共导出或真实配置，未暂存或提交。
- ConfigStore 通过显式存在性与已有条目类型检查区分缺失和 null/非对象；CredentialStore 同样先校验已有目标，再进行预期记录比较。正常缺失创建、stale 冲突、删除及无关字段保留没有被放宽。
- serialize_json 在编码前递归拒绝 Mapping/list/tuple 内非字符串对象键，失败转安全 ConfigError；编码后保留大小上限、allow_nan=False，并以既有 parse_json 严格读回，校验位于替换前。没有键强转、删除未知字段或关闭重复键保护。
- ConfigStore、CredentialStore、ConfigService 单向依赖最小 `_config_validation`；文本/URL 规则和原服务实现等价，接口标识与默认映射键有同步断言。Provider 默认预算、思考映射与连接来源业务仍留在服务层。httpx 仅在 URL 校验时导入，避免污染轻量入口。
- 静态检查新回归的输入和断言：null/非对象的 models/auth 都保护原字节；非字符串键覆盖碰撞、单键、数组和 tuple；正常未知字段严格读回；AST 检查覆盖相对导入。没有通过删除或放宽旧断言取得通过。

## 验证采信与 master 抽查

- 采信失败回归：修复前 7 failed，修复后含 tuple 参数共 8 passed，反例与第一轮 master 复现一致。
- 采信 worker 最终专项 **234 passed / 1 skipped**、最终全量 **2823 passed / 3 skipped**，Ruff check、format check 和 diff check 通过。
- 第一遍全量暴露的裸安装入口 4 项失败已保留：根因为共享模块顶层导入 httpx，修正为函数内导入；裸安装入口单独 4 passed，随后重新完成最终专项与全量。不是跳过失败或删测试。
- master 按高风险卡仅抽查关键保护与窄集成，不重跑全量。实际命令：

```text
uv --directory C:/Users/10601/Desktop/learning/mica-wt/T-037 run --no-sync --offline pytest tests/test_config_store.py::test_null_or_non_object_provider_target_is_not_treated_as_absent tests/test_config_store.py::test_non_string_keys_are_rejected_before_writing tests/test_config_store.py::test_serialized_configuration_is_strictly_readable tests/test_config_store.py::test_config_store_depends_on_shared_validation_not_the_service tests/test_credential_store.py::test_null_or_non_object_credential_target_is_not_treated_as_absent tests/test_config_service.py::test_shared_api_ids_stay_in_sync_with_provider_defaults tests/test_config_service.py::test_provider_and_credential_stores_feed_the_existing_parser -q -W error
```

实际结果：**10 passed in 3.73s**。全部使用隔离目录与假 Key，无真实目录/凭据或模型请求。

## 剩余边界与交接

- 本卡权限实现返工未变化，第一轮实际 Windows ACL 与受保护读取证据继续适用。POSIX 分支未实机执行，对应条件跳过仍保留为平台验证限制，不能称跨平台实机验收已齐备。
- `/login`、目录请求、跨文件连接业务、OAuth/token 刷新未在本卡交付；后续按既定 C1 顺序实现，不重开本卡功能范围。
- module_map 仅同步新增存储和共享规则的职责；未改变配置格式、默认选择或既有读取行为。
- master 将本卡产品、测试、报告及状态统一本地提交、ff-only 归并；按 STATUS 已有的卡片验收后推送授权同步并核对远端。实际同步结果由 STATUS/任务卡 Git 摘要记录，不把提交、测试和推送混为一项。
