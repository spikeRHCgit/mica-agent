# T-034 worker-02 · 第二轮返工汇报

日期：2026-10-21。依据：T-034 原卡、rework-01-request.md、review-01.md。

## 状态

**已修正 DeepSeek 测试装配前提；本轮仍因执行环境阻塞交回，返工与自动化实现验收未完成。**

本轮独立有界检查中，socketpair 与 asyncio 均超时，实际栈停在标准库 socket.accept。按返工要求未继续启动会依赖该前提的开发专项、相关链路、最终全量或安装态。修正后的接口文件成功收集 64 项，Ruff、格式和 diff 检查通过；收集和静态检查不代表行为测试通过。

用户真实渠道三接口各一条工具对话及一次跨接口切换仍未进行。不据本轮结果标记 T-034、B 第 6 步整体、B 或 Phase 6 完成。

## 本轮修改

仅写入以下两个仓库路径：

| 路径 | 本轮变更 |
|---|---|
| `tests/test_app_interfaces.py` | 修正四类装配场景，保留原测试和接口/渠道参数；渠道用例增加有/无工具两个维度，59 项增至 64 项。 |
| `docs/tasks/T-034/worker-02.md` | 本轮报告。 |

未改产品文件、适配器、Engine、会话/续接/wire、存储、工具/权限/取消、TUI、依赖或共享 SSE 辅助；未修改前轮报告、审查、返工要求、任务卡、STATUS、计划或决定。

### 根因与修复

静态核对 `providers/openai_compatible.py` 的现有请求编码：DeepSeek 仅在请求 tools 非空且对应保存记录含 reasoning_content 时回传该字段。review-01 记录的恢复用例丢失 ToolExecutor，断言的前提与实际请求不一致；该次 `1 failed, 3 passed` 是 **master 的首轮抽验**，不是本轮 worker 自己复现的结果。

本轮修正如下：

- `test_tool_round_trip_persists_only_current_continuations_and_fresh_snapshot`：恢复 App 时显式重新装配现有隔离 ToolExecutor，沿用同一执行记录列表；保留只回传一次、逐 assistant 续接数、本轮与历史不重复、真实工具事实以及 `executed == ["call-one"]`，补充全部请求均有 tools 的断言。
- `test_explicit_completions_channel_controls_persisted_replay`：五个渠道全部保留，并参数化 with_tools。带 tools 时 DeepSeek/OpenRouter 保留精确一次正向回传；无 tools 时明确断言 DeepSeek 不发送，OpenRouter 仍回传；custom、opencode-go、chatgpt-subscription 均不误启用。记录保存与回传分别断言，两个分支均确认工具定义是否存在，且无工具实际执行。未以负向断言取代正向覆盖。
- `test_pure_reasoning_empty_assistant_slot_survives_round_trip`：三接口均显式装配隔离工具定义，保留纯思考空正文、续接槽位及下一轮一次回传的强断言；补充请求含 tools、没有工具执行。
- `test_same_interface_other_provider_or_model_filters_and_return_rechecks`：跨供应商/模型及回切均保留同一真实隔离 ToolExecutor，保留来源过滤、回切一次回传、原 Turn 不变；补充所有请求含 tools 且无工具执行。

一并核对同文件其他场景：六个有向接口切换已有 ToolExecutor；中断恢复的初始与恢复 App 均已有 ToolExecutor；CLI new/continue/resume 及默认前端通过实际 CLI 装配工具注册表。能力展示、来源漂移等未要求 DeepSeek 回传的无工具场景保持原样。未改变 assembled_app 的 executor 缺省行为，也未改适配器的 request.tools 条件。

上述修正使用现有实际默认工厂、适配器、Engine、SessionService、JSONL 与模拟 HTTP 边界；工具仍经真实 ToolExecutor 读取隔离事实文件。未注入已选好的适配器、伪造历史工具结果、剥离续接重试、删除用例、减少参数或新增 skip/xfail。**因运行环境阻塞，不能宣称这些修正后的行为已通过。**

