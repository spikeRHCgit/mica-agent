# T-039 返工 1 验证记录

## 环境与隔离

- Windows worktree：C:/Users/10601/Desktop/learning/mica-wt/T-039；分支 task/T-039-connection-maintenance；本轮开始 HEAD f90c60e，执行基线未变。
- 使用既有 CPython 3.11.15 虚拟环境，所有 Python/pytest/Ruff 均经 `uv run --no-sync --offline`。
- 测试只使用 pytest 临时目录、假 `TEST_ONLY_*` Key、隔离临时 ACL 和 HTTPX MockTransport/RecordingTransport；未读取真实 `~/.mica`、真实 Key 或真实渠道，未发送真实模型请求。
- 底层 ConfigStore/CredentialStore 正常路径不 fake；本轮只在真实 ACL 的目录保护边界、auth 原子替换边界和后验读取边界注入失败/交错。

## 修前失败复现

为避免把"新增测试刚好通过"当成回归有效，将 `src/mica_agent` 复制到系统临时目录，仅逆向还原本轮对 `_connection_maintenance.py` 的改动，再用 `PYTHONPATH` 指向该副本运行本轮 9 个新增回归。已确认导入路径指向临时副本；工作区产品文件未被改写。结果：

    $env:PYTHONPATH = '<temp>/src'
    uv run --no-sync --offline pytest <9 tests> -q -W error
    9 failed in 8.54s

失败分布：

| 回归 | 修前实际 |
|---|---|
| 新增连接写前保护失败 | `unconfirmed` 但 `completed=("credential",)`，auth 未创建 |
| 改 Key 写前保护失败 | `unconfirmed` 但 `completed=("credential",)`，原 auth 字节未变 |
| 新增连接替换后保护未确认 | `completed=("credential",)`，把未确认写成已提交 |
| 改 Key 替换后保护未确认 | `completed=("credential",)`，把未确认写成已提交 |
| 改 Key 提交前并发禁用 | 返回 `complete` 和旧 ConnectionSummary |
| 改 Key 提交前并发删除 | 返回 `complete` 和旧 ConnectionSummary |
| 改 Key 后验读取失败 | 返回 `complete`，忽略连接状态已不可读 |
| 编辑句柄嵌套基线 | 内层 models 仍为 list，嵌套字典可原地修改 |
| 发现句柄嵌套基线 | 内层 models 仍为 list，修改未抛错 |

修后同一组回归：

    uv run --no-sync --offline pytest <9 tests> -q -W error
    9 passed in 8.45s

## 关键行为证据

### 1. 保护异常与提交范围

- 写前保护失败用真实 ACL 将临时配置根放宽为含 `CreateFiles` 的共享访问，使 `ensure_credential_directory` 在写入前抛出 `CredentialProtectionError`：
  - 新增连接：`status=unconfirmed`、`completed=()`、`remaining=("credential","models")`、`connection=None`；auth.json 不存在，models.json 不存在。
  - 改 Key：`status=unconfirmed`、`completed=()`、`remaining=("credential",)`；auth.json 字节与调用前完全一致，且不含新假 Key。
- 替换后保护未确认（受保护读取在真实 auth 替换后抛错）：
  - 新增连接与改 Key 均为 `unconfirmed`、`completed=()`；auth 中真实出现新 Key；hint 仍含 "replaced" 且不含 Key。
  - 这样不会把"可能已替换"写成确定提交，也没有声称旧记录一定还在。
- delete 的既有语义未改：models 已确认删除时仍为 `completed=("models",)`，protected delete 的 credential 进入 `remaining`。
- `MaintenanceOutcome` 文档已明确 `completed` / unconfirmed 下 `remaining` 的含义。

### 2. 改 Key 后验竞争

回归在真实 auth 文件 `os.replace` 之前插入另一窗口的真实 `ConfigStore.update_provider` / `delete_provider`，随后继续原 `CredentialStore` 替换；未 fake 维护动作或 CAS，也未用 sleep。

