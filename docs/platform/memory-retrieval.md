# 记忆检索设置

入口：任务与设置 → 记忆检索（`#/settings/10`）。

先在“模型供应商”保存一次地址与 Key，并在其下添加类型为嵌入、重排序的模型；在本页分别选择具体模型并保存，共用供应商连接。原独立连接仍可使用，在“原独立连接设置”中维护；替换为供应商模型引用后不再保存一份重复密钥。保存后可用固定合成短句测试连接。再选择关键词、向量或混合检索，独立启停重排序，候选数量可设 1–100。使用向量或重排序前须保存对应连接；不会继承聊天模型。

管理员权限复用现有登录、CSRF、`external.manage`。共享模型的密钥由 `ProviderCatalog` 唯一加密保存，`ExternalCatalog` 只保存模型引用和检索策略；旧独立连接密钥仍由原目录管理。浏览器只获得密钥状态，供应商设置支持留空保留、更换和清除。版本并发控制复用目录 CAS，保存冲突时刷新后再修改。部署未配置加密目录时显示不可管理，不伪造连接成功。

`services/platform/memory_retrieval.py` 拥有策略、脱敏视图和网关配置读取。复用 `ProviderManagement.gateway_request` 与既有私有网关测试通道进行连接测试；运行时 Memory 直接访问 Gateway，平台只给 Gateway 返回策略和精确模型连接，不转发真实记忆文本。

内部 `POST /internal/v1/memory-retrieval/execute` 只允许持有 `memory.retrieval` action 的 Gateway 服务身份：

- `{ "operation": "settings" }` 返回 mode、rerank、candidates、revision、embedding_revision。
- `{ "operation": "runtime", "kind": "embed|rerank", "revision": "当前策略指纹" }` 返回对应 connection；秘密仅在受信内部响应中存在。旧 revision 返回 409。

新功能不修改旧 text-dialogue/model-protocol 合同，也不改变聊天、日记或自动记忆整理开关。新网关版本和 Memory 配置需配套上线；本地代码交付不代表线上已启用。

模型上游协议参考：[浮点嵌入](https://api-docs.siliconflow.cn/docs/api/embeddings-post)、[重排序](https://docs.siliconflow.cn/docs/api/rerank-post)。没有绑定指定供应商或模型；此处接口兼容性需通过目标提供商测试确认。

共享模型与数据迁移见 [供应商与多个模型](provider-sources.md)。模型条目修订参与检索快照及嵌入空间指纹；变化期间的旧快照拒绝执行，不混用旧向量。
