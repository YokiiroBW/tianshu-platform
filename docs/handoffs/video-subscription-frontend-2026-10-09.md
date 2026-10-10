# 视频订阅前端本地交付（2026-10-09）

基线 `6a2ed3d0d6f6b3f0be6761c3b9cf61239ea5d2b4`；独立检出 `worktrees/video-subscription-20261009/frontend`。首批前端提交 `519c880b79f35e8daa5b5ec084fa880f95755db1`；后续联验修正的 SHA 由协调者读取 git。本地实现和隔离 HTTP 联验已验证，最终集成由协调者完成；没有推送、NAS 部署或真实 B 站账号操作。

## 实现及复用

唯一入口 `/#/resources/2`。复用应用壳、统一登录/CSRF、webFetch、requestId、StatusRail、StatePanel 和既有设计令牌，新增媒体页面懒加载。面板按链接下载、订阅、下载任务、账号与媒体库分工，领域规则与执行事实仍归后端。

链接解析支持分 P、共同可用画质、严格指定质量与显式降级；四类来源以粘贴链接为主，包含历史/新增、扫描间隔、分组规则试算、订阅编辑及暂停/恢复。任务历史通过 jobs/list 状态筛选和游标逐页读取，包含超过 view 的前 100 条；切筛选取消旧请求和丢弃迟到页。详情显示下载、发布、逐服务器核对，支持取消/重试、元数据版本保护及重新解析来源。账号支持本地二维码、可见时检查、本人 Cookie 导入清空、校验与撤销。

所有在途读取按槽位、会话与代号保护；隐藏/卸载清理，断线保留标记快照，会话失效清空页面；未确认同一写入沿用幂等 UUID。合法规则以 Unicode 码点计数，媒体请求总预算 6 MiB，LAN HTTP 复用现有 UUID 能力。桌面双栏、中屏详情在前、手机详情替换列表；键盘及焦点恢复保持可用。详细流程与命令见 `docs/platform/media-web.md`。

依赖 `qrcode-generator@2.0.4` 由协调者在 integration 更新 manifest/lock 并 npm ci；本检出 node_modules 只读 junction 复用，未修改共享依赖。服务端 DTO 消费 backend 的 `docs/platform/media-api.md`，未改服务、迁移或合同。

## 实际验证

- TypeScript `tsc -p apps/web/tsconfig.json --noEmit` 通过；Vite build 通过。原 renderer 541 KiB 警告仍在，本次媒体懒加载 JS 约 77 KiB。
- 媒体 UI：15 项 × 桌面/中屏/手机共 45 项。末次全套 44 通过、1 手机断言选中被详情隐藏的列表画质；断言收口到可见详情后，相关三尺寸 3/3 通过。产品代码状态未变。覆盖 105 条历史续页、筛选迟到页、四类链接、规则错误及 4096 Unicode 码点、元数据 CAS、重新解析来源 CAS、会话/断线、幂等重放、QR/导入、分 P 和实际画质、进度换算、键盘/尺寸。
- StrictMode 开发模式专项 1/1 通过（取消/重挂载能重新读事实）。
- 既有任务中心 8/8 通过（实际本地平台 HTTP 与合成 HA），不连接真实设备。
- 既有资产页 6 通过、1 失败：退出后旧用例断言行内“需要先登录”，当前统一登录已转到登录页。同一失败已在固定基线 6a2ed3d 隔离导出中复现，未改资产/Auth。
- 既有应用壳 15 通过、4 失败、1 按尺寸跳过。4 失败是两个 chunk 注入用例 × 两尺寸仍期待旧“小屋环境预览”标题；同一 4 项已在固定基线隔离导出中复现。未扩大本任务修旧用例。
- 最新三尺寸截图为 `.runtime/media-ui/screenshots/media-desktop.png`、`media-tablet.png`、`media-mobile.png`，已实际检查无横向溢出，画质中文标签及折叠错误码可读。截图和 trace 为忽略的运行产物。

早期一轮 trace 目录冲突由并行运行不同套件造成，已将媒体 UI/dev 输出隔离到 `.runtime/media-ui`，相关两项单独复跑通过，不将 ENOENT 归为产品失败。

## 实际平台 HTTP 联验及修正

`playwright.media-joint.config.ts` 对外部启动的实际平台 HTTP 不做 API 拦截。18898 联验使用 backend 所有者运行的 `run_media_fixture.py`；B 站解析/账号、资产发布接收为明确的合成上游，FFmpeg 实际生成视频并由 ffprobe 校验。桌面正常链+元数据恢复 2/2 通过；手机元数据恢复通过，正常链改为每次唯一合成新增 BV 后复跑通过，共四个场景均通过。

正常链覆盖实际网页登录、QR waiting→scanned→ready、会话校验、2P/画质解析、下载发布回执、未登记服务器时只显示 published、任务中心资源投影、四类 URL 规范化、真实规则解释、历史模式选择、完整首次成员基线及后续新增入队、暂停。恢复链以独立 BV 的空简介生成 waiting_metadata，在网页带版本补全后，验证该 job_id 恢复为 published。手机最初“新增入队应为1”的断言失败是共享后台中桌面已发布同视频命中质量复用，改唯一合成新 BV 后通过，未修改产品去重复下载。

真实 QR 找到前端缺陷：waiting→scanned 使 effect 重启并立即再读，触发 qr_poll_too_fast。修正为同一二维码只持有一个检查生命周期，状态变化不重启检查；恢复可见也保留5秒最小间隔，并用代号拒绝取消后迟到回包。新增最小间隔回归与既有导入回归在三尺寸共6/6通过，类型/构建重新通过。阶段原因、规则短路和实际 TS090 元数据 issue 用中文呈现；新增 Job.layout 展示“单视频/分P剧集”，single→订阅布局冲突明确提示选择独立媒体库。

启动和控制接口由 backend fixture 所有者提供，不在前端检出启动旧版 services。测试参数：`MEDIA_JOINT_URL=http://127.0.0.1:18898`（缺省此值），`MEDIA_FIXTURE_CONTROL=http://127.0.0.1:<上游端口>/__fixture/control`（必填，来自 fixture 启动输出）。执行 `node node_modules/@playwright/test/cli.js test --config apps/web/playwright.media-joint.config.ts`。该套件会修改隔离 fixture 的成员、分P、简介和扫码状态，不能指向生产。上游控制端口当次为62245，重启会改变。

## 后续集成与限制

前端替身与此 HTTP 联验都不是实际 B 站下载证据。18898 进程尚未包含 backend 最后的“订阅1P始终multipart”收口，但2P主流程已验；协调者将在固定integration上接最终后端与真实本地资产/Jellyfin/Emby。真实 B 站授权/上游下载、容器构建、NAS、生产发布不在本地前端验收证明范围。
