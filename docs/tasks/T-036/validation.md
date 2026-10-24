# T-036 验证记录

2026-10-24；初始工作区干净；分支 `task/T-036-messages-tool-fix`；HEAD `f523709a27cf3f0acb0f2e05a944abfe3196bbd4`。

不读取真实配置、凭据或会话，不访问真实模型渠道。后续测试在进程初始化前隔离配置根、移除凭据变量并安装 DNS/外连守卫。

## 可复执行的本轮离线诊断

下面代码通过标准输入交给 `uv run --no-sync --offline python -` 执行，不创建额外脚本。手写预期位于网络边界，不调用产品编码器生成答案。历史为合成事实，不执行历史工具。Engine 的唯一可执行工具为带精确路径及次数守卫的产品 read。

```python
import os
import re
import tempfile
from pathlib import Path

home = tempfile.TemporaryDirectory(prefix="mica-t036-")
for name in list(os.environ):
    if re.search(r"KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|AUTH", name, re.I):
        del os.environ[name]
for name in (
    "HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA",
    "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME",
):
    os.environ[name] = home.name

import asyncio
import gzip
import itertools
import json
import socket
import sys

connect = socket.socket.connect
connect_ex = socket.socket.connect_ex
def denied(*args, **kwargs):
    raise AssertionError("external network or DNS blocked")
def local_connect(sock, address):
    assert sock.family == socket.AF_INET and address[0] == "127.0.0.1"
    return connect(sock, address)
def local_connect_ex(sock, address):
    assert sock.family == socket.AF_INET and address[0] == "127.0.0.1"
    return connect_ex(sock, address)
socket.socket.connect = local_connect
socket.socket.connect_ex = local_connect_ex
socket.create_connection = denied
socket.getaddrinfo = denied
socket.gethostbyname = denied
socket.gethostbyname_ex = denied
socket.gethostbyaddr = denied

sys.path.insert(0, str(Path.cwd() / "tests"))
import httpx
import pytest
from test_anthropic_messages_provider import (
    request, record, sample, text, start, block_start, block_stop, delta, ending,
)
from sse_transport_helpers import RecordingTransport, sse_response
from mica_agent.continuation_contracts import (
    TextItem, ToolCallItem, MessagesThinkingItem,
)
from mica_agent.providers.anthropic_messages import AnthropicMessagesProvider
from mica_agent.providers.base import Finish
from mica_agent.models import Message, MessageRole
from mica_agent.tool_contracts import ToolCall, ToolResultStatus
from mica_agent.engine import AgentEngine
from mica_agent.events import RunCompleted, RunFailed, ToolFinished
from mica_agent.file_tools import register_file_tools
from mica_agent.tool_registry import ToolRegistry
from mica_agent.tool_executor import ToolExecutor
from mica_agent.policy import PolicyEngine
from mica_agent.permissions import PermissionMode
from mica_agent.workspace import resolve_workspace

async def histories():
    count = 0
    for ids, prose, reverse, source in itertools.product(
        [("a", "b", "c"), ("bad/id", "mica_tool_0", "bad.id"),
         ("调用", "bad:id", "mica_tool_1")],
        [False, True], [False, True], ["none", "native", "model", "degraded"],
    ):
        legal = {i: i for i in ids if re.fullmatch(r"[a-zA-Z0-9_-]+", i)}
        reserved = set(legal.values())
        counter = 0
        for i in ids:
            if i not in legal:
                while f"mica_tool_{counter}" in reserved:
                    counter += 1
                legal[i] = f"mica_tool_{counter}"
                reserved.add(legal[i])
                counter += 1
        messages = [Message(MessageRole.USER, "begin")]
        expected = [{"role": "user", "content": [text("begin")]}]
        slots = []
        for batch in [ids[:2], ids[2:]]:
            content = "正文\nquoted: \"中\"" if prose else ""
            args = {"path": "probe.txt", "nested": [None, True, {"中": "x\\y\n"}]}
            calls = tuple(ToolCall(i, "read", args) for i in batch)
            messages.append(Message(MessageRole.ASSISTANT, content, calls))
            items = ((TextItem(None),) if prose else ()) + tuple(ToolCallItem(i) for i in batch)
            # 原生引用不含私有签名，允许请求内重映射；错绑样本添加私有内容以检查过滤。
            if source == "none":
                slots.append(None)
            elif source == "degraded":
                slots.append(record())
            elif source == "model":
                slots.append(record((MessagesThinkingItem("private-thought", "opaque"), *items), model_id="other"))
            else:
                slots.append(record(items))
            blocks = ([text(content)] if prose else []) + [
                {"type": "tool_use", "id": legal[i], "name": "read", "input": args}
                for i in batch
            ]
            expected.append({"role": "assistant", "content": blocks})
            results = []
            for i in reversed(batch) if reverse else batch:
                result = json.dumps({"status": "execution_failed", "data": [i, "中"]}, ensure_ascii=False)
                messages.append(Message(MessageRole.TOOL, result, tool_call_id=i))
                results.append({"type": "tool_result", "tool_use_id": legal[i], "content": result})
            messages.append(Message(MessageRole.USER, "continue"))
            expected.append({"role": "user", "content": [*results, text("continue")]})
        async def handler(req):
            body = json.loads(req.content)
            assert body["messages"] == expected
            assert body["thinking"] == {"type": "disabled"}
            assert "tool_choice" not in body
            assert "private-thought" not in req.content.decode()
            return sse_response(sample([text("answer")]))
        transport = RecordingTransport(handler)
        provider = AnthropicMessagesProvider("fake-test-key", transport=transport, max_retries=0)
        req = request(messages=tuple(messages), continuations=tuple(slots))
        try:
            events = [e async for e in provider.stream(req)]
        finally:
            await provider.aclose()
        assert isinstance(events[-1], Finish) and not events[-1].tool_calls
        assert len(transport.requests) == 1 and all(r.is_closed for r in transport.responses)
        assert req.messages == tuple(messages)
        count += 1
    print("history_matrix:", count, "passed")

class Wire(httpx.AsyncByteStream):
    def __init__(self, data):
        self.data = data
    async def __aiter__(self):
        for offset in range(0, len(self.data), 3):
            yield self.data[offset:offset + 3]

async def engines():
    count = 0
    for compressed, mode in itertools.product([False, True], ["text", "tool", "missing_stop", "invalid_json", "refusal"]):
        with tempfile.TemporaryDirectory(dir=home.name) as directory:
            workspace = resolve_workspace(Path(directory))
            value = "read-result-中"
            (Path(directory) / "probe.txt").write_text(value, encoding="utf-8")
            original = ToolRegistry()
            register_file_tools(original, workspace)
            entry = original.resolve("read")
            guarded = ToolRegistry()
            reads = []
            async def allowed(req, token):
                assert req.name == "read" and dict(req.arguments) == {"path": "probe.txt"}
                assert not reads
                reads.append(req.call_id)
                return await entry.handler(req, token)
            guarded.register(entry.spec, allowed)
            chunks = [start(), block_start(0, {"type": "tool_use", "id": "t036", "name": "read", "input": {}}),
                      delta(0, "input_json_delta", partial_json="[]" if mode == "invalid_json" else '{"path":"probe.txt"}'),
                      block_stop(0), *ending("refusal" if mode == "refusal" else "tool_use")]
            if mode == "text":
                chunks = sample([text("Please use read; no protocol call was returned.")])
            elif mode == "missing_stop":
                chunks = chunks[:-1]
            async def handler(req):
                body = json.loads(req.content)
                assert body["tools"] == [{"name": "read", "description": entry.spec.description,
                                           "input_schema": {"type": "object", "properties": {"path": {"type": "string", "minLength": 1}}, "required": ["path"], "additionalProperties": False}}]
                response = chunks
                if len(transport.requests) == 2:
                    assert mode == "tool" and reads == ["t036"]
                    assert body["messages"][1] == {"role": "assistant", "content": [{"type": "tool_use", "id": "t036", "name": "read", "input": {"path": "probe.txt"}}]}
                    result = body["messages"][2]["content"]
                    assert len(result) == 1 and result[0]["tool_use_id"] == "t036"
                    assert result[0]["type"] == "tool_result"
                    assert value in result[0]["content"]
                    response = sample([text("done")])
                wire = "".join(response).encode("utf-8")
                return httpx.Response(200, stream=Wire(gzip.compress(wire) if compressed else wire),
                                      headers={"content-type": "text/event-stream", **({"content-encoding": "gzip"} if compressed else {})})
            transport = RecordingTransport(handler)
            provider = AnthropicMessagesProvider("fake-test-key", transport=transport, max_retries=0)
            executor = ToolExecutor(guarded, PolicyEngine(PermissionMode.DEFAULT), workspace=workspace)
            try:
                events = [e async for e in AgentEngine(provider, tool_executor=executor).run(request(tools=guarded.specs()))]
            finally:
                await provider.aclose()
            assert isinstance(events[-1], RunCompleted if mode in {"tool", "text"} else RunFailed)
            assert reads == (["t036"] if mode == "tool" else [])
            finished = [e for e in events if isinstance(e, ToolFinished)]
            assert len(finished) == (1 if mode == "tool" else 0)
            if finished:
                assert finished[0].status is ToolResultStatus.SUCCESS
            assert len(transport.requests) == (2 if mode == "tool" else 1)
            assert all(r.is_closed for r in transport.responses) and transport.aclose_count == 1
            count += 1
    print("engine_fragmented_wire:", count, "passed; product read:", 2)

try:
    asyncio.run(histories())
    asyncio.run(engines())
    raise SystemExit(pytest.main([
        "tests/test_anthropic_messages_provider.py",
        "tests/test_app_interfaces.py::test_pure_reasoning_empty_assistant_slot_survives_round_trip",
        "tests/test_app_interfaces.py::test_signed_history_server_rejection_is_reported_without_stripping_or_retry",
        "-k", "not real_engine", "-q", "-W", "error",
    ]))
finally:
    home.cleanup()
```

