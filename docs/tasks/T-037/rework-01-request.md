# T-037 返工 1 · 2026-10-25

依据：[原任务卡](T-037.md)、[第一轮审查及失败复核](review-01.md)。保留已完成的存储与安全写入主体，只修下面三项，不重新做可行性调研。用户此前要求推进产品交付，本轮属于既有实现的正确性收口，不新增功能目标或真实渠道权限。

## 必须完成

1. **区分目标缺失与显式无效值。** ConfigStore.update_provider 与 CredentialStore 保存路径不能以 get() 返回 None 判断不存在。对已存在的 null/非对象条目在写入前拒绝，原字节保持；真正缺失可创建，正常 stale 更新冲突、其他记录及未知字段保留。检查相应删除入口不因同类问题破坏记录，但不要扩充认证类型或删除业务。
2. **严格 JSON 输入/输出闭环。** serialize_json 在替换前拒绝任意层级非字符串对象键；生成的 bytes 必须能被既有 parse_json 严格读取，不能成功写出重复键或其他读回失败的文件。模型未知字段中的 `{1: "one", "1": "two"}` 必须拒绝；不要通过强转键、剥离未知字段、放宽重复键/大小校验修补。保持有界、明确的 ConfigError，不回显参数内容或路径。
3. **移除 ConfigStore → ConfigService 的反向导入。** 删除 _read_rules 及这条反向依赖，将两方确需共享的文本/URL、渠道/接口标识规则放入最小内部低层文件 `_config_validation.py`，ConfigStore 和 ConfigService 单向依赖它。服务的 Provider 默认预算、思考映射、来源解析、连接冻结和权限行为不搬进校验模块；不复制出两套漂移规则、不扩大顶层公共 API。允许通过导入别名保留现有私有规则名，减少无关 diff。

## 本轮范围

- 沿用原卡已有产品与测试实现，不覆盖或丢弃第一轮改动；开始时核对 master 本轮文档提交后的 HEAD、分支及未提交的产品 diff。
- 本轮主要修改 `src/mica_agent/config_store.py`、`src/mica_agent/credential_store.py`、对应两份测试；额外允许新增 `src/mica_agent/_config_validation.py`，窄改 `src/mica_agent/config_service.py` 以共享纯规则，必要时调整 `tests/test_config_service.py` 的直接保护，并同步 `docs/module_map.md` 的该辅助职责。
- 权限实现保留；只有直接回归证明必须调整时才在原允许范围内最小修改，不另起一轮 ACL 重构或诊断。
- 本轮报告仅写 `docs/tasks/T-037/worker-02.md`，证据写 `docs/tasks/T-037/validation-02.md`。worker-01.md、validation.md、review-01.md、任务卡、本返工要求、STATUS 与计划冻结，不由 worker 修改。
- 禁止真实配置、真实凭据、外部渠道、Provider/Engine、App/CLI/TUI、依赖、配置格式或公共导出变更。没有 T-036 余额使用权，不新增模型请求。

## 验证与交付

- 先补失败回归，在本轮修复前实现上记录 null 目标与非字符串嵌套键的实际失败，再改实现并通过；记录首次失败与根因，不以全量旧用例通过替代反例保护。
- 验证 models/auth 对显式 null、非对象目标均拒绝且旧字节不变；真正缺失仍创建，stale 仍冲突；嵌套字典/数组中的非字符串键拒绝，合法未知字段保留，成功写入结果可严格读回。
- 窄核对现有读取校验语义保持，包括 URL、三接口/渠道、模型能力默认/显式声明、非法输入安全错误与临时来源保护。静态检查依赖方向，ConfigStore 不再导入 ConfigService。
- 最终专项：`uv run pytest tests/test_config_store.py tests/test_credential_store.py tests/test_credential_permissions.py tests/test_config_service.py -q -W error`；稳定后集中一次最终全量 `uv run pytest -q -W error`、`uv run ruff check src/mica_agent tests`、`uv run ruff format --check src/mica_agent tests`。
- `git diff --check` 通过；不改依赖/分发，不构建。第一轮实际 ACL 证据可引用，现有权限回归不得跳过、放宽或删除；POSIX 本机未执行的限制保持如实披露。
- 不暂存、提交、推送、stash 或还原他人文件，不启用内置 subagent。交回本轮变更、失败回归前后结果与最终验证，由 master 收口后继续 C1 目录实现，不再回到泛泛探测。
