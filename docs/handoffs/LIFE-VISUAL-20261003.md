# LIFE-VISUAL — 生活页视觉与真实天气

基线：Platform `3f715b6e94c6552b30e03c17497f4dd56f32292e`。用户批准概念图后授权一比一视觉落地，并追加真实位置、时区、温度及和风天气。

## 实现

- 沿用原应用壳、角色授权和生活读接口，重排今日概览、当前活动、日程时间轴、心情和日记入口。
- 时间轴显示短摘要；悬停/键盘焦点显示预览，点击原生 dialog 展示完整原文，Escape 关闭并恢复焦点，手机支持点开。不新增摘要模型调用，不删原文。
- 已发生经历保留独立日期和分页，未来安排不冒充经历。30秒读取后台变化，生成中2秒；隐藏页面暂停。刷新不卸载天气设置，不更改后台独立生活时钟。
- 和风接入沿用服务端加密 ExternalCatalog、会话/CSRF/life.read/external.manage；位置按角色保存，连接共用。位置时区控制顶部真实日期/时钟，角色日程保持原业务时区。
- 支持 API Host/API Key 配置、城市搜索、位置选择、温度/体感/天气/风力/数据归因、失败保留旧值并标记。详见 [天气协议与运行说明](../platform/weather.md)。

## 验证

- TypeScript、生产构建、涉及前端文件格式和 diff whitespace 通过。小屋既有较大 chunk 警告不属于本批。
- 浏览器针对性用例累计16项（桌面/手机）：详情交互、未来状态、角色隔离、分页、日记读取、天气配置/凭据清空、纽约时区、失败旧值、后台刷新不卸载设置。分批执行；既有文本选择器因新增预览重复匹配已适配，合成 visibilitychange 触发时序已修正。
- 后端天气合成测试11项、原外部连接及天气真实本地HTTP入口5项通过；涉及Python文件Ruff通过。
- 截图基于隔离合成角色/天气，不是生产账号或真实和风读数。没有和风真实凭据，外部账号联验尚未执行。没有全量回归声明。

## 插画来源

`apps/web/public/life-evening.png` 使用 imagegen 依据已批准概念图生成，作为氛围装饰，不冒充角色现场照片。源图：`C:/Users/Administrator/.codex/generated_images/01a0fd63-f3a7-7813-803a-0cb755ca4c60/exec-32de75e0-f901-4f5d-8931-409de179bb84.png`。

提示词：Generate a standalone decorative illustration asset for the approved UI shown in reference. ONLY the cozy desk illustration inside the left 此刻 card: wooden desk with an open book and pen, ceramic white mug with tiny cat motif, stacks of books on right, warm desk lamp on left, plant leaves at right edge, broad window behind with lavender twilight sky and softly glowing distant city windows. Match reference illustration colors, composition, calm premium painterly anime editorial style. Wide landscape 2.8:1 crop, clean full-bleed art to every edge. No UI, no borders, no text, no labels, no watermark. This is ambient decorative art, not a depiction of actual character activity.

## 发布边界

本记录为本地实现交接；NAS状态由协调发布记录另行确认。新增天气记录后回滚旧镜像需同时恢复原external目录备份。用户需在生活页设置自己的和风专属Host/API Key，并选择位置，密钥不通过聊天提交。
