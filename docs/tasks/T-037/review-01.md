# T-037 第 1 轮审查 · 2026-10-25

依据：[任务卡](T-037.md)、[worker-01.md](worker-01.md)、[validation.md](validation.md)。

## 结论

**需要针对性返工，不推倒已有产品实现。** 配置/凭据存储主体和 Windows 安全写入路径已交付；现有自动化及真实 ACL 证据可采信，但 master 复现了两项违背保存契约的输入，并确认存储层新增反向导入。不能仅据全量通过收口。修复要求见 [rework-01-request.md](rework-01-request.md)。

本轮不新增真实凭据或网络授权，不回到 Messages 诊断，不改 C1 目标、配置格式、公共导出或权限语义。

## 范围与证据

- 开始 HEAD `cc6f0fd`，主仓干净；worker 仅修改/新增卡内产品、测试、module_map 和两份报告，没有暂存、提交或推送。跟踪文件 diff 与未跟踪文件合并后符合报告清单。
- 完整阅读报告、证据、新 CredentialStore、ConfigStore、权限模块和新凭据测试，静态核对锁内读改写、权限检查、临时文件创建/关闭/清理、替换前后校验及既有读路径。
- 采信 worker 的最终专项 225 passed / 1 skipped、全量 2814 passed / 3 skipped、Ruff 与实际 Windows ACL 证据；它们是现有用例结果，不覆盖下述遗漏输入。POSIX 分支仍未实机执行，不追认为通过。
- master 仅运行一个有界临时目录复核：无真实配置或凭据、无网络、假 Key、隔离 HOME/配置根，不修改产品或测试。第一次从默认主仓启动，导入新增 CredentialStore 时 ModuleNotFoundError，在用例前停止；随后显式指定任务 worktree，成功进入复核并得到下列失败。没有重跑全量或放宽断言。

## 必须修复

### 1. 显式 null 被当作目标不存在，破坏损坏条目保护

位置：`config_store.py::ConfigStore.update_provider`、`credential_store.py::CredentialStore._write`。

两处均使用 `entries.get(provider_id)`。当磁盘为 `{"providers":{"target":null}}`，调用方以 expected_value/expected_record=None 表示“预期不存在”时，比较错误通过。模型存储构造空记录合并新字段；凭据存储也跳过已有类型检查并构造新记录。两者均返回成功并覆盖原字节。

任务要求区分真正缺失与显式无效值，并拒绝重建结构无效记录。应以键是否存在区分，再校验已有条目的结构；不能把 null、非对象等坏记录当成创建入口。真正缺失仍可创建，正常 stale 冲突、未知字段保留不变。

### 2. 序列化可写出严格读取必然拒绝的 JSON

位置：`config_store.py::serialize_json` 及 `update_provider` 的嵌套输入。

用合法目录字段提交 `models=[{"id":"model","future":{1:"one","1":"two"}}]`。模型未知字段被允许保留，但 json.dumps 会把整数键转成字符串，与已有字符串键发生碰撞。当前保存返回成功；接着 `ConfigStore.read("models")` 因重复键抛 ConfigError，整个目录不可读。

写入入口必须拒绝所有层级的非字符串对象键，并确保输出通过既有严格解析；校验应发生在替换前，失败保留旧文件。不要通过放宽 parse_json 的重复键保护、丢弃未知字段或键强转掩盖。

### 3. 延迟导入没有消除存储层到服务层的反向依赖

位置：`config_store.py::_read_rules`。

ConfigService 已直接依赖 ConfigStore；本次 ConfigStore 为访问 `_text`、`_url`、`_CHANNELS`、`_API_DEFAULTS` 又导入 ConfigService。把导入移到函数内部只推迟了环的触发，并把底层字段校验耦合到服务装配及 Provider 默认参数。

应将确需共享的纯字段规则放在最小的内部低层接缝，两方单向依赖；Provider 默认预算/思考映射和 ConfigService 业务保留原归属。为此允许本轮新增 `_config_validation.py` 并窄改 config_service.py，详见返工范围。不是整体架构重构，不扩展顶层 API。

## master 的最小失败复核

实际命令使用 `PYTHONDONTWRITEBYTECODE=1 uv --directory C:/Users/10601/Desktop/learning/mica-wt/T-037 run --no-sync --offline python -`。一次 TemporaryDirectory 内执行下列三个场景；网络连接/DNS 被禁止，导入前隔离 HOME/配置根并清除凭据环境项：

1. 写假 models JSON，其 target 为 null；调用 update_provider(target, 合法基址与空模型列表, expected_value=None)。
2. 通过 CredentialStore 建立受保护的假凭据文件，在该隔离文件内写 target=null；调用 save_api_key(target, 假 Key, expected_record=None)。
3. 调用 update_provider 新建另一个供应商，模型 future 字段使用整数 1 与字符串 "1" 两个键；返回后严格读取 models。

实际白名单输出：

```json
{"models_null_treated_as_absent":true,"models_original_bytes_preserved":false,"auth_null_treated_as_absent":true,"auth_original_bytes_preserved":false,"real_credentials_read":false,"network_requests":0}
{"save_returned_success":true,"subsequent_strict_read_succeeded":false}
```

最后保护断言失败，exit 1：`AssertionError: invalid target and non-roundtrippable writes must be rejected`。临时目录随上下文清理。该失败是本轮产品输入反例，不是模型渠道、权限测试环境或观察器故障。

## 后续

- 返工沿用 T-037，不新开诊断卡。保留第一轮报告与证据，下一轮写 worker-02.md、validation-02.md。
- 稳定后一次最终专项、全量与 Ruff，平台证据保持原边界。权限实现未变化时不机械重做已完成独立 ACL 探针；现有安全回归照常覆盖。
- 当前不将产品代码合并为已验收交付，不推送。master 仅本地保存本轮交付证据、审查、返工要求和当前状态；通过后继续 C1 目录业务。