## 实际命令与结果

### 环境初始化

启动 `uv sync --python 3.11` 前，进程 HOME、USERPROFILE、APPDATA、LOCALAPPDATA、XDG_CONFIG_HOME、XDG_DATA_HOME、XDG_CACHE_HOME 指向本轮新建的临时目录；按变量名移除匹配 `KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|AUTH` 的环境项，不读取/打印值。

`uv sync --python 3.11` → exit 0；CPython 3.11.15，创建本 worktree 的 .venv；安装 25 个锁定依赖，下载所需 pygments/ruff 包，未改依赖定义或锁文件。依赖安装访问不等于模型渠道访问。

### 新输入及必要专项

在同样隔离的启动进程中执行：

```powershell
$diagnosticText = Get-Content docs/tasks/T-036/validation.md -Raw
$diagnosticCode = [regex]::Match($diagnosticText, '(?s)```python\r?\n(.*?)\r?\n```').Groups[1].Value
$diagnosticCode | uv run --no-sync --offline python -
```

实际输出（exit 0）：

```text
history_matrix: 48 passed
engine_fragmented_wire: 10 passed; product read: 2
169 passed, 6 deselected in 6.76s
```

48 组历史矩阵和 10 组 Engine 矩阵的执行先于 pytest，无产品 monkeypatch；仅在 HTTP 边界返回响应。历史中的合成 read 事实从未执行；Engine 使用临时文件、真实 read handler、真实 ToolExecutor 和真实 Provider。

