# 原记忆队列恢复的管理入口对齐

基于远程 main `0a691eb8976cb64b595a1564bb6437463294c89e`，运行代码与 R28 `519d5f0` 相同，仅此前交接文档不同。

复用已有 `/internal/v1/memory/learning/recover`、原 operator `role.manage` 和当前角色 `memory.write` 检查、原 Platform→Companion 客户端身份。`model_request_id` 允许 null，对齐 Companion 从未发起模型的 receipt 阶段恢复分支；非 null 仍按原 ID 合同校验。是否确实没有提交模型、原任务/账本/CAS/幂等约束仍由 Companion 负责，无新接口或权限。

R28 原 Linux 镜像内，以当前候选完整服务源码和既有合同隔离运行 `test_memory_learning_management`：6 项通过，1.306 秒。覆盖 null 原样转发、权限拒绝、非法非 null ID 拒绝、原命令和丢失响应幂等边界。无生产挂载、无依赖安装、无模型/QQ 调用。Windows 两个现有解释器缺 aiohttp，未修改共享环境；改用镜像内现有依赖完成有效检查。

本候选由记忆窗口统一与 Memory、Companion 和监控候选串行推送部署；本提交不表示已部署。
