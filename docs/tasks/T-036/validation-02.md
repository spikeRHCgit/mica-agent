# T-036 第 2 轮证据 · 2026-10-24

依据：resume-01-request.md。基线 `cedd91d047d47a18a4b7b7a3b3e572eb36364247`，初始工作区干净。

本轮最多新增 5 次、含既有 3 次累计最多 8 次；计数在发送前持久化，失败也占用。输出仅白名单结构事实，无真实原始请求/响应、地址、模型、正文、ID、参数或文件内容。

## 进度与不可重置计数

- 初始累计：3；本轮已发：0。

- `{"live_requests": 0, "passed": true, "phase": "selftest", "real_credentials_read": false}`

- `{"live_requests": 0, "passed": true, "phase": "selftest", "real_credentials_read": false}`

- `{"live_requests": 0, "passed": true, "phase": "selftest", "real_credentials_read": false}`

- `{"live_requests": 0, "passed": true, "phase": "selftest", "real_credentials_read": false}`

- `{"phase": "stopped", "reason": "guard"}`

## 无凭据自检与首个失败

一次性探针默认只执行无凭据自检；只有显式 `--authorized-live-resume-01` 才进入真实配置/渠道分支。临时 HOME 等配置根在产品导入前隔离；清除凭据环境变量只按变量名，不打印值。无凭据阶段拦截外连和 DNS，仅保留 Windows asyncio 的 IPv4 本地回环。

本轮自检针对观察器和出站闸门，不重复第一轮 48/10 成功矩阵：

- 普通/gzip × HTTP 200/400 的三字节分片，共 4 个网络边界用例；在内层 HTTPX `aiter_bytes()` 解压后观察，外层去掉 Content-Encoding/Content-Length，防止二次解压；正常帧及终态可读，响应收口。
- 错误体解压后最多 64 KiB；正常观察内存最多 512 KiB。仅提取已知错误类型、协议字段路径及固定关键词布尔值；数字下标归一为 `[]`，未知字段、URL、假秘密均不输出。合成错误的预期路径为 `messages[].content[].input`、必填字段为 `input`。
- 出站累计上限 8；达到上限后闸门在 transport 前拒绝，无真实访问。
- 缺 message_stop 的合成流不能当作完成；仅在临时文件上执行一次真实产品 read，经 ToolExecutor 返回 success；其他路径/第二次读取由精确守卫拒绝。
- live 入口每次发送前将累计数写入本文件并 flush/fsync，失败也占用；如果文件已有已发标记，不允许从 3 重启。每请求包括流消费的总时限 30 秒；禁止自动重试、重定向及非冻结 endpoint，保留 H 后同进程分支入口。

初版传输自检通过后，补充“全局停止标志”时出现首个失败：`SyntaxError: name 'FATAL' is used prior to global declaration`。这是临时探针的声明位置错误，发生在脚本编译阶段；当时没有读取真实配置/凭据或发送请求。将 `global FATAL` 移到函数入口后重新执行本轮自检，通过。未改产品、正式测试、断言或规则。

最终探针版本的默认入口实际输出（exit 0）：

```text
{"live_requests": 0, "passed": true, "phase": "selftest", "real_credentials_read": false}
```

实际命令形式：`uv run --no-sync --offline python <本轮临时目录>/probe.py`。没有重复执行第一轮矩阵或全量测试；本轮不新增依赖。

## 唯一一次 live 入口与停止位置

在同一最终探针版本执行 `uv run --no-sync --offline python <本轮临时目录>/probe.py --authorized-live-resume-01`，先再次运行上述必要自检，再进行获准连接读取。实际白名单输出（exit 1，未发送请求）：

```text
{"live_requests": 0, "passed": true, "phase": "selftest", "real_credentials_read": false}
{"phase": "stopped", "reason": "guard"}
```

