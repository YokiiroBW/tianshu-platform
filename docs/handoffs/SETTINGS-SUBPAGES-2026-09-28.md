# SETTINGS-SUBPAGES-2026-09-28 交接

## 目标与基线

- 基线 `c5f4d3a`，分支 `codex/settings-subpages-20260928`，工作树 `worktrees/SETTINGS-SUBPAGES/tianshu-platform`；本交接与实现一起提交，固定 SHA 见交付消息。
- 把原 `/#/settings/1` 的四种设置拆开，解释机器人尚无登记对象时的下一步。保留现有风格和小屋，不改后端协议、凭据、锁文件或依赖。

## 变更

- 设置顶部导航扩为七页：`0` 任务记录、`1` 连接总览、`2` 模型供应商、`3` 机器人接入、`4` 资产库接入、`5` 家庭设备接入、`6` 访问与域名。旧 `0–2` 数字深链仍有效；`1` 仅显示连接状态和独立设置入口。
- AssetLink 与 Home Assistant 各自只渲染当前类型的表单；组件按类型独立挂载，保存和检测请求仅带当前 `kind`，切页放弃未保存草稿。
- 无机器人登记时显示“尚未添加机器人”：说明当前网页不能直接添加机器人账号，列出 NoneBot/AstrBot 宿主选择、机器人账号、宿主实例、测试群或私聊、允许作者与角色，以及部署登记后创建默认停用连接的步骤。没有伪造添加表单或连接状态。
- 更新受影响的浏览器用例路径，并新增页面隔离、双向草稿隔离、未登录、桌面与手机断言。机器人原有用例在点击“停用”后增加“已停用”页面状态等待，再断言旧凭据被拒绝。

## 实际验证

- `pnpm exec tsc -p apps/web/tsconfig.json --noEmit`：通过。
- `pnpm exec vite build --config apps/web/vite.config.ts`：通过。Vite 仍提示现有小屋 chunk 超过 500 kB；本任务没有修改小屋。
- `pnpm exec playwright test --config apps/web/playwright.config.ts settings-subpages.spec.ts external-connections.spec.ts access-settings.spec.ts shell.spec.ts --workers=2`：桌面与手机共 29 通过、1 个既有桌面专属跳过。最后补充双向保存断言后，仅重跑受影响 `settings-subpages.spec.ts`：6 通过。
- `pnpm exec playwright test --config apps/web/playwright.config.ts integration-pages.spec.ts --grep 'external HA setup' --workers=2`：2 通过，保存与只读检测在桌面和手机均可用。
- `pnpm exec playwright test --config apps/web/playwright.bots.config.ts`：1 通过，使用仓库的隔离本地合成后端，验证创建、一次性凭据、启停、轮换与心跳。首次运行在停用后立即调用心跳得到 200；页面点击返回不代表异步停用完成。用例等待“已停用”状态后检查 403，重跑通过，未改后端。
- `git diff --check`：通过。依赖安装使用本机 `pnpm install --no-lockfile --ignore-scripts`，未写入锁文件；浏览器、服务数据均为隔离夹具。

## 视觉证据与未验范围

- 桌面连接总览：`apps/web/test-results/settings-overview-desktop.png`。
- 手机机器人空态：`apps/web/test-results/settings-bot-empty-mobile.png`。截图是忽略的本地测试产物，使用合成会话和状态。
- 未接真实机器人账号、群/私聊、AssetLink、Home Assistant 或生产部署；网页仍需部署方先登记机器人及允许会话，没有后台自助登记接口。真实插件收发和设备操作待总控按实际对象验收。

## 下一步

- 总控审查本分支完整 diff 和截图，按顺序合入协调检出；如需网页直接添加机器人账号，需另设后台登记与授权任务，再设计前端向导。