pytest 的范围是 Messages 文件，加 App 中纯思考空 assistant 往返、带签名历史被拒绝的两个测试名（纯思考测试参数化为三接口，均为离线 transport）。`-k 'not real_engine'` 在执行前排除了 Messages 文件内六个既有合成 lookup 场景（一个多步往返、五个不执行保护），避免执行本卡禁止的其他 Agent 工具；本轮十组真实产品 read/零执行矩阵针对工具往返与失败边界补证。这里的 deselected 是授权范围选择，不是跳过失败，也不是完整 Messages 专项 171 项通过。

守卫在导入 pytest/产品前安装：DNS 查询禁止，create_connection 禁止，socket connect/connect_ex 只允许 Windows asyncio 所需 IPv4 127.0.0.1。所有 Provider 使用 RecordingTransport 和 fake-test-key，不读取真实配置、会话或模型 Key；没有真实模型请求。脚本中的临时 HOME 和每例工作区由 TemporaryDirectory 创建者关闭和清理；没有递归删除用户目录。启动隔离空目录保留在系统临时区，没有清理其他文件。

### 首个失败与修正

本轮新增矩阵及上述 pytest 首次运行全部通过，无失败回归、无产品修正，也无“旧失败 → 补丁 → 新通过”证据；不能宣称修复完成。

只读定位时尝试读取 `tests/conftest.py` 返回文件不存在；随后依据实际文件读取测试内 fixture 和入口，不创建该文件，不把定位命令的缺失文件当作产品失败。官方 `text-block-param` 子页无法抓取，未重试/绕过访问限制，未据此设定协议断言。`Create a Message` 正文可读，但其当前版本没有用于更换产品版本头。

### 文档及 Git 检查

最终命令：`git diff --check`；对两个新增文件分别执行 `git diff --no-index --check -- NUL <file>` 以覆盖未跟踪文件的空白检查；核对文档中的本地链接目标、两份文档无尾随空白；核对 `git status --short`、`git diff --name-only`、`git diff --cached --name-only`。

未运行全量 pytest、全仓 Ruff 或构建/安装复验：仅新增两份 Markdown 证据文档，没有产品或正式测试变更，按本卡要求不重复全量刷通过数。无真实 Provider、用户会话、终端或远端同步验收。

最终检查结果：上述三个空白检查命令全部 exit 0；文档本地链接与尾随空白检查通过。`git status --short` 仅列本轮两份未跟踪 Markdown；产品 diff 和暂存区均为空。最终 HEAD 与初始值相同，未执行 Git 写操作。
