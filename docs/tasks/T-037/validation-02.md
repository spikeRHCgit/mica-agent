# T-037 返工 1 验证证据

> 验证环境：Windows（`os.name == "nt"`）· Python 3.11.15 · pytest 9.1.1 · Ruff 0.16.2
> worktree：`C:/Users/10601/Desktop/learning/mica-wt/T-037` · 分支 `task/T-037-connection-storage` · 开始 HEAD `f910d79`（保留第一轮未提交产品 diff）
> 本轮沿用隔离临时目录与假 Key；未读取真实 `~/.mica`、真实凭据或渠道，未修改 Provider，未新增模型请求。未暂存、提交或推送。

## 1. 先补的失败回归（修复前）

新增回归后、修改实现前执行：

```
uv run pytest tests/test_config_store.py::test_null_or_non_object_provider_target_is_not_treated_as_absent tests/test_config_store.py::test_non_string_keys_are_rejected_before_writing tests/test_config_store.py::test_serialized_configuration_is_strictly_readable tests/test_config_store.py::test_config_store_depends_on_shared_validation_not_the_service tests/test_credential_store.py::test_null_or_non_object_credential_target_is_not_treated_as_absent -q -W error
```

结果：**7 failed**，均为产品输入反例，不是环境故障：

| 回归 | 修复前实际表现 |
|---|---|
| `test_null_or_non_object_provider_target_is_not_treated_as_absent` | `DID NOT RAISE ConfigError`：`{"target": null}` 被当作缺失，`update_provider(..., expected_value=None)` 直接成功 |
| `test_non_string_keys_are_rejected_before_writing[future0]` | `DID NOT RAISE`：`{1:"one","1":"two"}` 写入成功 |
| `...[future1]` | `DID NOT RAISE`：单个整数键被静默转成字符串 |
| `...[future2]` | `DID NOT RAISE`：嵌套数组中的整数键同样通过 |
| `test_serialized_configuration_is_strictly_readable` | `serialize_json({"future": {1: "one"}})` 未拒绝 |
| `test_null_or_non_object_credential_target_is_not_treated_as_absent` | `DID NOT RAISE`：`auth` 的 null 目标被当作缺失并覆盖 |
| `test_config_store_depends_on_shared_validation_not_the_service` | `_config_validation` 不存在，仍存在 `_read_rules` 反向导入 |

该项静态依赖回归最初只识别带模块名的 `ImportFrom`，会漏掉 `_read_rules` 使用的 `from . import config_service`；审查发现后已改为同时收集相对导入的 `names`，修复前后续运行以此为准。修复后新增的 tuple 嵌套参数（`{"nested": ({2: "two"},)}`）用于覆盖容器递归，列入同一测试。

## 2. 修复内容

- **目标缺失与显式无效区分**：`ConfigStore._provider_target` 与 `CredentialStore._write` 改为先按键是否存在判断；已存在但非对象（含 null）立即 `ConfigError`，不参与“预期不存在”比较，原字节保留；真正缺失 + `expected_* = None` 才创建；缺失 + 期望记录、或现存 + 期望不存在均保持 `ConfigConflictError`；删除入口对非法/缺失/过期目标同样拒绝。`models` 的 `providers` 根结构、`auth` 的 `providers` 根结构校验保持不变。
- **严格 JSON 输入/输出闭环**：`serialize_json` 在编码前递归拒绝 `Mapping`、`list`、`tuple` 中任意层级的非字符串对象键，保持 `allow_nan=False` 与 4 MiB 上限；编码后调用既有 `parse_json(payload)` 严格读回，只有可严格读取的字节才返回给原子替换。错误统一为不含参数内容的 `ConfigError`，替换发生在校验之后，失败保留原文件。
- **消除反向依赖**：新增最小内部模块 `src/mica_agent/_config_validation.py`，放置 `API_IDS`、`CHANNELS`、`RESERVED_PREFIXES`、`is_text`、`validate_url`；`ConfigStore` 单向导入它，删除 `_read_rules`。`ConfigService` 以导入别名保留 `_text`、`_url`、`_CHANNELS`、`_RESERVED`，接口/渠道成员判断改用 `_API_IDS`；Provider 默认预算、思考映射、来源解析、连接冻结和权限行为仍留在服务层。`CredentialStore` 也复用 `RESERVED_PREFIXES`/`is_text`，不再保留第三份 ID 前缀规则。
- `docs/module_map.md` 增加 `_config_validation.py` 的实际职责。

