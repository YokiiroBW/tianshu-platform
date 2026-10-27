# 统一宿主中的持久 SDK 地址

基于 `c17b70c`，为已有 `bind_owned_adapter(instance_id, session_factory)` 增加可选显式 `origin`。仅当宿主已绑定原持久 SDK instance_id 时，使用该模块固定 origin 调用原 RPC；保留 SDK key、path、请求/回执和既有持久 row 地址。外部 SDK、未知 instance、显式连接工厂的原检查不变，也没有放松 exchange 的 origin 核验。

解决已部署连接保存 NAS IP/公开别名而本地模块使用逻辑 origin 时的实际不匹配，无需重写目录、迁移权限或让用户重建 BOT 连接。原外部连接即使地址相同也仍走 DNS/pin/TLS。

`test_owned_bot_adapter` 集中 **7 tests / 2.314s / OK**，新增持久别名重基且 row 原值保持；包括原凭据拒绝、相同 URL 外部对象、origin mismatch 不回落、取消后真实 finally 和 SQLite 已入工作完成后关闭。未访问真实 SDK/QQ/NAS。
