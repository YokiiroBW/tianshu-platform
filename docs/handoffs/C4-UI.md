# C4-UI 候选交接（2026-10-04）

状态：`local_verified_not_integrated`。分支 `codex/companion-complete-ui-20261004`，基线 `6a2ed3d0d6f6b3f0be6761c3b9cf61239ea5d2b4`。本交接与完整 UI 候选同提交，固定 SHA 由提交后的协作消息交协调者。仅 `apps/web/**` 与此交接；没有 services、适配器、共享合同、依赖或锁文件变更。六个初始 runtime 文件由 C4 后端写者移交后接续。

## 接通结果与复用

- 原生活页挂载完整工作区：此刻/短期感受、活动进度暂停恢复、开放事项；保持原日程、天气、经历、日记布局。活动切换读取对应时间，空时间真实写 null，取消编辑清空草稿；新卡心情复用原中文映射，旧经历原文不改写。
- 衣柜编辑/选择当前穿搭，角色身份与服装参考实际上传图片；描述→二进制→独立 Knowledge→Core acquire 的真实 ref。图像创作采用任务回执、原任务查询/取消、相册原图读取与保存、真实 ComfyUI 配置及模型列表，不伪造生成结果。参考原件绑定真实版本/来源范围，实际认证身份由后台派生。
- 原文/媒体/共同阅读和自主阅读，实际指定范围读取、画面/音频表示与覆盖缺口；仅真实 reading ID 实读推进断点。作品、大纲、章节目标、当前原稿修订/审阅/发布和同一版本继续；管理读显式 `chapter_view=current`，不以目录当正文。
- 统一人物页增加主动订阅、安静时段、限额及准确送达段数/未知状态；账号关联经真实双账号挑战，恢复仅保存 challenge ID 后重新查询当前授权和版本，撤销沿真实 Memory CAS。
- 小屋复用原分层插画、环境控制和现有几何预览；读实际角色时区、活动、进度、当前穿搭授权原件。插画待机保留原坐姿层，休息只移动至床边，外出隐藏；几何预览做有限姿态变换。没有走路、骨骼、睡眠或衣物物理动画，也没有新增角色素材或引擎。
- 网页对话沿原 messages/snapshot/cancel 增量链，分段正文与送达标签独立；已送内容在未知取消后仍显示，失效范围清空，引用按需读真实原件。模型能力显示实际 provider revision 的核验状态，未知能力仍可尝试。
- 所有新调用沿同源 Cookie/CSRF；浏览器不用服务凭据、人物/会话 ID 表单或 JSON 编辑器。重用现有 requestId，未知结果保留同内容 client ID 与对象 ID，手动重试核对原操作；上传复用同 descriptor/status，LAN 无 SubtleCrypto 时标准 SHA-256 回退，Knowledge 再核原 bytes。

## 实际验证

最终产品代码状态：TypeScript `--noEmit`、Vite build、改动 Prettier 检查与 `git diff --check` 通过。原 Three renderer 分包 541.77 kB 的构建提示保留，未变更依赖或拆新引擎。

使用既有 Playwright。以下按实际运行记录列出，不推算全仓库总数：

1. 原 life-visual/people/room 相关 24 例运行：22 通过，2 例旧小屋固定昼夜/旧控件名称假设失败。调整为固定时间与当前真实控件后，仅对应桌面/手机两例重跑，纳入下项通过。
2. 定向 10/10：活动编辑时间清理、未知写入同体重试、分页撤权隐藏；增量回复与未知取消/失效历史；关联恢复与版本撤销；主动偏好重试与部分/未知投递；旧小屋像素和模式切换。状态 fixture 为明确合成投影，不代替实际 owner 鉴权。
3. 独立 SHA-256 1/1：原生与无 SubtleCrypto 回退，空串、UTF-8、55/56/63/64/65 字节与百万字符，对比 Node 原生 hash。
4. 媒体展示 2/2：抽样画面、实际时间、音轨缺口保持可见；未知生图任务的已有原图可读，不据此声称生成完成。
5. 实际五 TLS owner 与独立 Memory/Knowledge DB 的桌面/手机四例通过：活动与关注、实际 TXT/PNG descriptor+bytes 上传、原文实读/断点重载/衣柜选择；旧日程真实返回、image_backend 管理读、作品/章节/原稿重载/审阅发布。未调用模型。强制禁用 randomUUID/SubtleCrypto 的实际上传通过。
6. 新增身份参考实际流 2/2：图片上传、配置 CAS、重载、原件实读与小屋读取。发现并交 C3 修复未配置参考 read version=1 与首次写 expected=0 不一致；最终 fixture v4 使用统一 read 1→首写 2（C3 固定 `a22acfa6890072794d40442222bb2f4704b01292`）。中间截图脚本旧作品依赖/精确 label 假设失败已修，未掩盖业务错误。
7. 最后仅替换插画待机/床边分支后 TypeScript/build 通过，实际 v4 桌面/手机登录→小屋只读补拍，角色投影在场、无横向溢出。已通过业务不再重跑。

