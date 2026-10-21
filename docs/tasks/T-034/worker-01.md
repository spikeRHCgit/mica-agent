# T-034 worker-01 汇报

日期：2026-10-21。角色：aTa worker。按 2026-10-20 已审查方案施工。

## 交付状态

**代码与测试编写已完成，但本卡自动化实现验收未完成，不可据此验收 T-034、B 或 Phase 6。**

同步配置测试 169 项、成功提交完整性负向测试 4 项通过；静态检查通过。三接口实际装配、相关链路和全量运行被当前执行环境的 Windows asyncio 本地 socketpair 阻塞。安装态已完成离线构建并观察到前 9 项通过，整套安装态运行也未结束；没有将部分输出记作整套通过。新增 59 项接口装配测试仅完成收集，尚无运行通过证据。

**用户真实渠道验收未进行**：三类接口各一条真实工具对话及一次跨接口切换，仍由用户完成；本轮未读取真实凭据、真实 `~/.mica`，未访问真实渠道。

## 实现与写入范围

| 路径 | 变更 |
|---|---|
| `src/mica_agent/app.py` | ProviderFactory 接收完整 ConnectionBinding；提供唯一默认三接口工厂；只按明确 Completions 渠道启用 DeepSeek/OpenRouter 续接。启动/切换共用能力计算，切换候选的能力、设置、Engine 在保存选择前准备；保存后安装并关闭旧资源。RunBinding 冻结 provider_id/api；每轮一次统一历史快照；请求传入来源与历史续接；接入本轮续接 sink，并拒绝缺失、重复或 assistant 数不匹配的成功提交。 |
| `src/mica_agent/cli.py` | 使用 App 默认工厂；模型标签取实际解析连接，修正保存模型显示为 unconfigured；更新附近说明。 |
| `src/mica_agent/config_contracts.py` | ConnectionBinding 末尾添加 `channel="custom"`，保留五位置参数和脱敏 repr。 |
| `src/mica_agent/config_service.py` | 保存连接透传已校验渠道；提取 effective_model_options，沿用原接口默认表、偏好及来源；for_run 保留签名和五项结构，继续在提交边界检查显式容量冲突，新增 API/渠道漂移拒绝。临时连接保持 custom/Completions。 |
| `tests/test_app_interfaces.py`（新增） | 59 项参数化装配验证。替换 HTTP 客户端的传输边界，保留默认工厂、真实适配器、Engine、SessionService 和 JSONL；复用既有 SSE 样本及辅助，不修改共享辅助。工具处理器实际读取隔离工作区事实文件，使用真实 ToolExecutor 执行。 |
| `tests/test_app.py` | 迁移工厂替身；新增成功提交续接 sink 缺失、重复、长度错位及非法记录的拒绝测试。 |
| `tests/test_cli_startup.py` | 迁移工厂签名及完整绑定输入断言，保留原启动、优先级和诊断保护。 |
| `tests/test_config_contracts.py` | 五位置参数、渠道缺省和脱敏 repr 保护。 |
| `tests/test_config_service.py` | 渠道透传、临时 custom 身份、共享计算不提前做容量校验；原接口重载测试改为拒绝漂移、无 current 查询实际接口、偏好不变；补渠道/URL 漂移保护。 |
| `tests/test_cli_interactive.py`、`tests/test_cli_tool_lifecycle.py` | 仅工厂签名迁移。 |
| `tests/test_tui_main_buffer_cli.py`、`tests/test_tui_main_buffer_screen.py` | 仅工厂签名、完整绑定输入及本地构造 helper 迁移；保留原测试、参数和行为断言。 |
| `README.md` | 同步三接口、已有 /model、渠道、来源冻结、续接/v6、未交付能力及用户真实验收边界。 |
| `docs/module_map.md` | 同步本卡相关职责及新测试条目。 |
| `docs/tasks/T-034/worker-01.md` | 本报告。 |

未修改 Engine、适配器、续接/Provider/会话/检查点契约或 wire、SessionService、存储、工具/权限/取消实现、TUI 产品代码、顶层导出、依赖、共享 SSE 辅助、STATUS、计划、决定或任务卡。未引入双工厂签名、渠道探测、自动剥离续接重试或另一套历史过滤。

