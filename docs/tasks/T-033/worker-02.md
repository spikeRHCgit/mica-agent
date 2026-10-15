# 第 2 轮汇报 · 2026-10-15

依据：[`T-033.md`](T-033.md)、[`rework-01-request.md`](rework-01-request.md)。

- 结果：完成 —— 三接口保存连接与按实际接口计算的每轮有效能力已实现；专项、相关链路、最终全量及静态验证通过。
- 改动：`src/mica_agent/config_service.py`、`src/mica_agent/providers/anthropic_messages.py`、`tests/test_config_service.py`、`tests/test_app.py`、`docs/module_map.md`、本报告。
- 验证：Messages 专项 171 passed；ConfigService 最终专项 153 passed；ConfigService + App 相关链路 221 passed；最终全量（含安装态）2687 passed、2 项既有平台条件跳过；Ruff、格式及 diff 检查通过。
- 偏离与疑点：无。
- 建议：无。

### 实现与写入范围

- `src/mica_agent/config_service.py`：用一张固定 `_API_DEFAULTS` 表直接引用三适配器公开预算常量与原只读思考映射，协议合法性也只检查该表的键。保存目录中已校验的三接口模型只要供应商启用且凭据可用就建立绑定。`for_run` 按本次解析连接的 `model.api` 选默认预算和映射，继续保留显式预算、显式非空映射、容量冲突、思考降级和偏好来源边界；公共签名与返回结构未变。
- `src/mica_agent/providers/anthropic_messages.py`：仅新增 `DEFAULT_MAX_TOKENS = 4096`，并将原请求预算表达式中的 4096 替换为该常量；`or` 语义保持原样。
- `tests/test_config_service.py`：沿用隔离目录和受保护假凭据装配；添加三接口参数化 fixture 与独立字面量 expected，扩展既有重复 ID、混合未知档位、容量冲突与损坏文件测试，补齐选择、完整能力、来源、降级、不可变性及安全负向证据。已有 helper 的签名与默认行为保持原样，未修改共享测试装配。
- `tests/test_app.py`：仅授权的两个 `anthropic-messages` 样本改为 `unknown-protocol`；没有其他修改。
- `docs/module_map.md`：仅更新 ConfigService 与对应专项测试的职责说明。
- `docs/tasks/T-033/worker-02.md`：本轮汇报。第一轮报告、任务卡、返工要求、审查、STATUS 和计划未修改。

### 明细

- [x] Messages 仅提取公开预算常量并替换字面量，保留 or 语义及默认/显式请求预算：精确 diff 与 171 项 Messages 专项证明。
- [x] 三种供应商默认与九种模型接口覆盖，绑定保留 api、选择、地址和来源：`test_saved_interfaces_inherit_or_override_without_rewriting_address` 对每种供应商接口验证继承及三种覆盖，并比较基址、选择和 saved directory 来源；有效映射从覆盖后的实际接口取得。
- [x] 三接口 disabled、缺 Key、未知接口、重复模型及合法邻居保护：`test_unavailable_saved_interface_cannot_be_selected` 覆盖四种不可用原因，拒绝运行和保存默认；未知供应商接口不被模型覆盖绕过。既有重复 ID 测试参数化到三接口，保留全部拒绝和合法邻居断言。
- [x] Responses/Messages 选择、保存默认、不回退、环境优先级及临时身份：`test_saved_interface_selection_precedence_and_default_save` 覆盖三接口的 explicit/saved/global default/workspace default、保存全局默认且保留无关字段、失效 explicit/saved 在环境连接可用时仍不回退；原临时身份测试保留，新增 direct/environment 每轮能力回归。
- [x] 三接口缺失/空映射的完整默认映射、预算及各思考档位：`test_interface_default_mapping_budget_and_all_efforts` 逐一比较独立列出的完整协议值 expected、4096 预算与七档偏好。Messages minimal 为 OFF，其余五档可用；Completions/Responses minimal 可用。解析能力内的显式映射仍为空，sources 为 adapter default，不回填模型文件。
- [x] 非空显式映射不扩充、自定义目标、Messages minimal 到 low 及偏好降级：`test_explicit_mapping_is_complete_without_default_neighbors` 验证完整相等、显式来源、自定义目标、Messages minimal 到 low 和未列出 low 降为 OFF；原混合未知档位测试扩展到三接口，合法邻居完整保留。
- [x] reasoning 缺失/false 为空映射与 OFF，偏好、来源、文件字节不变：`test_nonreasoning_model_preserves_preference_but_has_no_effective_map` 同时覆盖默认/显式映射；默认映射与非空显式映射测试也覆盖降级后的偏好和来源保留、全局/工作区配置字节不变。
- [x] 非字典/非法目标/未知档位回退及合法邻居、安全诊断：`test_malformed_mapping_uses_actual_interface_defaults_without_echo` 覆盖字符串、列表、null、空/空白/控制字符/bool/整数/null 目标及仅未知档位；非法已知目标即使有合法邻居也整份回退实际接口默认。snapshot/selection/binding 的诊断和 repr 不回显输入哨兵。真实隔离 ACL 失败与工作区假凭据忽略由 `test_unprotected_saved_credentials_do_not_fall_back_to_workspace` 覆盖。
- [x] 显式预算、来源、正整数/拒绝 bool、容量冲突及只读保证：三接口逐项验证 1、17、300000 的显式预算及非法预算回退，默认上下文仍为 200000；非法容量回退、默认预算在显式容量 4095/4096/4097 的冲突边界，以及原显式预算相等冲突测试全部通过。默认映射、显式能力映射和来源写入均触发 TypeError，原 settings 来源不可变断言保留。
- [x] 两处 App 授权样本及 module_map 同步，未扩展 App/CLI 装配：精确 diff 仅替换两个协议字符串，测试名称、参数、装配与原断言均保留；相关链路 221 项通过。源码地图仅更新 ConfigService 与专项测试两条职责。
- [x] 专项、相关链路、最终全量（含安装态）、静态验证和 diff 审阅：以下命令均取得最终版本有效证据；通读全部已跟踪 diff 和本报告，修改范围恰为五个授权文件及本报告。第一轮报告和 master 管理文件保持不变。