## 本轮环境与最小诊断

实测身份：

- Windows / `sys.platform == "win32"`。
- Python `3.11.15 (main, Jun 2 2026, 22:29:49) [MSC v.1944 64 bit (AMD64)]`；解释器为仓库 `.venv/Scripts/python.exe`。
- uv `0.11.19 (7b2cff1c3 2026-06-03 x86_64-pc-windows-msvc)`，入口 `C:/Users/10601/AppData/Local/hermes/bin/uv.exe`。
- PowerShell `7.6.6`。

所有 Python、pytest、Ruff 均经 uv run；HOME/USERPROFILE、TEMP/TMP 使用本轮隔离目录，清空三项 MICA 连接环境变量和 PYTEST_ADDOPTS，uv 离线且禁止 Python 下载。沿用前轮可写 uv 缓存，未做额外构建。本轮未读取真实凭据或真实 `~/.mica`，未访问真实渠道。

环境前置为：

```powershell
$env:HOME = 'C:\Users\10601\AppData\Local\Temp\mica-t034-worker02\home'
$env:USERPROFILE = $env:HOME
$env:TEMP = 'C:\Users\10601\AppData\Local\Temp\mica-t034-worker02\tmp'
$env:TMP = $env:TEMP
$env:UV_CACHE_DIR = 'C:\Users\10601\AppData\Local\Temp\mica-t034\uv-cache'
$env:UV_OFFLINE = 'true'
$env:UV_PYTHON_DOWNLOADS = 'never'
$env:PYTEST_ADDOPTS = ''
$env:MICA_API_URL = ''
$env:MICA_API_KEY = ''
$env:MICA_MODEL = ''
```

实际执行的最小诊断命令为 `uv run --directory C:/Users/10601/Desktop/learning/mica_agent python -c $code`，修正后的 `$code` 内容如下。两个子进程分别限时 8 秒，5 秒输出实际栈，不导入 Mica：

```python
import subprocess, sys
from pathlib import Path
log = []
for name, source in [
    ('AF_INET socketpair', 'import socket; a,b=socket.socketpair(socket.AF_INET); a.close(); b.close(); print("socketpair returned and closed")'),
    ('asyncio', 'import asyncio; asyncio.run(asyncio.sleep(0)); print("asyncio.run returned")'),
]:
    try:
        result = subprocess.run([sys.executable, '-c', 'import faulthandler; faulthandler.dump_traceback_later(5); ' + source], capture_output=True, timeout=8)
        text = name + ' exit ' + str(result.returncode) + '\n' + result.stdout.decode('utf8', 'replace') + result.stderr.decode('utf8', 'replace')
    except subprocess.TimeoutExpired as error:
        text = name + ' TIMEOUT after 8 seconds\n' + (error.stdout or b'').decode('utf8', 'replace') + (error.stderr or b'').decode('utf8', 'replace')
    print(text, flush=True)
    log.append(text)
Path(r'C:\Users\10601\AppData\Local\Temp\mica-t034-worker02\diagnostic.txt').write_text('\n'.join(log), encoding='utf8')
```

实际输出结论：

```text
AF_INET socketpair TIMEOUT after 8 seconds
  socket.py:294 in accept
  socket.py:625 in _fallback_socketpair

asyncio TIMEOUT after 8 seconds
  socket.py:294 in accept
  socket.py:625 in _fallback_socketpair
  asyncio/proactor_events.py:784 in _make_self_pipe
  asyncio/proactor_events.py:639 in __init__
  asyncio/windows_events.py:315 in __init__
  asyncio/events.py:699 in new_event_loop
  asyncio/events.py:810 in new_event_loop
  asyncio/runners.py:137 in _lazy_init
  asyncio/runners.py:59 in __enter__
  asyncio/runners.py:189 in run
```

