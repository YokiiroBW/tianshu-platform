# 直连入站排队与确认恢复

观测主循环在每批入站前、每处理四条消息后穿插既有出站队列，避免已生成回复一直等在二十条慢请求之后。尚未完成启用的回复连接不阻断其他连接投递。host_pending / host_dropped 通过受管理版本保护的 catalog.observe 落盘，在慢批次开始前就显示实际积压。

注册来源与准备映射发生在 Core 调用之前。这些阶段的临时故障带明确 source_not_started 证据，才允许原事件重试；发出后的不确定异常没有这项标记。来源授权复用也检查剩余期限，避免重试时拿到即将到期的 actor origin；重新签发仍经过当前注册、路由及撤销检查。

结果未知时，查询 Core 的 `/internal/v1/conversation/ingest-receipt`，只发送原服务幂等 key 和 fanout 语义摘要。只有正向持久回执允许重走原授权、原输入和原 key，以补齐映射及确认；此时 Core 复用原 admission，不重新生成。缺失、查询失败或响应不匹配都维持 unknown。精确字段在 Companion 的 docs/ingress-receipts.md，接口不返回聊天正文。

查询前后均沿用当前连接、账户、角色与来源核验，停用或撤销不会被恢复绕过。原 NoneBot/AstrBot 出站 unknown 处理不变；本轮没有新增发送出口、真实 QQ 测试或生产配置变更。
