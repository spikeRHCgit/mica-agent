# 第 1 轮审查 · 2026-10-21

依据：[`T-034.md`](T-034.md)、[`worker-01.md`](worker-01.md)。

## 审查范围与证据

- Git 核对：main，较 origin/main 领先两个既有草案/派发提交，暂存区为空；14 个已跟踪修改文件及新增接口装配测试、worker 报告与汇报清单一致。未发现修改 Engine、适配器、会话/wire、TUI 产品代码或其他范围外文件。
- 审查四个产品文件、全部调用侧/配置/提交完整性测试 diff、README 和源码地图，并通读新增 59 项装配测试的构造、输入及断言；结合 Completions 请求编码核对首个失败的前提。静态阅读不作为产品链路通过证据。
- 采信 worker 已完成的证据：配置 169 passed、提交完整性负向 4 passed，Ruff、格式及 diff 检查通过。相关链路、全量、完整安装态未完成；2761 项全仓收集及 59 项新增测试收集只证明可收集，安装态前 9 项输出不代表整套通过。
- master 用同一项目 `uv run` 的 Python 3.11.15 做独立有界诊断：隔离 HOME/USERPROFILE，清除三项 MICA 连接环境变量，uv 离线且禁止 Python 下载；两个子进程分别执行 `socket.socketpair(socket.AF_INET)`（随后关闭两端）和 `asyncio.run(asyncio.sleep(0))`，各设 8 秒超时，均退出 0 并正常返回。不导入 Mica、不访问真实渠道、不改系统网络或事件循环。
- 因当前 master 环境未复现阻塞，追加一次高风险定向抽验：

  ```text
  uv run pytest tests/test_app.py::test_blank_prompt_is_rejected_before_provider tests/test_app_interfaces.py::test_tool_round_trip_persists_only_current_continuations_and_fresh_snapshot tests/test_app_interfaces.py::test_all_interface_switch_directions_preserve_facts_filter_private_and_switch_back -q -W error -x -o faulthandler_timeout=20
  ```

  实际由 `uv run --directory` 下的 Python 启动 `sys.executable -m pytest` 子进程，并设 150 秒总超时；沿用上述隔离环境，另清除 PYTEST_ADDOPTS。结果为 **1 failed, 3 passed in 4.29s**，退出 1。三项通过是既有空白输入参数；首个 Completions 工具恢复用例失败后停止，后续接口和切换用例未运行。没有重跑完整相关链路、全量或安装态。

## 发现与判断

### 1. 新增 DeepSeek 恢复测试缺少回传所需的 tools 前提

- 失败位置：`tests/test_app_interfaces.py:353-359`。首轮使用真实 ToolExecutor，保存的续接记录及同轮回传均通过；恢复时调用 `assembled_app(config, api, service=service, session_id=app.session_id)` 未传 executor。该 helper 默认 `executor=None`，因此恢复 App 的下一轮请求不携带 tools。
- 该场景的配置默认 `channel="deepseek"`。现有 `providers/openai_compatible.py:321-326` 明确只在 `request.tools` 非空且保存记录含 reasoning_content 时发送该字段。这是已验收的 DeepSeek 边界，不是新工厂漏选渠道。
- 失败断言要求恢复请求包含一次 `saved thought`，实际为 0。根因是测试重建 App 时没有保持其所断言的工具续接前提；本轮没有证据据此判定产品续接丢失。不得修改适配器、强行给无工具请求补字段或降低回传断言掩盖问题。
- 同文件的明确渠道、纯思考空槽、同接口换供应商/模型等场景也有无 executor 却要求 DeepSeek 私有回传的写法，下一轮需逐项核对相同前提；这些场景本轮尚未执行，不能记成已复现的失败。
- 建议保留工具续接场景的强断言，在需要验证 DeepSeek 回传的 App 构造及恢复中装配真实隔离 ToolExecutor；恢复仅提供工具定义，不应触发历史工具重放。无工具场景如需保留，应明确断言既有不发送行为，不把它混作带 tools 的回放证据。

### 2. 自动化验收证据仍不完整

- worker 如实报告 Windows socketpair/asyncio 当时阻塞，并区分实测栈和环境限制推断，没有把收集或部分安装输出当通过；交回是正确的。
- 当前 master 诊断和实际 pytest 已能运行，故不能把 worker 当时的现象表述为当前所有执行环境仍无法运行，也不能据此否定当时记录。两边执行环境差异或暂态原因尚未确认；本轮未取得 worker 环境恢复正常的证据。
- 现有实现从静态 diff 看遵循固定工厂、渠道透传、共享能力计算、来源漂移保护、同次历史读取和本轮续接 sink 接入方案，未发现其他已确认的产品阻断缺陷。但新增测试存在真实失败，且大部分异步装配、原生命周期回归及安装态没有完成证据，不能验收。

## 结论

**第一轮未通过，T-034 不验收。** 卡片与 STATUS 登记阻塞中，等待用户确认安排下一轮返工。产品实现和测试保持未提交，不将未完成证据补记为通过，不推送中间受阻记录。

建议下一轮沿用 T-034 原写入范围：修正 DeepSeek 测试装配前提，在能正常初始化 socketpair/asyncio 的执行环境完成原卡专项、相关链路、最终全量及完整安装态证据；如仍阻塞，保留有界最小诊断及环境信息交回，不改系统网络、替换事件循环或跳过测试。确认前不创建或派发返工要求；下一轮汇报应为 `worker-02.md`，不改第一轮报告。

用户真实渠道验收、B 第 6 步整体及 Phase 6 均未完成。