原测试未删除、未减少参数、未新增 skip/xfail。原先“磁盘接口改动继续重载”的断言按本卡要求替换为安全拒绝，仍保留无 current 时查询磁盘实际能力与偏好不变的保护。

## 验收覆盖与证据边界

新增装配用例包含：三接口工具往返及重新读取/下一轮续接；六个有向跨接口切换与切回；同接口跨供应商/模型；CLI new/continue/resume；默认主缓冲区模型标签和思考提示；明确渠道开关；startup/switch/prepare 的 minimal 默认/显式映射；API/渠道/URL/Key 轮间漂移；工具轮内能力/来源修改及下一轮刷新；纯思考空 assistant；三接口取消、可恢复失败、部分批次、未 Finish 流及恢复；原子保存失败；Messages 服务端签名拒绝且不重试。

这些是**已编写并成功收集的覆盖**，不是通过结论。真实默认工厂的请求编码、跨接口/来源过滤、实际续接持久化和中断恢复，以及既有异步资源/门禁回归，均须在可运行 asyncio 的环境执行后才能判断。同步配置和 sink 负向测试的通过，不能替代这些链路。

## 验证环境

- Windows / Python 3.11.15 / pytest 9.1.1，工具均经 `uv run`。
- HOME 与 USERPROFILE 使用隔离目录；配置、会话、测试工作区均为临时目录；假 Key 的读取仍通过实际文件 owner/DACL 保护检查，未替换凭据安全边界。
- 清空 MICA_API_URL、MICA_API_KEY、MICA_MODEL；HTTP 场景使用模拟传输，安装态沿用既有本地回环测试，不访问真实渠道。
- UV_OFFLINE=true。将已有缓存中的 setuptools 索引及对应包内容复制至可写临时 uv 缓存，供既有安装态套件离线构建使用；未新增依赖或修改锁文件。

最终验证使用的 PowerShell 环境前置：

```powershell
$env:HOME = 'C:\Users\10601\AppData\Local\Temp\mica-t034\home'
$env:USERPROFILE = $env:HOME
$env:TEMP = 'C:\Users\10601\AppData\Local\Temp\mica-t034\tmp'
$env:TMP = $env:TEMP
$env:UV_CACHE_DIR = 'C:\Users\10601\AppData\Local\Temp\mica-t034\uv-cache'
$env:UV_OFFLINE = 'true'
$env:MICA_API_URL = ''
$env:MICA_API_KEY = ''
$env:MICA_MODEL = ''
```

## 实际命令与结果

| 命令 | 实际结果 |
|---|---|
| `uv run pytest tests/test_config_contracts.py tests/test_config_service.py -q -W error -ra` | **169 passed in 416.85s**，退出 0。 |
| `uv run pytest tests/test_app.py::test_completed_turn_rejects_missing_duplicate_or_misaligned_continuation_sink -q -W error` | **4 passed in 0.42s**。 |
| `uv run pytest tests/test_app_interfaces.py tests/test_app.py tests/test_cli_startup.py -q -W error` | 已启动，未完成。首个接口装配用例无法越过 asyncio 事件循环初始化；有界监督停止，监督脚本的子进程收口亦超时退出 1；无 pytest 通过结论。 |
| `uv run pytest tests/test_app_interfaces.py tests/test_app.py tests/test_cli.py tests/test_cli_startup.py tests/test_cli_interactive.py tests/test_cli_tool_lifecycle.py tests/test_tui_main_buffer_cli.py tests/test_tui_main_buffer_screen.py tests/test_config_contracts.py tests/test_config_service.py tests/test_session_integration.py tests/test_tool_session_integration.py tests/test_reasoning_integration.py -q -W error` | 已直接启动；无完成输出，阻塞后通过执行工具中断，工具退出 1。**未通过验收**。 |
| 上一行相关链路命令追加 `--collect-only` | **525 tests collected in 2.37s**，退出 0；仅证明收集。 |
| `uv run pytest -q -W error` | 已直接启动；无完成输出，阻塞后通过执行工具中断，工具退出 1。**最终全量未完成**。 |
| `uv run pytest tests/test_distribution.py -q -W error -x` | 既有离线构建成功，观察到前 **9** 项通过输出（wheel/sdist 内容、依赖、TUI 导入、四组裸安装入口契约、无副作用导入）；后续安装态 CLI 未返回，执行工具中断，退出 1。**安装态整套未完成**。未改测试来绕过阻塞。 |
| `uv run pytest --collect-only -q -W error` | **2761 tests collected in 0.95s**，退出 0。 |
| `uv run pytest tests/test_app_interfaces.py --collect-only -q -W error` | **59 tests collected in 0.25s**，退出 0。 |
| `uv run ruff check src/mica_agent tests` | **All checks passed!** |
| `uv run ruff format --check src/mica_agent tests` | **117 files already formatted**。 |
| `git diff --check` | 退出 0，无输出。 |

