# PERSONA-CLEAN-20260929

- 目标：`#/companion/2` 直接呈现简洁的左侧人格列表与右侧详情；新建进入编辑态，编辑时名称和正文为主，其他字段默认折叠。
- 基线：`c4033532cd2876c8c1caddbf75e424e643bc64a8`；交付分支：`codex/persona-clean-20260929`，提交以该分支最终 HEAD 为准。
- 变更：删除本页旧目录、历史、修订和对比 UI；合并可复用档案及授权角色列表，隐藏技术 ID；保留创建、复制、草稿、应用、权限、幂等、版本冲突输入和离页提醒。后端、合同及其他页面未改。
- 验证：TypeScript 类型检查通过；Vite 生产构建通过；真实隔离 HTTPS Platform + Companion Core 的 `playwright.persona-author.config.ts` 2 项通过，覆盖创建、编辑、应用、折叠字段保留、扩展字段仍存在、后退拦截、冲突保留、只读权限与桌面/手机布局。`git diff --check` 通过。未重跑输入未变化的后台套件。
- 截图：[桌面详情](persona-clean-2026-09-29/desktop.png)、[桌面编辑](persona-clean-2026-09-29/edit.png)、[手机详情](persona-clean-2026-09-29/mobile-detail.png)、[手机列表](persona-clean-2026-09-29/mobile-list.png)。均来自上述真实 HTTPS 浏览器夹具。
- 风险/下一步：浏览器投影只公开扩展字段名称，不公开扩展正文；该用例核对字段仍存在，底层值保持依赖既有 Core 写入契约。总控审查本提交与截图后串行集成、按部署流程发布；本任务未操作 NAS。
