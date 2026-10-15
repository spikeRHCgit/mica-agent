# 第 2 轮审查 · 2026-10-15

依据：[`T-033.md`](T-033.md)、[`rework-01-request.md`](rework-01-request.md)、[`worker-02.md`](worker-02.md)。

## 审查范围与证据

- Git 核对：main，较 origin/main 领先四个既有任务文档提交；暂存区为空。五个已跟踪改动文件及新增本轮报告与汇报一致，未发现范围外修改。
- 逐项审查 ConfigService、Messages 两处例外、两项 App 输入迁移、ConfigService 全部测试 diff 和源码地图。结合 `for_run`、选择流程及临时连接实现核对接口默认值的实际来源和不回退边界。
- 采信 worker 本轮最终机械证据：Messages 专项 171 passed，ConfigService 专项 153 passed，配置/App 相关链路 221 passed，全量（含安装态）2687 passed、2 项既有平台条件跳过；Ruff、格式及 diff 检查通过。命令、首次失败定位和最终结果详见本轮汇报，不将先前失败抹去。
- 本卡标为高风险，master 另做一次定向抽验：三接口继承/覆盖、缺失/空映射及全部偏好、显式映射、磁盘接口重载、Messages 原预算行为和两项 App 负向保护。抽验使用独立临时 HOME/USERPROFILE，清除三项 MICA 连接环境变量，uv 离线且禁用 Python 下载：

  ```text
  uv run pytest tests/test_config_service.py tests/test_anthropic_messages_provider.py tests/test_app.py -q -W error -k 'saved_interfaces_inherit_or_override or interface_default_mapping_budget_and_all_efforts or explicit_mapping_is_complete_without_default_neighbors or for_run_reloads_actual_model_interface or request_literal_endpoint_auth_system_schema_budget or default_effort_mapping_and_minimal_rejection or model_choices_filter_sort_and_refresh_flags or failed_switch_preserves_connection_selection_and_file'
  ```

  实际使用 `uv run --directory` 指定 Mica 工作目录；结果为 `38 passed, 354 deselected in 60.82s`。筛选仅用于 master 抽验，不替代或缩小 worker 已执行的正式专项、相关链路和全量。
- master 未重跑全量、安装态或构建，未运行真实渠道或真实终端验收。两项全量跳过仍是 Windows 创建隔离符号链接触发 WinError 1314，位置和原因见汇报；没有新增 skip/xfail。

## 发现与判断

- 固定 `_API_DEFAULTS` 是唯一接口默认值表，同时用于接口合法性判断；三项直接引用各适配器公开预算及只读思考映射，不复制默认值、不实例化 Provider，也未引入注册框架。
- 保存连接移除仅 Completions 的绑定限制，仍经过供应商/模型接口、启用状态及受保护凭据校验；模型覆盖后的实际接口保留在连接中。选择优先级、不回退、临时 Completions 身份和连接地址/密钥变更保护未改。
- `for_run` 按本次解析出的 `connection.model.api` 取默认值，显式预算与非空映射优先；保持显式容量冲突保护、无思考/偏好不支持时降为 OFF、保存偏好和 sources 不变。不回填默认映射，不新增 Messages 专属目标校验，也未放宽既有适配器限制。
- Messages diff 精确限制为公开 4096 常量定义和原表达式引用，保留 `or` 语义；没有其他协议或资源行为变动。App diff 仅两处输入从 `anthropic-messages` 改为 `unknown-protocol`，全部原断言、参数与装配保留。
- 测试 expected 独立列出三接口协议值；三接口继承/九种覆盖、选择与安全负向、完整默认/显式映射、降级、来源、不可变性及预算边界均有行为证据。既有混合未知档位、重复 ID 和容量冲突保护扩为三接口，没有以删除原保护换取通过。
- 首次新增测试的对象身份失败已按同一快照固定解析结果修正，仍保留身份断言，符合 ConfigService 重载目录的现有行为；不是吞错或放宽断言。首次导入排序问题已在授权文件内解决。
- 本地连接 ready 仅代表配置可用。App/CLI 尚未按三接口选择 Provider 或完成续接装配，磁盘接口变化后的产品装配一致性也必须在后续 App/CLI 卡中核对；本次配置层抽验不作为产品链路或真实渠道完成证据。

## 结论

第二轮验收通过，T-033 完成。卡片、计划中 B 第 6 步的配置接口与能力子项及 STATUS 同步收口；没有需继续返工的缺陷或待处理建议。

本卡仅交付配置层。下一步为既定的 App/CLI 装配及集成验收，仍需单独编卡并经用户审查后派发；B 第 6 步整体、B 的真实渠道工具对话与跨接口切换验收均未完成。
