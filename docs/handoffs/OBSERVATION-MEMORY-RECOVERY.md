# 原记忆队列恢复的管理入口对齐

基于远程 main `0a691eb8976cb64b595a1564bb6437463294c89e`，运行代码与 R28 `519d5f0` 相同，仅此前交接文档不同。

复用已有 `/internal/v1/memory/learning/recover`、原 operator `role.manage` 和当前角色 `memory.write` 检查、原 Platform→Companion 客户端身份。`model_request_id` 允许 null，对齐 Companion 从未发起模型的 receipt 阶段恢复分支；非 null 仍按原 ID 合同校验。是否确实没有提交模型、原任务/账本/CAS/幂等约束仍由 Companion 负责，无新接口或权限。

R28 原 Linux 镜像内，以当前候选完整服务源码和既有合同隔离运行 `test_memory_learning_management`：6 项通过，1.306 秒。覆盖 null 原样转发、权限拒绝、非法非 null ID 拒绝、原命令和丢失响应幂等边界。无生产挂载、无依赖安装、无模型/QQ 调用。Windows 两个现有解释器缺 aiohttp，未修改共享环境；改用镜像内现有依赖完成有效检查。

本候选由记忆窗口统一与 Memory、Companion 和监控候选串行推送部署；本提交不表示已部署。
## 原分段恢复转发

同一原 memory/learning/recover 入口新增与job_id排他的segment_id选择，仍只允许原角色operator的role.manage及memory.write。分段恢复的model_request_id必须为空；响应严格回显原segment/请求，并校验Memory真正job标识，不接受错绑或畸形回执。旧job请求/回执形状、鉴权、未知响应不重试保持。原生HTTP管理文件7项通过，含原有6项和分段转发、拒绝双selector/未知模型、错绑回执/非法job ID场景；无生产挂载、模型或QQ调用。

# 2026-10-30 批量来源校验的 HTTP 大小边界

R33 自然诊断为 PlatformVerifier 收到413；按原 job 的现有 collect() 完整范围进行真实只读校验，20条来源无重复但返回 budget_exceeded。server.py 把已支持批量的 segment/segment_resolve 仍限制为旧单条4096B，Memory再包装为依赖503。

仅 OBSERVATION_VERIFY 的这两个既有操作按 Memory 原262144B边界接收，来源数量/schema/鉴权和业务验证仍由原owner执行；其他观察操作及其他端点限制保持，无配置、权限或DDL变化。新增原生HTTP边界用例先复现413，修复后合法大集合转交、超256KiB拒绝、旧ingest 4KiB和401拒绝共5场景通过。原隔离R28镜像、无生产挂载、无安装依赖、模型或QQ调用；只格式化了该新测试文件。发布后核同一原20来源证明与自然队列，不重新生成旧unknown任务或模型输出。