开发期另执行了 `uv run pytest tests/test_app.py tests/test_cli_startup.py -q -W error` 和同范围 `-v -W error -x`。二者在原有第一个 asyncio 场景挂起，中断；不记作测试通过。首次采用沙箱重定向的临时目录时，uv 查询解释器缓存反复提示 `Failed to persist temporary file ... 拒绝访问`，还未进入 pytest；改用上面的明确可写目录后该错误消失。开发期 Ruff 曾提示 import 排序、未用 import、可合并条件及格式问题，均已修正，最终检查结果见表。

平台跳过：已完整执行的 169+4 项无 skip/xfail。本轮未新增平台跳过。全量及安装态未完成，未得到全仓平台跳过统计，不能声称其他平台或 Windows 进程/终端场景已验收。

## 范围外阻塞定位

用于定位的实际命令：

```powershell
uv run pytest tests/test_app.py::test_blank_prompt_is_rejected_before_provider -v -W error -o faulthandler_timeout=15
```

在尚未调用 Provider 的原有空白输入测试中，15 秒 faulthandler 栈显示：

```text
asyncio.run
  -> asyncio.new_event_loop
  -> windows_events.ProactorEventLoop.__init__
  -> proactor_events._make_self_pipe
  -> socket._fallback_socketpair
  -> socket.accept
```

还以 `uv run python -c` 执行两个独立子进程：分别调用 `socket.socketpair(socket.AF_INET)` 与 `socket.socketpair(socket.AF_INET6)`，使用 `subprocess.run(..., timeout=8)` 设置边界；两者均输出 `TIMEOUT after 8 seconds before socketpair returned`。该诊断不加载 Mica、不读取用户配置、不请求远端，证明当前环境本地 socketpair 无法正常返回；并非由新工厂签名或续接请求触发。精确归因为当前执行环境限制仍属推断，未改动系统网络配置或 Python 运行时。

安装态的阻塞发生在前九个同步用例之后的 CLI 场景；未取得安装态子进程栈，不能将其独立根因说成已验证，但当前 socketpair 阻塞同样会阻止该环境初始化 asyncio。

未替换 socketpair、事件循环或业务执行路径来制造通过，也未申请越权修改运行环境。按卡片范围将此问题交回 master。下一步是在本地 socketpair/asyncio 可正常工作的授权环境，重跑本表三组规定运行及完整安装态；若出现产品或测试失败，先定位，在本卡范围内修复后补受影响验证。当前新增集成测试尚未经运行，仍可能需要修正，不能视为已通过审查。

## Git 与待交回事项

- 起始工作树干净，`main...origin/main [ahead 2]`；结束仍为同一分支且 ahead 2。无建分支、暂存、提交、推送、stash 或还原；`git diff --cached --name-only` 无输出。全部交付为未暂存修改或新文件。
- master 需要处理或安排上述执行环境阻塞，并取得完整自动化验收证据；本报告不改 STATUS 或阶段结论。
- 用户真实渠道三接口工具对话与跨接口切换尚未执行；不读真实凭据、不代做真实渠道验收的本轮约束保持。