每个栈均由 faulthandler 在实际子进程中于 5 秒时采集，8 秒由 subprocess 超时收口。诊断外层退出 0 仅表示两个超时均被记录，**不是 socketpair 或 asyncio 通过**。

本轮首次诊断也观察到第一个 socketpair 子进程超时，但外层用 text=True 后仍对字符串 stderr 调用 decode，导致 AttributeError、退出 1，未执行第二个检查。修正诊断的输出处理后，仅补跑一次上述完整最小检查，取得两个栈；此诊断脚本错误不记为产品失败。

已知：本轮 worker 的本地 socketpair/事件循环初始化仍不能正常返回；review-01 中 master 的独立诊断和定向 pytest 曾能运行。未知：两个执行上下文的差异及是否存在暂态原因，尚不能确认是哪个系统或沙箱条件导致。未把 master 的通过写成本轮结果，也未否定前轮记录。未擅改系统网络、标准库、socketpair 或事件循环；未尝试换业务执行路径获取通过。

## 本轮验证与未完成项

| 实际命令 | 本轮结果 |
|---|---|
| 上述最小诊断 | 两个子进程各 8 秒超时；实际栈见上；诊断外层退出 0。 |
| `uv run ruff format tests/test_app_interfaces.py` | 1 file reformatted。 |
| `uv run pytest tests/test_app_interfaces.py --collect-only -q -W error` | **64 tests collected in 0.44s**，退出 0；仅收集。 |
| `uv run ruff check src/mica_agent tests` | **All checks passed!** |
| `uv run ruff format --check src/mica_agent tests` | **117 files already formatted**。 |
| `git diff --check` | 退出 0，无输出。 |

按返工要求中的“若仍在事件循环初始化阻塞……保留最小证据……交回”，本轮未再启动以下运行，不把前轮运行或部分输出补记为本轮通过：

```text
uv run pytest tests/test_app_interfaces.py tests/test_app.py tests/test_cli_startup.py -q -W error

uv run pytest tests/test_app_interfaces.py tests/test_app.py tests/test_cli.py tests/test_cli_startup.py tests/test_cli_interactive.py tests/test_cli_tool_lifecycle.py tests/test_tui_main_buffer_cli.py tests/test_tui_main_buffer_screen.py tests/test_config_contracts.py tests/test_config_service.py tests/test_session_integration.py tests/test_tool_session_integration.py tests/test_reasoning_integration.py -q -W error

uv run pytest -q -W error
```

因此，开发专项、相关链路、最终全量及其包含的完整安装态均 **未完成**。本轮最后完成位置是最小诊断、接口测试收集和静态检查；没有新的行为测试通过数，也没有整套 pytest 运行的退出状态。未额外运行安装态或构建来替代全量。前轮 169 项配置、4 项提交完整性以及部分安装态输出仅是 worker-01 的历史证据，不作为修正后的本轮验收结果。

平台跳过：本轮没有执行行为套件，故没有实际平台跳过清单或全仓 skip 数；不能声称 Windows 进程/终端或其他平台已验收。未新增或更改 skip/xfail，未用排除测试、减少参数或放宽断言来取得通过。

## 现场与交回

- 接续起始 `main...origin/main [ahead 4]` 的未提交实现；未建分支、暂存、提交、推送、stash 或还原，未覆盖前轮现场。
- 原有 14 个已跟踪实现/文档修改保持；本轮只改尚未提交的接口测试，并新增本报告。`git diff --cached --name-only` 无输出；针对前轮报告、审查、返工要求、任务卡、STATUS、计划及决定的 diff 核对无输出。
- 交回 master：处理或安排可正常运行 socketpair/asyncio 的授权执行环境，再在当前修正后的版本执行上述三组命令及静态检查，取得全量中完整安装态的结果；若有新失败，先定位，在原卡范围内修复，越界问题先交回。
- 当前不能判断修正后的 64 项装配测试及其余异步回归是否全部通过。用户真实渠道验收仍未进行；本轮返工不宣称完成。
