# DQ Platform 本地交接

状态：`local_verified_not_integrated`。本轮授权范围 DQ05 / DQ09 / DQ10 / DQ12 / DQ14 与 DQ06 / DQ08 的 Platform 生活页消费已完成；未发布、推送、部署、操作 NAS / QQ / 真实账号或生产数据库。

基线：`8b9949aeceb58691cb9e660b78d007f36a0b0b9d`（rc5）。分支：`codex/quality-life-20261003`。本文和实现一起提交；精确提交 SHA 由最终交接消息和协调仓库交付记录引用。

## 变更与归属

- DQ05：会话退出与 Memory 角色选择复用安全的标签页缓存助手。浏览器禁止 storage 时，服务器注销及页面选择仍完成；退出清空缓存尽力执行。
- DQ09：成功轮询开始新的首页与分页游标，移除不再符合状态筛选的记录；正在追加页时不并发替换游标。后端各来源在 LIMIT 前应用全局水位，来源没有贡献上一页时仍能继续读取未返回的旧记录。网络错误保留上次快照并明确标记。
- DQ10：角色协调的 SQLite 阶段记录、恢复、权限和模型目录检查进入既有 LocalWork；外部配置同步和读取同样复用该能力。没有第二个执行池。
- DQ12：关系路由进入现役 WebConsole 的 cookie / Origin / CSRF / 会话入口，删除重复子类。WebMemory 公开角色检查与 scoped_origin，统一临时来源条目的生成、前后检查和取消后的清理；关系管理复用它和 QQAdmin 公开权限接口，不跨模块调用私有方法。
- DQ14：删除不可达 ModelsPanel / PersonaHistory 及只被废弃人格历史引用的 PersonaRevision / personaTypes。现役 ProviderModelsPanel 和 PersonaAuthor 保留。相关任务中心验证改用现役 ProviderModelsPanel 和真实受理接口。
- DQ06 / DQ08：现役 `/#/companion/1` 增加真实持久当日安排、当前阶段和按日分页经历；沿 integrationPost → WebReader(life) → Companion 双侧读授权。角色和会话切换取消旧请求，晚到响应不能恢复旧角色；未来安排不显示为已发生经历。基础、不可用、失败、排队、生成、跳过等状态明确展示。
- 角色启用与允许对话分开。无 dialogue 角色不获得网页接收资格，不强制模型配置，但可独立基础生活；已选择模型或已发布默认模型走既有 ProviderSelection / Gateway，自主生成由 Companion 负责。停用暂停生活。
- 失败 / unavailable / interrupted 允许显式重试计划或当前阶段。复用现役 role.manage + life.read，前后均经独立生活读 token 验证角色可读，Core 管理 token 只发送固定重试端口。回执只表示受理，随后读持久投影；计划版本 CAS 冲突和权限撤回明确报错。没有新增解锁或确认门禁。
- actors 只为 Companion 已授权返回的本地角色附 RoleRuntime 已保存名称。旧 Core 角色保持原返回，页面缺名称时显示 id；没有额外管理授权、人格查询或第二份生活状态。

消费正式 `life-read/v1`，LF manifest SHA256：`7be7507d58f897a739b269de3c096ba948c92b25266888a91fa50130d342c551`。根合同由协调者单写；Platform 未修改根合同、任务板、AGENTS、依赖锁或其他产品。部署及正常配置路径见 `docs/platform/role-life.md`。

## 实际验证

以下检查沿已有 pytest / Playwright / TypeScript / Vite，复用相关代码未变的已通过结果，没有反复跑全量。