闸门在 `ScopedStore.__init__` 通过现有 `ConfigStore.read('models')` 读取模型元数据后，核对启用 Completions 模型连接的唯一性。候选数为 2，违反恢复要求“只使用这两条测试连接；若不能唯一确认则停止”。停止发生在 ConfigService 构造及 auth 读取之前，HTTP transport 尚未建立，更未执行累计计数消耗。没有按默认模型或名称猜测选择，没有切换至其他候选，没有重跑 live 入口。

为了将笼统 guard 输出定位成可交接的确切原因，随后仅执行了一次**禁网、无 auth 的元数据复核**。通过现有 ConfigStore 只读 models，按启用供应商与模型实际 API 计数，仅输出以下安全事实：

```json
{"anthropic-messages_count": 1, "anthropic-messages_custom": true, "anthropic-messages_https": true, "openai-completions_count": 2, "same_provider": false}
```

`same_provider=false` 不能用于选择，因为 Completions 本身不是唯一候选；未输出任何供应商 ID、模型标识或地址。custom/https 布尔值只是 Messages 唯一候选的元数据，不证明实际后端或工具能力。本次复核禁止 socket connect/connect_ex 和 DNS，不使用 auth、不发模型请求，不是对真实请求的重试。

该复核的无凭据片段（在配置根隔离、凭据环境变量移除和禁网之后执行；`authorized_root` 仅为进程内的既有配置根）：

```python
from mica_agent.config_store import ConfigStore
raw = ConfigStore(authorized_root, isolated_workspace).read("models")
counts = {}
for api in ("openai-completions", "anthropic-messages"):
    candidates = [
        (pid, provider, model)
        for pid, provider in raw.get("providers", {}).items()
        if isinstance(provider, dict) and provider.get("enabled", True) is True
        for model in provider.get("models", [])
        if isinstance(model, dict)
        and model.get("api", provider.get("api", "openai-completions")) == api
    ]
    counts[api + "_count"] = len(candidates)
# 仅可输出 counts 的 API 枚举与计数；不打印 raw/candidates。
```

## 计数、未执行项与结论边界

- 本轮真实出站 **0**；历史累计仍为 **3/8**，没有任何失败网络请求需要加计，也不存在未知消耗。
- auth 受保护读取入口尚未执行；没有读取用户会话、其他客户端凭据或私有控制台，没有修改真实配置。模型目录仅在内存解析，未保存原始目录或任何实际模型/地址/认证值。
- H 的 Completions read、结果续问和 Messages 历史请求均未执行；A3 未执行；真实 Agent read 总计 0（自检临时文件的一次 read 独立计为离线自检，不是真实模型调用）。没有 Responses。
- A/H 没有新增真实根因证据，未确认或修复本地缺陷；没有产品与正式测试变更，因此未重跑 Messages/Engine/App 专项、全量 pytest、全仓 Ruff、构建或安装态验收。
- 剩余额度不能消除“哪条 Completions 连接被授权”的身份歧义。本轮按停止条件交回，需要 master/用户给出唯一的获准连接标识/选择依据；不需要 Key、服务地址或用户会话。不私自禁用/改动另一连接来制造唯一性。

## 最终检查与清理

- 已核对本轮临时目录的绝对位置位于系统临时目录内，且只剩自身创建的 probe.py；仅删除该文件及随后空目录，没有递归清理。TemporaryDirectory 所有的自检/live 隔离资源均由创建进程关闭。
- `git diff --check` exit 0；两份新增文档各自的 `git diff --no-index --check -- NUL <file>` 无空白错误输出，实际 exit 1（与空设备比较有文件差异），不将其记为 exit 0。本地 Markdown 链接和独立尾随空白检查通过。
- 最终 `git status --short` 只列 `validation-02.md` 与 `worker-02.md` 两份未跟踪文件；`git diff --name-only` 与 `git diff --cached --name-only` 均为空，旧报告及受保护文件无改动。
- 最终 HEAD 仍为 `cedd91d047d47a18a4b7b7a3b3e572eb36364247`。没有暂存、提交、推送、stash、还原或清理他人文件。
