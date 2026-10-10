# MODEL-FUNCTIONS 本地交付

日期：2026-10-10。用户要求默认几个功能，各自可选择不同模型。

## 状态与基线

已完成本地实现与定向验证，尚未提交、集成、推送或部署。

- Platform 基线 `6a2ed3d0d6f6b3f0be6761c3b9cf61239ea5d2b4`。
- Companion 基线 `a5725d04aba00762d66306ae4821a40b70f7e2ed`。
- 两者均在 MODEL-FUNCTIONS 独立 worktree，分支 `codex/model-functions-20261010`。
- 原 projects 检出比上述当前候选旧，且存在原有未跟踪 build；既有分配脚本只能从 projects HEAD 分配，不能选择当前候选。因此沿用此前固定提交创建隔离检出的做法，未重置、清理或修改原检出，未新开聊天窗口。

## 实现与复用

新增六个功能选择：主对话、工具处理、代码、网页搜索、日常与日记、记忆整理。网页选择、保存、刷新、恢复继承；主对话直接复用原 default 指针，所有旧调用方同步看到默认变化。其他项通过既有 ProviderCatalog 和 receipts 持久化，新增附加表前备份旧库。

复用原供应商目录、管理员会话、管理租约、版本校验和网关配置发布。ProviderAuthority 按 function_id 选择；Companion 将生活/日记生成接到 writing 功能，其余网关传输不变。功能状态由服务端目录统一给网页，页面不自行虚构已接入状态。

实现和协议细节见 [功能分工说明](../platform/model-functions.md)。配套陪伴交接位于另一个检出的 docs/handoffs/MODEL-FUNCTIONS.md。

## 实际验证

- 平台新功能后端 5 项通过：真实同源 HTTP 登录/管理权限、保存与读回、CAS/幂等、持久化和升级备份、默认同步、角色覆盖、固定 grant 和按功能读取真实模型配置。
- 原 ProviderCatalog 21 项通过；未改动后重复运行。
- 陪伴相关 68 项通过，分批结果：model selection / life / 新 function selection 共 40 项，daily life 28 项。包括实际生成参数选取和显式重试新 grant。
- TypeScript 类型检查和 Vite 正式构建通过；保留既有 renderer chunk 大于 500 KB 提示。
- 新同源真实平台浏览器场景：桌面、手机各 1 项通过，含默认设置同步、独立配置、刷新保留、恢复继承、无横向溢出；已查看两张截图。
- 变更 Python Ruff 检查及前端格式化通过；git diff --check 通过。

最初平台升级测试的临时 SQLite 连接未关闭，及前端未使用 import 已修复。首轮浏览器因 select 的包裹 label 含选项文本导致按标签定位失败，已改为显式 htmlFor/id，重跑通过。

最初使用旧根合同缺少 life-read，daily life 的四项失败；直接使用 coordination 合同则基础 manifest 与产品固定哈希不匹配，不能绕过验证。最终测试夹具使用根已有匹配发布包，并从 coordination 固定提交 `1389d5c2b5d20a4292ad2e4a97096229c5aad955` 补齐 life-read 等缺失包，放在 MODEL-FUNCTIONS/.runtime/contracts。生活读取 manifest 为 `7be7507d58f897a739b269de3c096ba948c92b25266888a91fa50130d342c551`，基础文字包为 `81e6cc4ddef7c6f82e055d4cb04b090db036dd5c52763473ce697aa02db478a1`；产品原哈希校验正常通过，未改写源合同。

截图：apps/web/test-results/model-functions/ 下 desktop/mobile 两个目录的 model-functions.png。

## 限制及下一步

工具、代码、搜索、记忆整理已能单独保存及经内部端口选择模型，自动任务消费者尚未接入；主模型先短回应、后台辅助执行、主模型二次回复仍待单独编排。长篇作品保持原静态配置，不冒称全部写作消费者已切换。

本轮没有真实模型调用、NAS 操作、QQ 消息、生产迁移或实际账号配置。后续集成需将文档中的候选协议增量正式纳入共享合同，先更新平台，再更新陪伴；网关无需变更。部署前使用匹配产品固定哈希的合同包，不直接使用上述已发现不匹配的 coordination 基础 manifest。