| 实测范围                                                                                     | 结果                                                                                  |
| -------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------- |
| role_runtime / relationships / web_memory / tasks / web_readers / web_external 受影响后端    | 57 passed，40 subtests passed                                                         |
| 共享 HTTPS 管理传输 / 最终合同绑定，role_runtime / web_readers / web_console                 | 26 passed                                                                             |
| 重试独立读授权前后检查；actor 拒绝时不发送管理调用，await 期间权限撤回                       | 1 passed                                                                              |
| quality-platform + integration-pages + memory-role，桌面和手机                               | 24 passed；新增重试 UI 后重跑前两套 22 passed                                         |
| 现役模型 ProviderManager                                                                     | 2 passed                                                                              |
| 任务中心旧断言调整及过期会话恢复                                                             | 完整一次 7 passed / 1 failed，修正恢复导航后仅失败用例重跑 1 passed；8 项各自最终通过 |
| 实际 Platform → Companion life 读链和浏览器桌面 / 手机，无 route mocks                       | 最终 1 passed（8.48s，含两个浏览器项目）                                              |
| 实际新建角色 Platform / Companion / Memory / Gateway 四服务 + 合成模型 + 浏览器              | 最终 1 passed（12.61s）                                                               |
| 关系 Platform / 固定提交 Memory / 固定提交 Companion 实际 HTTPS 联合                         | 5 passed（63.31s）                                                                    |
| TypeScript noEmit / Vite build，含最终角色名称与页面                                         | passed；保留既有 RoomPage 大于 500 KiB 提示                                           |
| 受影响生产模块和 life/read 定向用例 Ruff；新增生活页 / 联合浏览器 Prettier；git diff --check | passed                                                                                |

四服务用例由浏览器在现役角色管理新建“独立生活角色”、选择模型 B、启用但取消 dialogue 与 memory 能力；没有预写角色、生活授权或调用测试 tick。Core 运行后台登记与派发；实际 Selector → Gateway → 合成模型请求先返回无效计划。网页显示失败并显式重试，Core 受理后以新请求身份生成计划与阶段内容并落库；独立 reader 读取、页面显示并重载保持；停用角色后 today.enabled=false / plan.state=paused。断言无聊天接收资格、角色名称、所选模型实际调用、计划与阶段生成、受理重试、停用暂停和手机无横向溢出。

读链用例在明确隔离合成库保存 23 条同秒经历，真实 HTTPS 分页 20 + 3 无重复 / 漏项；旧 actors / snapshot / diaries / revision 一并验读。actor 越权 404、生活权限撤回 403、正式 schema 请求 / 响应、读前后所有持久行不变。该静态夹具覆盖分页与只读，动态角色和生成由四服务用例另覆盖。

真实四服务截图（忽略的本机运行产物，没有入库，父级已目视）：

- `C:/YOKI/Codex/tianshu-peiban-bot/worktrees/quality-life-20261003/platform/.runtime/role-life-joint/results/role-life-desktop.png`
- `C:/YOKI/Codex/tianshu-peiban-bot/worktrees/quality-life-20261003/platform/.runtime/role-life-joint/results/role-life-mobile.png`

## 联合验证入口

在本 Platform 检出中运行。Python 需要当前已安装的 pytest / aiohttp / cryptography 等依赖，Node / Playwright 复用原工作区已安装环境。所有运行目录均隔离、忽略，不用生产默认值。

```powershell
$dqPython = 'C:/YOKI/Codex/tianshu-peiban-bot/.runtime/nas-a1-r1-venv/Scripts/python.exe'
$dqSources = 'C:/YOKI/Codex/tianshu-peiban-bot/worktrees/quality-life-20261003'
$dqGateway = 'C:/YOKI/Codex/tianshu-peiban-bot/projects/tianshu-model-gateway'
$env:PYTHONDONTWRITEBYTECODE = '1'
$env:TS050_CONTRACTS = "$dqSources/coordination/contracts/text-dialogue/v1"
$env:TS012_CONTRACT_DIR = $env:TS050_CONTRACTS
$env:TS050_RUNTIME = "$dqSources/platform/.runtime/role-life-joint"
$env:TS014_JOINT_RUNTIME = $env:TS050_RUNTIME
$env:TS050_SNAPSHOT_ROOT = $dqSources
$env:TS_ROLE_GATEWAY_ROOT = $dqGateway
$env:PYTHONPATH = "$dqSources/coordination/tests/integration;$dqSources/platform/tests/backend;$dqSources/platform;$dqSources/companion/src;$dqSources/memory/src;$dqGateway/src"
New-Item -ItemType Directory "$env:TS050_RUNTIME/results" -Force
& $dqPython -m pytest tests/backend/test_role_joint.py::RoleJoint::test_new_runtime_role_life_reads_and_generation_without_dialogue -q --tb=short

$env:TS013_TLS_PYTHON = $dqPython
$env:TS_COMPANION_PATH = "$dqSources/companion"
$env:TS_LIFE_BROWSER = '1'
& $dqPython -m pytest tests/backend/test_life_joint.py -q -s --tb=short
```

