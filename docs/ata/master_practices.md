# Master 项目实践手册

本文记录 Mica master 侧仍有效的项目特有约定；通用 master 流程以 aTa master 角色卡为准。

- **协作落盘**：项目状态使用 `docs/STATUS.md`，已确认决定使用 `docs/decisions.md`，讨论中内容使用 `docs/scratchpad.md`；候选经验统一进入 `docs/ata/inbox.md`。
- **契约任务入口**：Mica 的公共输出和 wire 变更，派卡时纳入直接断言与安装态验证资产；设计审查及兼容要求统一引用 `docs/code_spec.md` §1.3、§2.4。
- **视觉验收**：视觉任务在卡片中指明相关正向、负向参考图；对照流程遵循 `docs/assets/tui/README.md`，自动化截图不能替代用户真实终端确认。
