# T-038 worker-01 执行报告

## 任务完成情况总览

C1 第 2 步的模型目录发现与勾选结果保存已实现并完成隔离验证：

- 新增 `src/mica_agent/model_catalog.py`：三内置渠道（DeepSeek、OpenRouter、OpenCode Go）固定目录表与独立解析；自定义供应商仅在调用方提供同源运行期目录 URL 时使用 OpenAI 风格 `data/id` 最小结构。
- 目录请求有界、可取消：每次最多一次 GET、总 deadline 30 秒、零自动重试、不跟随重定向；DeepSeek/OpenRouter 只向已核实的固定目录发送 Bearer Key，Go 使用公开目录、Mica User-Agent 和调用方会话 ID、不发送 Key。
- 解析只映射能明确对应现有配置的 ID、展示名、上下文/输出预算、输入模态和显式思考等级；未知能力保持未知，不按名称、价格或 `supported_parameters` 外推思考能力。
- 勾选保存只更新 `models` 字段：保留既有手工/未知字段、既有选择以及远端本次未返回但仍被选中的模型；显式取消选择可移除；新 Go 模型必须显式提供 `api_choices`；失败、空目录和需要手填的结果不会覆盖连接。
- 保存前以调用方读到的完整供应商记录做锁内比较；发现期间发生编辑、禁用或删除时保存冲突且旧字节不变；不新建替代连接，不改默认模型、凭据或其他供应商。
- 未修改 Provider/Engine/App/CLI/TUI、依赖、既有配置/取消/权限契约、计划、决定、STATUS、任务卡或渠道依据；`docs/module_map.md` 仅登记新增文件的实际职责。

危险操作：无。本轮未提交、未推送、未暂存、未 stash，未读取真实 `~/.mica`、真实 Key 或其他客户端凭据，未访问真实渠道。

本卡完成的是目录业务与勾选保存，**不代表** `/login` 界面、真实渠道登录、账号资格或 C1 整体通过。

## 修改/创建的文件

- `src/mica_agent/model_catalog.py`（新增）：`CatalogTarget`、`CatalogModel`、`CatalogResult` 不可变数据值；`discover` 请求/解析/取消收口；`save_selection` 勾选合并与既有 `ConfigStore.update_provider` 保存。未加入包顶层导出。
- `tests/test_model_catalog.py`（新增）：三渠道手写样本、请求/认证/元数据、空/失败/不完整/手填区别、取消与超时、gzip/分片/过大/坏 JSON/重复 ID、HTTP 401/429/重定向、资源收口、勾选合并、并发冲突及 ConfigService 窄集成；共 62 项。
- `docs/module_map.md`：配置与选择组新增 `model_catalog.py` 与 `tests/test_model_catalog.py` 的实际职责说明。
- `docs/tasks/T-038/worker-01.md`：本报告。
- `docs/tasks/T-038/validation.md`：命令、首次失败、根因和验证证据。

## 内部接口与状态归属

- `CatalogTarget`：调用方显式传入渠道、已确认基址/接口/必要 Key、可选运行期目录 URL 和会话 ID；自定义 `repr` 隐藏 Key、URL 和会话 ID。
- `CatalogModel`：仅包含现有配置字段白名单中的 ID、名称、上下文窗口、输出预算、输入模态、思考开关和思考等级映射；`null` 视为未提供，非法字段只计入 `invalid_metadata`，不写成发现结果。
- `CatalogResult`：固定状态 `ok/incomplete/empty/manual/failed` 与固定错误类别 `authentication/rate_limited/http/redirect/network/timeout/too_large/invalid_response`；失败结果不含远端正文、URL、Key 或本地路径，也不保留原始响应。
- `discover`：每次拥有自己的 HTTPX client、响应和取消观察任务，`finally` 中取消并等待任务；注入的 transport 所有权交给该 client。取消 token 抛 `CancellationRequested`，外部 `task.cancel` 的 `CancelledError` 原样传播。
- `save_selection`：不读取配置或凭据；`expected_provider` 必须为调用前读到的完整记录（禁止 `None` 创建路径）；合并后仅调用 `ConfigStore.update_provider(..., changes={"models": ...})`。网络等待期间不持有存储锁。

## 验证结果

完整命令与首次失败记录见 `docs/tasks/T-038/validation.md`，摘要：

- 环境：`uv sync --python 3.11` 成功，CPython 3.11.15，25 个包；worktree `C:/Users/10601/Desktop/learning/mica-wt/T-038`，分支 `task/T-038-model-catalog`，HEAD `45ed62d`。
- 新测试：`uv run pytest tests/test_model_catalog.py -q -W error`：**62 passed**。
- 专项回归：`uv run pytest tests/test_model_catalog.py tests/test_config_store.py tests/test_config_service.py -q -W error`：**267 passed**（87.59s）。
- 全量：`uv run pytest -q -W error`：**2885 passed, 3 skipped**（294.79s）；3 项跳过为既有平台/条件用例，本卡未新增跳过。
- 静态检查：`uv run ruff check src/mica_agent tests` 通过；`uv run ruff format --check src/mica_agent tests` 为 122 files already formatted；`git diff --check` 通过。
- 关键闭环：隔离供应商记录 → `discover` → 选中模型 → `save_selection` → 现有 `ConfigService.load()` 正确解析；手工/未知字段、既有选择、远端缺席仍选中项、显式取消选择、默认与凭据字节均受保护；并发编辑/禁用/删除后保存冲突且旧字节不变。
- 测试只使用临时目录、假 Key（`TEST_ONLY_*`）、HTTPX transport 替身和实际 asyncio Event；未访问真实渠道、真实模型请求或真实 `~/.mica`。

## 未完成事项或风险

- 未实现 `/login` 菜单、登录/连接维护界面或真实渠道验收；这些属于 T-038 之后的 C1 应用/终端闭环，不在本卡范围。
- OpenRouter 的 `supported_parameters` 只表示参数名，未提供可用 effort 取值集合；本卡保持 `reasoning` 未知，不用适配器默认映射冒充远端发现。后续如需支持，应由渠道明确字段或产品决定补充，不能由本卡猜。
- DeepSeek 基址同时接受官方 `https://api.deepseek.com` 与既有配置示例使用的 `/v1` 兼容别名；目录请求仍固定发送到 `https://api.deepseek.com/models`，不向别名路径发送 Key。
- 新模型条目会写入解析出的显式 `api`（优先 `api_choices`，其次内置渠道表或显式供应商接口）；既有条目的显式或继承接口事实不被刷新改写。该选择便于保存结果不依赖供应商默认值。
- Go 会话 ID 必须由后续调用方提供稳定、无路径/控制字符的值；本卡不新增身份持久化格式。
- 本机为 Windows；取消/HTTPX 代码无平台分支，但未在 POSIX 实机补跑。真实渠道、真实账号、额度与 `/login` UI 未验证。
- 未提交、未推送；工作区改动由 master 验收。

## 建议沉淀

- lessons.md：HTTPX 测试中 `Response(content=AsyncIterable)` 会包一层 `AsyncIteratorByteStream`，内层流的 `aclose` 不一定被直接观察到；要断言产品确实关闭了自定义流，应使用 `Response(stream=...)` 并把“响应关闭/客户端关闭/内层流关闭”分开断言，避免把测试替身行为误判为产品泄漏。