静态 life 联合浏览器入口为 `apps/web/playwright.life-joint.config.ts` / `apps/web/tests/life-joint.spec.ts`，由 Python 服务夹具派发，不单独假造页面服务器。四服务真实浏览器入口为 `tests/backend/role_life_browser.mjs`，复用 RoleJoint / 本轮协调仓库 ts050 source support；没有新测试框架。

关系联合沿固定历史提交导出，可独立绑定仓库布局：

```powershell
New-Item -ItemType Directory .runtime/relationship-source/projects -Force
git clone --shared --no-checkout ../memory .runtime/relationship-source/projects/tianshu-memory
git clone --shared --no-checkout ../companion .runtime/relationship-source/projects/tianshu-companion
$env:TS_RELATIONSHIP_SOURCE_ROOT = "$dqSources/platform/.runtime/relationship-source"
$env:TIANSHU_CONTRACTS = $env:TS012_CONTRACT_DIR
$env:PYTHONPATH = '.;tests/backend'
& $dqPython -m pytest tests/backend/test_relationship_joint.py -q --tb=short
```

临时共享仓库只创建一次；再次运行复用已有目录，不覆盖或删库。

前端构建与定向浏览器命令：

```powershell
node node_modules/typescript/bin/tsc -p apps/web/tsconfig.json --noEmit
node node_modules/vite/bin/vite.js build --config apps/web/vite.config.ts
node node_modules/playwright/cli.js test --config apps/web/playwright.config.ts quality-platform.spec.ts integration-pages.spec.ts memory-role.spec.ts
node node_modules/playwright/cli.js test --config apps/web/playwright.config.ts task-center.spec.ts --project=desktop --workers=1
```

## 失败归因与未覆盖

原 HEAD 的四项 task-center 失败已用 git archive 到忽略目录窄重放，全部同样失败：两项旧 ModelsPanel 专属断言、两项统一 Auth 已卸载面板后的旧等待 / .tasks-error 断言。本轮删除废弃组件后已调整这些验证路径，并保留真实任务受理与会话晚回包目标行为。一次并行 Playwright 使用同 outputDir 导致 trace 文件 ENOENT，改独立 outputDir 后消除；不视作业务通过或失败。

旧 `test_relationship_joint.py` 首次五项停在 bootstrap：脚本的 ROOT/projects 选错仓库布局，git archive 无法导出目标，未进入应用代码。协调者复核确认两个历史 SHA 均存在；不能归因成历史 SHA 缺失。本轮补充 `TS_RELATIONSHIP_SOURCE_ROOT`，在忽略目录用对应本轮仓库建立 `git clone --shared --no-checkout` 的临时 projects 布局，仍只导出固定 Memory `1f3121c9758faeb31fc9d0fe2a54974c72505d55` 与 Companion `21e4ff37f1d4f4e3e9db94f8c965031a8dcb9cd6`。导出的旧 Harness 还需要 `TIANSHU_CONTRACTS` 显式指本轮正式合同，以代替不会入库的 workspace-context。配置这两项后实际五项全部通过，覆盖私密表达 / 群聊裁剪 / 回复落定、冻结解冻不追补、人物能力 / 来源 origin / 版本变化撤回拦截在途回复。没有伪造历史源、修改固定 SHA 或读取对方可变应用源。

本轮未执行全量 Platform / 真实 NAS / 真实账号和模型 / QQ / 发布包启动 / 生产迁移 / 长时间跨日与外部网络验收。Gateway 是既有 rc5 生产实现，付费模型端点是隔离合成服务。生活内容权威在 Companion，关系和来源权威在 Memory，本次没有访问对方生产数据库。

发布脚本按交付 manifest 的合同清单打包，不自动发现新合同。协调者已将 life-read/v1 与 source-sync-batch/v1 显式纳入本轮交付 inventory；本检出仅绑定并消费，未创建或部署发布包。正式集成、发布和设备验收由父级安排。
