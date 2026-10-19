# 容器与 Dockge 应用管理

入口为“家庭与服务 → 容器”（`/#/home/1`），在页内切换“容器 / Compose 栈”。沿用现有管理员会话、CSRF 和 `external.manage`，普通启停重启直接点击，不增加密码解锁。

## 使用方式

- 容器详情显示完整身份、运行和健康检查、CPU/内存资源条、端口、挂载与限定日志。控制描述按完整 64 位 ID 单独读取；没有当前授权描述时不可操作。可用“刷新控制状态”重新核对。
- Compose 栈按运行节点选择，支持搜索与分页。详情内切换运行、配置部署、配置版本和操作记录。普通应用可启动、停止和重启；镜像更新进入具体预览。
- 创建应用填写部署端允许的项目名和 Compose；修改应用基于已读取的配置版本编辑。预览显示配置差异、镜像、发布端口、目录和卷、注意事项与阻断原因。核对一次后“执行此计划”，执行只引用原计划 ID。
- 草稿保留其读取时的连接及配置修订号。尚未编辑、尚未生成预览的表单会跟随最新读回；轮询不会覆盖已编辑或已预览的草稿。外部 Dockge 编辑、节点修订号变化、草稿变化或计划过期都会使旧预览不可执行。“载入当前配置”重新开始编辑。
- 配置版本列表使用服务端原始版本 ID 和可恢复标记。选择版本后预览恢复，再执行。按预览恢复受管配置输入及运行版本，保留数据目录和卷，不表示已经发生的数据写入被回滚。
- 天枢受保护栈和 Dockge 控制依赖显示维护责任与不可用原因，普通启停按钮不向它们开放。实际可用动作由后端及节点再次核对，浏览器不决定权限。

## 结果与恢复

受理、发送、命令结束、实际运行和健康检查分别显示。未配置健康检查、健康混合、未创建容器、部分完成、来源陈旧和未知都保留原义。进度来自后台阶段，未提供确定百分比时只显示进行中。

操作持久化在后端，离开页面不取消节点操作。浏览器只在当前会话标签页保留待核对的 `client_id`，不保存配置或凭据。请求丢响应或结果未知后，刷新页面仍可按这个编号查询；“查询原操作”只调用 operation 读回，绝不重新 action/execute。节点未明确报告 `execution_active=false`、命令结果未知或操作尚未结案时，相关操作保持禁用。已结案后由后台刷新容器与栈状态，页面继续读取投影。

节点和应用操作记录分页读取；任务中心使用同一后台台账。桌面沿用三栏，窄屏依次排列；列表、详情、日志和操作记录在卡片内滚动，长配置和历史不会无限扩展页面。

## 实现与验证入口

HTTP 合同由 TS-206 的 `docs/platform/dockge-control-http.md` 固定提交 `7bbf30d` 提供，含原 `7a05cff` 的七接口及容器 detail、命令状态补充。旧只读入口保持。前端复用 `useInfrastructureQuery`、`useInfrastructureAction`、`integrationPost`、主题令牌和资源条；`dockge/` 分别负责回执恢复、动作、计划、栈详情和历史，`ContainerInspector.tsx` 承担原容器详情与日志。

```powershell
node node_modules/typescript/bin/tsc -p apps/web/tsconfig.json --noEmit
node node_modules/vite/bin/vite.js build --config apps/web/vite.config.ts
node node_modules/@playwright/test/cli.js test --config apps/web/playwright.infrastructure.config.ts
node node_modules/@playwright/test/cli.js test --config apps/web/playwright.dockge.config.ts
```

Dockge 专用配置使用独占 4837 端口、`reuseExistingServer=false`，默认启动 `tests/backend/dockge_fixture.py --register-node`。隔离开发时可通过 `TS207_FIXTURE`、`TS207_BACKEND_ROOT`、`TS207_PYTHON` 指向已固定的后端检出与解释器；`TS012_CONTRACT_DIR`/`TS177_NODE_CONTRACT_DIR` 使用协调者的冻结合同包。`TS207_UI_ONLY=1` 仅用于 `--grep projection` 的前端投影测试，不能代替真实 HTTP 测试。

projection 用例验证断线/重复点击/恢复、预览失效、冲突、维护边界、部分结果、节点命令仍活动、创建部署与滚动；真实 HTTP 用例经过 Cookie/CSRF、后台持久台账和独立执行器进程。该执行器是明确合成节点，不操作 Docker、SSH 或 NAS；真实设备验收由协调者另行执行。
