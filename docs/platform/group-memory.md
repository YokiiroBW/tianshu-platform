# 群记忆与 BOT 自身记忆接点

群画像页面复用现有登录、Cookie/CSRF、角色目录和 MemoryPage。一个已登记群同时显示
Chat 原始互动与热度、Memory 已整理话题和共享画像，并可按需检索原始归档。
消息量、回复和提及只表示实际互动，不推断亲密程度；话题布局只作显示，不写回事实。

## 群读取配置

在现有完整设置中增加 `web_group_memory`。下例只展示新增部分，凭据仍从部署环境读取：

```json
{
  "web_group_memory": {
    "enabled": true,
    "chat": {
      "base_url": "https://chat.example.invalid",
      "token_env": "PLATFORM_CHAT_READER_TOKEN",
      "ca_file": "/private/ca.pem"
    },
    "groups": [
      {
        "id": "reading-group",
        "label": "读书群",
        "entry_id": "web-reading-group",
        "conversation_id": "conversation:reading-group",
        "chat_channel": {
          "namespace": "qq",
          "binding_id": "onebot:reader",
          "channel_conversation_id": "group:42",
          "thread_id": null
        },
        "archive_mapping_id": "onebot:reader:42"
      }
    ]
  }
}
```

`web_memory` 沿用现有 Memory HTTPS peer 和角色读取配置。每个群的 `entry_id` 必须是当前
Web 操作员拥有的 `local_operator` 登记，使用操作员真实 WEB account 和 WEB channel，
`audience=group`，并且只有一条 `platform → memory / dialogue` 读取路由。该登记的
`actor_id` 是对应角色；可为不同角色分别登记群。操作员仍需现有 `memory.read` 权限。

`chat_channel` 是服务器配置的真实 QQ 渠道。Origins 用现有当前渠道映射查到其 conversation，
再与配置的 `conversation_id` 比较；不拿配置中的 conversation 当映射缺失时的后备值。
登记 WEB 身份保持真实，不冒充 QQ 账号。Memory 的 browser reader 必须明确允许这个 WEB
账号和精确群 scope。角色失效、入口撤销或渠道映射变化后，正在等待的结果也不能返回。

Chat 的独立 evidence reader 需要允许该真实账号和群。Memory 的同一 browser reader 下
`archive_mappings` 必须登记对应 `archive_mapping_id`，将真实 channel、平台、自身账号、
room 和 physical conversation 绑定到同一群。客户端不能选择 mapping、账号、渠道或服务 URL。

历史结果先校验 Chat locator/完整内容哈希，再校验每个实际 native alias 的 Memory 已知遗忘状态。
任一 alias 已明确遗忘即过滤整条；全部 alias 不受支持也过滤。可信映射下尚无 Core 对应的导入
历史可以使用，但不伪造 Core-current 证明。Memory 不可用时不返回未经遗忘检查的原话。
邻近消息分别过滤，目标失效时不保留旧目标文本。

固定浏览器接点位于现有 `/api/web/memory/`：`groups`、`group-insights`、`group-history`、
`group-evidence`、`group-records` 和 `group-topics`。浏览器只提交角色版本、登记群 ID 和查询条件。
Cookie/CSRF、当前角色及 scope 在远程等待前后校验，服务凭据和 origin 留在服务端。

统计窗口为 24 小时、7 天或 30 天，按 UTC+08:00 固定偏移分桶，一次最多扫描 10000 条原始消息。
未解析 reply/@、全体提醒、缺失互动结构和扫描覆盖单列。首版按物理消息计数，无法由时间接近
推断一个 BOT 多段发送只算一次。话题、画像和原始统计分别呈现不可用与空结果。

## BOT 自身记忆的 Platform 权威

`POST /internal/v1/actor-self-memory/authority` 接受候选合同中的 `actor_check` 和
`context_check`。固定 Memory/Companion 服务主体需有 `actor.memory.authorize`。
操作范围来自当前角色的 `memory.read` / `memory.write`，版本来自 Platform RoleRuntime；
Companion、Memory 各自的角色版本不能替代它。

`actor_only` 只表示当前角色内部可读，不授予向外发布。群聊或私聊 context 必须持当前
`companion → memory / dialogue` origin，真实 actor、conversation、audience 以及私聊 person
均匹配。Platform 仅证明身份和当前上下文，实际阅读及其区间由真实内容/动作 owner 证明。

## 后台模型身份

ProviderAuthority 支持固定 `companion` 与 `companion_background` 两种调用者。
`config.select` 凭据的实际服务身份必须等于请求中的固定 caller；后续 Gateway
`provider.runtime` 读取必须匹配同一 turn、版本、caller 和 workload。不能通过请求头或请求体
把前台凭据改称后台。后台使用独立服务凭据、固定 background Gateway/selector 实例，Gateway
workload binding 决定排队类别；沿用原槽位、预留和时限。

这些接点使用本批固定候选合同及既有用户 rev6。当前是本地候选；生产启用必须单独配置真实
reader、角色、mapping、后台身份和 actor 独立存储。组件验证不表示已连接生产聊天库或已上线。
