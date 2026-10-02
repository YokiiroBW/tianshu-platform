# 角色独立日常读取

“陪伴 → 生活与日记”（`/#/companion/1`）沿现有同源会话、CSRF 和 `WebReader(life)` 读取 Companion 的持久投影。账号需要现有 `life.read`；服务读取身份及角色范围仍由 Companion 的 `life_readers` 和 `life_access` 双侧核对。网页不创建计划或推进时钟；失败内容可显式重试。

- `POST /api/web/life/today` 接受 `{actor_id}`，返回当日计划、当前阶段、时区、运行状态和内容生成状态。安排与已经发生的经历分开展示；模型不可用时明确显示基础作息。
- `POST /api/web/life/timeline` 接受 `{actor_id, day, limit, after}`。网页每页读取 20 条，最大 50；游标为 `{position, known_id}`，完整传回 Companion。日期表示素材获知后归档的角色当地日期；事件的发生时间与获知时间分别保留。
- 原 `actors/snapshot/diaries/revision` 端口及已发布日记指针核对语义继续使用。新协议为协调仓库 `contracts/life-read/v1`。
- `POST /api/web/life/retry` 接受 `{actor_id, plan_id, phase_id, expected_version}`。`phase_id=null` 重试当日安排，非空重试当前阶段；版本取自刚读到的计划。沿原 `role.manage` 与 `life.read` 管理权限，先后分别通过独立生活读服务核对该角色可读，再以现有 Core 管理身份请求重试。只返回受理回执；网页刷新读投影确认进展。版本冲突或权限撤回明确报错，不宣称已生成。

角色与会话变化会清空投影并取消旧请求；晚到的旧角色响应不能写回新角色。刷新日常读取后台已经保存的安排和经历。读取失败明确显示错误，不当作空经历。

“陪伴 → 角色管理”分开设置“启用角色”和“允许对话”。启用但不允许对话的角色可在没有模型配置时运行基础生活；对话仍须现有模型配置及 Core 的角色能力检查。停用角色暂停日常；已保存经历保留。生活是否发生与机器人是否发消息分别归属原有模块。

模型仍在现役模型管理发布，并由角色管理选择；未选择时使用已发布默认模型。Companion 自主内容生成默认启用，按角色通过现有 ProviderSelection / Gateway 获取租约，不要求聊天绑定或 `dialogue` 能力；显式 `life_writing=false` 才关闭生成。没有可用模型时保留基础作息并返回 `unavailable`，不需要用户填写第二份生活模型或 `life_config_version`。

部署读取身份必须与 Core 管理 token 分离。Companion 可以在已配置的独立 `life_readers` 项中设 `runtime_roles:true`，让已受理的运行角色首次启用时安装该 reader 的生活读授权；旧静态 `actor_ids` 和已存在的撤回记录保持原语义。Platform 不扩大 actors 返回范围，只给已经获准返回的本地角色附现役角色管理名称用于展示。

本地验证使用隔离数据库和合成模型端点。联合用例经过真实 Platform / Companion / Memory / Gateway 生产代码和 TLS，浏览器新建角色、读动态授权、计划失败、显式重试、生成落库、停用暂停；未手工登记新角色或推进后台时钟。部署后零对话、网页关闭、经过更多真实时间节点的生活验收仍应单独执行。