| 交错 | 返回 | 磁盘 |
|---|---|---|
| auth 替换前并发禁用 | `partial`；`completed=("credential",)`；`remaining=("models",)`；`connection=None`；hint 要求刷新 | auth 已是新 Key；models 的 enabled=false；未回滚 |
| auth 替换前并发删除 | `partial`；同上 | auth 已是新 Key；models 中无该连接；未重建 |
| auth 提交后 models 读取失败 | `unconfirmed`；`completed=("credential",)`；`remaining=("models",)`；hint 说明 Key 已保存但状态待确认 | auth 已是新 Key；未回滚 |

后验读取失败只在该 auth 替换成功之后注入；失败后仍保留"凭据保存已成功"的已确认事实，不伪装成整个操作未执行。

### 3. 嵌套基线不可变与真实保存闭环

- `ConnectionEdit._expected_provider["models"]` 与 `CatalogDiscovery._expected_provider["models"]` 均为 tuple；内层模型字典、`future.nested` 映射及 `input` 均为只读表示。对它们赋值均抛 `TypeError`。
- auth 基线的未知扩展 `future.nested` 同样只读；对嵌套 tuple 赋值抛 `TypeError`。
- 未修改的原始基线随后完成真实保存：
  - `rename` → `complete`，完整 models 记录（含嵌套未知字段）原样保留；auth 字节不变。
  - `save_catalog_selection` → `complete`，既有模型保留、新勾选模型写入；auth 字节不变。
- 证明内部 tuple/list 冻结没有把 tuple 直接交给 ConfigStore，存储比较仍使用原生 JSON dict/list。

## 最终验证

维护模块：

    uv run --no-sync --offline pytest tests/test_connection_maintenance.py -q -W error
    37 passed in 32.40s

最终专项：

    uv run --no-sync --offline pytest tests/test_connection_maintenance.py tests/test_config_service.py tests/test_model_catalog.py -q -W error
    267 passed in 146.16s

最终全量（稳定后集中执行一次）：

    uv run --no-sync --offline pytest -q -W error
    2927 passed, 3 skipped in 375.31s

3 项 skip 为既有平台/条件用例，本轮未新增 skip。

## 静态检查与工作区

    uv run --no-sync --offline ruff check .
    All checks passed!

    uv run --no-sync --offline ruff format --check src/mica_agent tests
    124 files already formatted

    git diff --check
    （无输出，退出码 0）

全仓格式检查的如实结果：

    uv run --no-sync --offline ruff format --check .
    7 files would be reformatted, 224 files already formatted

7 个文件均为既有文档（docs/tasks/T-024.md、docs/tasks/T-034/worker-02.md、docs/tasks/T-036/validation*.md、docs/tasks/T-037/T-037.md、docs/tasks/T-038/T-038.md），属于旧任务产物，本轮未修改。产品代码与测试的 124 个文件全部通过格式检查；未为了让全仓检查变绿而重排他人文档。

未执行 `uv lock --check` 或 `uv build`：本轮未改依赖、锁文件、安装入口、打包规则或包资源，按卡不另构建。

## 未验证与剩余边界

- 未实现 `/login` 菜单、应用/终端接入或真实用户验收；未验证真实渠道登录、账号资格、额度或推理可用。目录成功和本地保存不等于远端认证成功。
- 本机为 Windows；POSIX 分支未实机运行，3 项全量 skip 与既有记录一致。
- 逐文件原子/CAS 不是跨文件事务；进程在两次提交之间崩溃不会自动回滚。改 Key 的后验核对发生在凭据提交后，只能报告当时可观察到的变化；之后的新变化仍按既有快照失效/for_run 规则处理。
- 保护异常采用保守报告：替换后实际可能已写入时也不进入 `completed`，调用方必须刷新并重新读取；不能把 `remaining` 理解为"证明绝未写入"。
- 本轮不新增跨文件事务、锁序、底层异常/存储契约或配置格式变更。

## 修前副本清理

修前对照使用系统临时目录中的独立副本，完成后已按确认真实路径删除；工作区未保留副本、未暂存、未提交、未推送、未 stash。