重现主要命令（已有 Node/依赖，不安装新框架）：

```powershell
node node_modules/typescript/bin/tsc -p apps/web/tsconfig.json --noEmit
node node_modules/vite/bin/vite.js build --config apps/web/vite.config.ts
node node_modules/@playwright/test/cli.js test --config apps/web/playwright.config.ts life-runtime.spec.ts people.spec.ts room-illustration.spec.ts --grep 'activity editing|incremental replies|account link resumes|proactive preferences|illustration loads' --workers 2
node node_modules/@playwright/test/cli.js test --config apps/web/playwright.config.ts content-hash.spec.ts --project desktop
node node_modules/@playwright/test/cli.js test --config apps/web/playwright.config.ts life-runtime.spec.ts --grep 'sampled media'
# C4 先启动五 owner 隔离夹具，并提供含合成登录的 ignored JSON；不用于生产账号。
$env:TS_C4_JOINT_FIXTURE='<C4 browser-fixture-v4.json 绝对路径>'
node node_modules/@playwright/test/cli.js test --config apps/web/playwright.runtime-joint.config.ts
```

## 截图与限制

本 checkout 绝对根：`C:/YOKI/Codex/tianshu-peiban-bot/worktrees/companion-complete-20261004/platform-ui/`。实际截图均为 ignored 运行产物：

- 当前手机身份参考：`.runtime/ui-browser-joint/runtime-joint-real-identit-011ca-kspace-and-room-projections-mobile/identity-workspace-viewport.png`；同目录 `writing-workspace-viewport.png` 为重启后新库的真实新作品面板，没有造旧作品。
- 同场景 desktop 目录有对应 viewport。最终小屋 `.runtime/ui-browser-evidence/room-final-desktop-viewport.png` 和 `room-final-mobile-viewport.png`。
- 首组实际业务 `.runtime/ui-browser-evidence/runtime-joint-real-split-o-ac8b0-drobe-on-desktop-and-mobile-{desktop,mobile}/` 下 activity/reading/wardrobe.png；原稿实读/发布 `.runtime/ui-browser-evidence/runtime-joint-real-current-27835-on-with-the-current-manager-{desktop,mobile}/writing.png`。长图用于完整记录，viewport 用于视觉评审。

真实生图后端/模型尚缺实际配置，本 UI 候选不声称已真实生图；生图状态/原图展示的 fixture 与真实 Knowledge 图片上传明确区分。未操作真实 QQ、用户数据、模型或 NAS，未推送/部署。原件下载仍要求当前真实 owner/share 权限，不把角色读取或 QQ 附件发送当成用户网页库授权。账号关联真实双作者 proof/CAS 联合由 C4 后端验证，UI 浏览器状态 fixture 只验证恢复/反馈/重试呈现。

下一步：协调者将本固定候选串行合入以 Platform `e2e4795f71729fdbfb03e48ae3a9247b3f382a6e` 为基线的独占 release checkout，整批候选镜像构建与生产验收仍由协调者负责。没有当前未关闭的 UI 业务失败。

## 图片呈现 follow-up

只对 ContentViewer 的 image kind 隐藏字节起止输入和字节进度，使用原 readContent 的完整 coverage 读取；按钮改为“查看图片”“下载原图”，真实图片、缩放/缺口说明和拒绝读取后的隐藏保留。文本/文章、视频/音频继续原有实际范围，读取权限/版本/保存逻辑不变。同步图片浏览器选择器，补图片整图请求/拒绝/原图下载定向用例；桌面/手机图片与媒体共 4/4，TypeScript/build/diff check 通过。按协调者收口指示不再重建完整夹具补截图，既有真实原件链证据继续适用；最终新 UI 图片由后续 NAS/clone 验收观察。