## 3. 修复后结果

| 阶段 | 命令 | 结果 |
|---|---|---|
| 回归（修复后） | 上述回归选择（含新增 tuple 参数） | 8 项全部通过 |
| 存储专项 | `uv run pytest tests/test_config_store.py tests/test_credential_store.py -q -W error` | 64 passed, 1 skipped |
| 最终专项 | `uv run pytest tests/test_config_store.py tests/test_credential_store.py tests/test_credential_permissions.py tests/test_config_service.py -q -W error` | **234 passed, 1 skipped**（110.29s） |
| 裸安装入口点 | `uv run pytest tests/test_distribution.py::test_installed_public_entrypoints_match_contract -q -W error` | 4 passed |
| 最终全量 | `uv run pytest -q -W error` | **2823 passed, 3 skipped**（324.83s） |
| 静态检查 | `uv run ruff check src/mica_agent tests` | All checks passed |
| 格式 | `uv run ruff format --check src/mica_agent tests` | 120 files already formatted |
| 空白 | `git diff --check` | 通过（无输出） |

静态依赖方向核对（`config_store.py` 无 `config_service` 引用，两方均导入 `_config_validation`）：

```
--- imports in config_store
src/mica_agent/config_store.py:11:from ._config_validation import (
src/mica_agent/config_store.py:18:from ._file_lock import locked_file
src/mica_agent/config_store.py:19:from .config_contracts import ConfigConflictError, ConfigError
src/mica_agent/config_store.py:20:from .reasoning import ReasoningEffort
src/mica_agent/config_store.py:21:from .session_contracts import SessionSelection
--- config_service refs in config_store
none
--- config_service.py
src/mica_agent/config_service.py:12..24:from ._config_validation import (...)
src/mica_agent/config_service.py:38:from .config_store import ConfigStore
```

`tests/test_config_service.py::test_shared_api_ids_stay_in_sync_with_provider_defaults` 保证共享接口标识与 Provider 默认映射键一致；`tests/test_config_store.py::test_config_store_depends_on_shared_validation_not_the_service` 以 AST 固定依赖方向。

## 4. 本轮首次全量失败与根因

修复三项返工后第一次全量：

```
4 failed, 2819 passed, 3 skipped in 339.17s
FAILED tests/test_distribution.py::test_installed_public_entrypoints_match_contract[arguments0..3]
ModuleNotFoundError: No module named 'httpx'
  File ".../mica_agent/_config_validation.py", line 5, in <module>
```

根因：新增 `_config_validation.py` 在模块顶层 `import httpx`，而 `ConfigStore` 被 CLI 顶层导入；既有裸安装契约要求 `mica --help`/`--version`/`python -m mica_agent` 在未安装运行依赖时仍可启动。修正为仅在 `validate_url` 实际校验时局部导入 httpx；`_config_validation`、`ConfigStore`、`CredentialStore` 的模块导入恢复为不依赖第三方包。

修正证据：

- 阻塞 httpx 的导入探针下 `import mica_agent.config_store` 与 `import mica_agent.credential_store` 均成功；
- `tests/test_distribution.py::test_installed_public_entrypoints_match_contract` 4 passed；
- 随后最终专项与最终全量均通过（上方数字）。

## 5. 语义保持与边界

- 现有读取校验语义保持：URL、三接口/渠道、模型能力默认/显式声明、非法输入安全错误与临时来源保护的 `tests/test_config_service.py` 全部通过（含本轮新增共享标识同步断言）；`ConfigService` 仅替换共享规则来源，业务解析与连接冻结逻辑未改。
- 权限实现本轮未修改；第一轮真实 Windows ACL 证据继续适用，`tests/test_credential_permissions.py` 未跳过或放宽。POSIX 分支仍未在本机实机执行，维持原披露。
- 第一轮报告 `worker-01.md`、`validation.md` 与 master 的审查/返工文档未修改；工作树中第一轮产品改动保留。未提交、未推送。
