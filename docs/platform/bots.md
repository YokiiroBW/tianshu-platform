# 机器人连接（BOT-P）

本页描述平台端部署与网页操作。NoneBot、AstrBot 插件需分别按各自安装说明安装到机器人宿主；网页只能管理已经在宿主安装且在平台/Core 登记的连接。首轮仅文本；未指定真实测试对象前保持停用，不向真实群或联系人发送。

## 部署登记

平台设置增加 `bot_connections`，并给网页 operator principal 加 `bot.manage` action。另建独立 `platform` service principal，仅有 `source.register`、`source.dispatch`、`mapping.prepare`，凭据环境变量留在平台服务端，不填写到插件。Core 发给平台的 `companion` principal 需有 `dialogue.send`。插件通过网页创建时拿到每连接一次性随机 token；它既不是 Core token，也不是平台管理员 token。

每个槽位精确登记 `adapter`、`platform_id`、`self_id` 和同一外部会话的一个或多个 `input_entry_ids`。每个 input entry 对应一个明确的作者账号，owner 为前述平台 service principal。未知作者拒绝；群成员不能复用群主的 account。每个关联 actor entry 必须有 `platform→companion`、`companion→memory`、`companion→platform` dialogue route。Core 对该槽位 `channel.binding_id` 的 binding 必须显式选 `service=platform` 与已登记角色；旧绑定不切换。QQ 外部会话键为 `group:<群号>` 或 `private:<用户号>`，同时写入 input entry 的 `channel_conversation_id`。Core 自己生成的 conversation ID 是另一标识。

[两种机器人登记样例](bots.settings.example.json)是**合成占位片段**：把占位 ID 替换为管理员明确选定的宿主实例、机器人账号、会话、作者和角色，并合并到现有平台设置与 Core 绑定。仅把片段原样复制到生产不会变成可用连接。改动设置前备份平台权威库与 `<database_path>.bots.sqlite`；首次启动创建 sidecar schema 1，未迁移原权威库。不要用首次安装工具覆盖现有服务。

## 网页流程

1. 登录“设置 → 连接”，在“机器人连接”中查看 NoneBot/AstrBot 槽位、外部会话与已登记作者数量。没有槽位会显示部署提示，不显示假在线。
2. 输入管理员密码解锁（900 秒）。选一个槽位和角色，创建连接；连接默认为停用。复制一次性 token 到**对应宿主插件的私有配置**，页面关闭后仅能轮换，不可回读。
3. 在宿主完成插件安装及配置后手动启用连接。`待配置或离线` 表示没有最近 90 秒认证心跳；`插件在线` 只表示心跳；`授权失败` 表示登记身份/来源权限已失效。最近有效事件时间来自已确认 Core admission，回复计数来自持久 sender 台账，不等于实机验收。
4. 撤权时在网页停用，或由部署方撤销来源 entry/principal。停用后新事件、领取均拒绝。已领取且可能发送的回复保守标 unknown；后到的同 attempt SDK 回执仍可如实记录（需恢复该连接鉴权），不能重复发送。

轮换 token 会立即让旧插件凭据失效。重新配置插件后继续心跳；旧 unknown 入站不自动重发。插件的 `events/status` 与 `replies/status` 只读核对，不能获得新的发送权。

## 端点与结果

最终首轮 HTTP 字段见 [BOT-HTTP-DRAFT](../handoffs/BOT-HTTP-DRAFT.md)；跨产品发布由总控负责。平台 `POST /internal/v1/conversation/send` 对 bot namespace 持久接收并返回 `unknown/retry_safe=false`，因为此时尚无原 SDK 确认。插件一次 claim 后先持久发送意图，再调原 SDK，随后 ACK。平台 `reply-status` 向 Core 返回已确认真实状态。Core 默认 30 秒内轮询；超过 Core 等待时间与平台最终收到 SDK 回执是两件事。平台台账保留晚到 `sent`，网页回复计数展示该事实；不会触发再次发送。

API 拒绝任意浏览器 Cookie/Origin 与管理员 Bearer；同源网页端点只用 Cookie/CSRF/Origin。日志不写 token、正文、作者或会话原始值。运行库在部署持久卷中，与平台权威库一起备份；本地测试使用隔离临时目录。

## 本地验证

设置 `TS012_CONTRACT_DIR` 为已发布 `contracts/text-dialogue/v1` 绝对路径后：

```powershell
.runtime/venv/Scripts/python.exe -m unittest discover -s tests/backend -p test_bots.py -v
```

`test_bots.py::bot_settings(tempdir)` 提供合成 QQ 群槽位（`group:123`、`user:1/user:2`、`actor:a`）。`Platform(settings)`、`create_app(platform)`、`fixtures.start_http(app)` 是真实 loopback HTTP 入口，可供 BOT-N/A 的 SDK 适配器做联合测试；`sources.dispatch` 在平台单元测试中是明确的合成 Core 响应，不代表模型执行。实机安装、SDK 版本、账号白名单和真实发送另由总控验收。
