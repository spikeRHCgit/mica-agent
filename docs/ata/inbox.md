# 候选经验收件箱

> 由 master 维护。这里的条目是候选，不是规则，任何会话都不按它行事。
> 格式：`- YYYY-MM-DD · 来源 · 现象和证据位置 → 建议去向`

- 2026-10-25 · T-038 worker · HTTPX 的 `Response(content=AsyncIterable)` 包装层不一定把 aclose 转发到内层测试流；用 `Response(stream=...)` 时应分别观察响应、客户端和自定义流关闭，并保留取消任务收口断言。证据见 `docs/tasks/T-038/validation.md` §2 → 网络测试边界与资源观察的 worker 实践候选，尚未晋升。

- 2026-09-30 · T-015 / T-016 跨平台审计 · Git 源码输出需明确按 UTF-8 解码；CRLF 工作树与 LF
  blob 的原始字节不同，也可能具有相同过滤后 object ID。证据见 `c6c7997:docs/tasks/T-015.md`、
  `26383d5:docs/tasks/T-016.md` → 区分物理字节契约与 Git 内容检查的审计实践候选，尚未晋升。

- 2026-09-29 · T-012 / T-013 生命周期诊断 · `id(object)` 会复用，warning 观察 node 不一定是
  创建 node，追踪也会改变时序。证据见 `1dfae8d:docs/tasks/T-012.md`、
  `d91e45a:docs/tasks/T-013.md` → 创建代次与追踪证据限制的诊断实践候选；历史 warning 未定位。

- 2026-09-28 · T-007 worker · 一次相对路径结构化替换误改主工作区，随后精确恢复。证据见
  `e73f0f4:docs/tasks/T-007.md` → 显式绑定目标 worktree 并核对变更归属的 worker 实践候选，
  本次不修改实践手册；不得仅凭恢复成功将事件视为没有发生。

- 2026-09-26 · P5-11C-MR3 光标修复 · 没有正文要重画时，仍可能需要发送光标定位；只比较正文会
  漏掉可观察输出。证据见 `3829052:docs/log/phase5_build_log.md` P5-11C-MR3、
  `docs/tui_design.md` §5.2 → 领域实现观察，尚未提炼为所有模块通用的优化规则。

- 2026-09-26 · P5-11C 构造边界 · `MainBufferRuntime` 曾用 `hasattr(run/aclose)` 接受冒充对象，
  现已采用严格类型检查。证据见 `3829052:docs/log/phase5_build_log.md` P5-11C、
  `src/mica_agent/tui/main_buffer/runtime.py` → 保留为该装配边界经验，不据此要求所有协议使用
  `isinstance`；是否提炼通用规范仍待后续证据。
