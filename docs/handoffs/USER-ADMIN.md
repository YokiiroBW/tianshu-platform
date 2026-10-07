# USER-ADMIN 用户详情管理员开关

- 用户授权：在用户界面直接选择是否设为管理员，删除重复的设置表单。
- 基线：0a3e50c97a9403611e79995b1c74a9b31d06c490；分支 codex/user-admin-toggle-20261007。
- 实现：PersonDetail 身份栏挂载 AdminToggle；复用 integrationPost、qq-admin/view/grant/revoke、既有版本检查和服务端认证。没有后端、数据库或能力范围变更。开启使用既有 identity.explain 能力及已接入角色/会话范围；已有精确范围授权保持原样并标注限定范围。
- 删除 QQAdminPanel 和专属样式，移除设置目录和导航入口；旧 settings/7 地址解析到用户档案，技能和天气地址保持不变。
- 读取失败、拒绝写入和版本冲突显示状态未确认，刷新读取后才允许再次修改；请求取消避免换用户后旧结果覆盖。写操作成功以服务端返回为准。
- 验证：TypeScript noEmit、Vite build、8 个修改文件的 Prettier 检查、git diff --check 通过。用户页既有 6 个用例桌面/手机共 12 项通过；新增 2 个用例桌面/手机共 4 项通过（保存、撤销、刷新持久状态、用户隔离、版本冲突、读取失败、403、限定范围、旧地址与导航、移动宽度）。首轮新测试遗漏选择用户且使用立即断言的 checkbox check API，已修正为选择用户、点击并等待异步服务端状态；仅重跑 4 项新测试。
- 截图：apps/web/test-results/people-user-administrator--28775-ers-and-refreshes-conflicts-{desktop,mobile}/user-admin.png，已人工查看。
- 环境：npm 不在 PATH，直接使用 node 运行同等 tsc/vite 命令；依赖通过 node_modules junction 复用基线检出。Vite 现有 renderer 大包提示不阻塞构建。
- 联调限制：已更新 qq-admin.spec.ts 到开关交互，但隔离 HTTPS fixture 启动被根合同 published contract hash mismatch 阻断，未执行真实服务浏览器验证，没有修改合同绕过校验。
- 状态：仅本地实现和验证；未集成、未推送、未部署、未修改真实管理员授权。后续集成此分支并在有效合同夹具/部署环境复核 HTTPS 保存与撤销。