### 已完成验证与诊断记录

- `uv run pytest tests/test_anthropic_messages_provider.py -q -W error` → `171 passed in 1.84s`。测试文件未修改，默认 4096 与显式 17 的原有请求断言保留。
- `uv run pytest tests/test_config_service.py -q -W error` 最终专项 → `153 passed in 398.05s (0:06:38)`，无跳过。
- `uv run pytest tests/test_config_service.py tests/test_app.py -q -W error` → `221 passed in 451.50s (0:07:31)`，无跳过。
- `uv run pytest -q -W error` → `2687 passed, 2 skipped in 741.23s (0:12:21)`，退出码 0；完整执行既有 `tests/test_distribution.py` 的构建/安装态回归，未对测试做范围筛选。仅通过环境 `PYTEST_ADDOPTS=-rs` 增加跳过原因显示，不改变执行范围或验收级别。
- 配置专项首次运行 → `3 failed, 150 passed in 395.30s (0:06:35)`。三项失败均为新增继承/覆盖测试的对象身份断言受循环中 `for_run` 重载目录影响；修正测试装配，在调用重载前固定同一快照的解析结果，保留对象身份及全部能力断言。产品实现未因这些失败改变；最终专项、相关链路与全量全部通过。
- `uv run pytest tests/test_config_service.py -q -W error -k saved_interfaces_inherit_or_override` → `3 passed, 150 deselected in 17.91s`。deselected 来自本次定向筛选，正式专项、相关链路与全量不使用该筛选。
- 首次 Ruff 检查发现新增别名导入需要按仓库规则拆分，已仅修正 ConfigService 导入排序；随后 `uv run ruff check src/mica_agent tests` → `All checks passed!`，`uv run ruff format --check src/mica_agent tests` → `116 files already formatted`。
- 最终静态验证：`uv run ruff check src/mica_agent tests` → `All checks passed!`；`uv run ruff format --check src/mica_agent tests` → `116 files already formatted`；`git diff --check` → 退出码 0。Git 提示 Messages 文件下一次被 Git 写入时 LF 将转换为 CRLF，该提示未导致检查失败；本轮未执行 Git 写入。

### 平台跳过与未执行项目

- `tests/test_tool_presentation.py:273`：创建隔离目录符号链接时触发 WinError 1314，当前进程缺少所需权限，沿用原有跳过条件。
- `tests/test_workspace_paths.py:69`：同为创建隔离目录符号链接时触发 WinError 1314，沿用原有跳过条件。
- 没有新增 skip/xfail、删除测试参数或放宽原断言。定向复验中的 150 deselected 仅是已明确列出的筛选命令结果，正式专项、相关链路和全量均无该筛选。
- 无依赖或打包规则变更，未额外单独运行构建；全量中的既有分发构建与安装态验证正常执行。
- 未运行真实渠道、真实终端验收。本轮不宣称 App/CLI 已按新接口选取 Provider，不宣称 B 第 6 步整体或 B 的真实渠道验收完成。

### 环境与交付边界

初始工作区干净，位于 `main...origin/main [ahead 4]`。测试进程使用系统临时目录中的独立 HOME/USERPROFILE，清除三项 MICA 连接环境变量；uv 设为离线并禁用 Python 下载，复用已有缓存。新增配置测试使用 pytest 临时目录、显式隔离 environment 及真实 ACL 保护的假凭据；凭据拒绝测试仅调整隔离测试文件权限并在 finally 恢复。

本轮未读取真实凭据或真实 `~/.mica`，未访问真实渠道；Messages 专项使用既有模拟传输，安装态链路使用既有本地回环服务。未执行建分支、切换分支、暂存、提交、推送、stash 或还原。最终仍位于 main，沿用开工前的 ahead 4 状态；没有依赖、公共导出、配置契约/存储、会话、Engine、App/CLI/TUI 或其他适配器改动。

本地配置可用表示配置已通过解析并形成可选连接，不表示服务端接受所有自定义思考目标；Messages 专属目标限制仍由既有适配器负责，未放宽。本轮完成配置层及自动化验证，App/CLI 装配、B 第 6 步整体与真实渠道验收仍待后续卡片和用户验收。
