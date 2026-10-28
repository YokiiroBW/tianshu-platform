# UI 调整与联合发布交接

基线 `91e78f213b1c2a684697264b53852f917f072b6a`，分支 `codex/ui-tuning`。本轮交付侧栏折叠和用户子导航、移除重复页标题、固定视口与局部滚动、角色范围下的用户/群档案、群状态自动保存和关键词悬浮卡片、群概览与统计合并、可浮动/拖拽/缩放的点云，以及工作台主机资源。复用原会话、权限、policy revision、档案目录、基础设施采样缓存；未增加生产数据表或依赖。

本地专用 Vite 入口、API 替身、演示合成数据和运行目录已删除，预览进程已停止。原浏览器合成测试夹具移入 `apps/web/tests/fixtures/`，不打入运行包。

## 范围与验证

- 仅提交本轮 UI 及对应 Platform 后端。另一记忆窗口的 MemoryPage、MemoryRecords、MemoryReview、memoryTimeline.css、memory.css 和 memory-timeline.spec.ts 六文件保持原样，不纳入本提交。
- 相关浏览器原30节点分轮最终通过：22初轮通过，8个旧选择器/角色刷新夹具在修正后复验通过。新增桌面点云动作与独立滚动1节点，主机资源/侧栏偏好/7页面视口桌面手机2节点通过；手机的鼠标拖拽专项1项明确跳过。不是完整浏览器套件。
- 后端 GroupDirectory 4节点通过，含新增角色过滤、档案归属和稀疏分页；主机资源真实本地HTTP1节点通过，覆盖显式主机绑定、缓存读数、请求格式和原管理员/CSRF门。
- 主机HTTP首次误用旧合同，setup因bot-delivery/v2.1 pin不匹配失败，未进入业务。后续复用 `context-capacity-20261028` 中 TS-400-owner 严格物化合同单独复验通过，没有改pin。GroupDirectory原4节点不重跑。
- 相关 Ruff、Prettier 与 diff 检查通过，TypeScript与Vite构建通过。已有renderer大分块提示保留。最终部署包应使用本提交干净检出或联合候选构建，不能使用原混合工作目录dist。

## 联合发布

用户已在两个窗口明确授权统一串行发布；由“记忆系统隔离测试接续”窗口持有唯一生产写窗口，本窗口只推固定UI分支，不并行更新main或切换生产。

已只读核实线上192.168.31.230的统一实例 `tianshu-unified-tianshu-1` 健康，Compose位于 `/srv/tianshu/unified-20261027/compose.yaml`；应保留当前R17底图和所有数据挂载。线上尚无本基线群目录，因此联合Companion候选必须包含 SDK `4e129aa` 配套功能，不能仅替换网页。旧常驻容器维持停用。

主机资源读取 `POST /api/web/infrastructure/host`，请求空对象；部署设置新增可选 `infrastructure.platform_node_id`。沿现有 `infrastructure/view` 注册节点、执行器目录和实际机器身份确认平台所在节点后显式绑定，不能任选第一个节点。沿用现有采样缓存，没有另加采集器、SSH或模型调用。未绑定显示待绑定，过时显示上次读数。

本轮不验证真实QQ收发或模型，不进行无关存储迁移。联合记忆候选所需迁移、备份及最终镜像/线上健康收据归唯一发布者记录，本文件不冒称部署完成。
