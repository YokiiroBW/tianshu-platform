# CONNECT-U 前端集成交接

## 目标与基线

产品基线 `1d51d888ff77594a1f6fbb1c7e0d4fdf92285ff8`。只改 `apps/web`、前端测试及本交接；没有修改后端、其它产品、根合同、小屋渲染资产或 NAS。浏览器请求按 CONNECT-B 固定 `347a15d87189f0269861d1e0d209b4748380926b` 的 `docs/handoffs/CONNECT-B-API.md` 接入；Memory 用户只读端口另依 CONNECT-M 产品候选 API，最终须以其固定提交复核。

## 第一批前端闭环

| 导航 | 实际网页动作 | 数据和限制 |
| --- | --- | --- |
| 记忆 → 概览、人物与群、本人记忆 | 当前授权范围概览、主体分页、共享画像及本人有效语义组分页 | `/api/web/memory/{state,overview,subjects,records}`；不显示来源原文、服务身份或未批准内容；账号关联没有已实现网页证明流程，明确标记尚未提供。 |
| 资料与资源 → 研究资料；项目 → 项目工作区 | 项目选择、资料目录分页、按版本核验正文、关键词检索、研究笔记检索 | `/api/web/knowledge/{state,documents,document,query,notes}`；目录不冒充来源已新鲜，正文再核版本；搜索空结果、遗漏、过期与失败分开。 |
| 陪伴 → 生活与日记 | 授权角色目录、最后持久化的虚构生活状态、已发布日记分页和版本核验正文 | `/api/web/life/{state,actors,snapshot,diaries,revision}`；不触发生活时钟，草稿正文无入口。 |
| 设置 → 连接 | 九项固定业务能力的真实状态摘要及按状态的下一步 | `/api/web/connections/view`；已配置未验、真实已读、未配置、未授权、失联和尚未提供分开；内置服务无地址或 token 输入。 |

未提供的订阅下载、容器清单、节点/游戏服、身体活动、Memory 账号关联等入口改为明确说明，避免“仅需配置”误导。工作台任务文案不再声称所有来源未接入。原对话、人设、资产、家庭、任务与模型页面保留。

## 实际验证

- 使用捆绑 Node 与本产品固定依赖版本安装本地 `node_modules`；未改清单与锁。`tsc -p apps/web/tsconfig.json --noEmit` 通过；Vite 生产构建通过，既有 RoomPage 超 500 KiB 提示仍在。
- `playwright.config.ts` 运行 `integration-pages.spec.ts` 与 `shell.spec.ts`：桌面/移动 **25 通过、1 条桌面专用跳过**。新增场景包括项目目录/正文/搜索/笔记、角色生活/已发布正文、记忆概览/共享与本人范围、未配置与未授权区别；连接摘要、壳与可访问性回归同轮通过。
- 新页面的浏览器用例使用明确的合成同源响应，不构成 Memory/Companion 真实进程或 NAS 验收。截图为忽略产物 `apps/web/test-results/**/knowledge.png`，桌面与手机均核对布局，手机宽度无横向溢出。
- `git diff --check` 通过。最终提交 SHA 和后续新增自助连接/经验接口验证待补。

## 待集成与风险

- CONNECT-B / CONNECT-M 的同源与上游服务还在分别验收；生产必须配置 Memory 专用浏览身份、知识服务独立进程、Companion 生活读者与正式授权，执行真实业务读取后才能把状态标“已实际读取”。
- 人格候选合同仍受正式发布门槛，既有人格页不可把本地 rehearsal 称为生产可用。
- 项目“经验与交接”当前未有浏览器端口。总控已要求 Memory 只对已实现的 lesson/experience 检索与受限 continuation 增最小 HTTP，再由 Platform 发布固定同源 API；页面等待其精确契约，不直接调用 Memory 或读取工作目录。
- HA 与 AssetLink 自助连接页待 CONNECT-B 发布精确管理接口；保存不等于业务连接通过，外部目标与凭据仍需服务端验证。
- 不涉及真实账号、真实设备、真实资产、真实记忆资料、付费模型或 NAS 操作；这些必须由协调者在受控环境联合验收。

## 下一步

接 B 的 HA/AssetLink 管理接口和项目经验接口；更新本记录、复核完整 diff、运行受影响浏览器流程，交固定 SHA 给总控和独立审查。
