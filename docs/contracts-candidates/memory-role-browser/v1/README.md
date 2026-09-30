# 记忆浏览角色选择候选 v1

本候选仅描述同源网页到 Platform 的窄增量，不修改已发布的 Platform–Memory 来源或 Memory 浏览请求。浏览器会话仍需当前 `memory.read`；Memory 继续拥有记忆事实、来源屏障和最终读取授权。

## 部署门控

- Platform 的 `web_memory.runtime_roles: true` 显式启用动态角色选项；缺省 `false`，只保留旧 `entry_id` 对应的静态角色。
- Memory 当前 `callers.platform` 保留 `allowed_actors` 静态白名单和 `operations: ["browse"]`，另设 `allow_runtime_roles: true`；Memory `browser_readers.platform` 保留原 `account`、`actor_id` 和完整 `scopes` 模板，另设 `allow_runtime_roles: true`。这两个门控缺一不可。
- Memory 既有 `role_grants_database_path` 必须指向角色管理使用的持久 grant 库；动态 actor 还须当前精确 grant 为 enabled。`allowed_actors` 不应改为通配或罗列全部角色。现有静态角色若被 grant 接管并停用，也会被拒绝。
- 未部署时不改 NAS 配置；由总控对照当时实际配置审查和实施。

## 同源网页请求

`POST /api/web/memory/state` 的旧字段保留，并增加 `roles`。每项仅有 `id`、友好 `label`、整数 `version`、`available`、`reason`。目录来自当前登录 operator 的已管理角色；停用、待核验和未开 `memory.read` 可显示原因但不可读取。旧默认 `entry_id` 角色始终出现在目录第一项，不从其他账号或全局管理目录补选。

`POST /api/web/memory/{overview,subjects,records}` 可增加精确 `role_id` 与 `role_version` 两字段，必须同时提供；值必须对应当前目录和版本。未知角色、显式 null、停用、待核验、未开 `memory.read` 一律失败，不回退到默认角色。完全省略两字段仅为旧单角色客户端兼容，选用当前静态默认角色。其余分页/subject 字段沿用原网页请求。

Platform 从 `web_memory.entry_id` 的当前 operator、account、person、audience、conversation、route 模板派生临时角色来源，仅替换经过目录与版本核验的 actor；每次请求签发新的 origin，并在上游响应后再次核验角色版本、来源及会话。浏览器不能提交 scope、account、person、conversation、origin、URL 或服务 token。临时来源在请求结束后移除。

Memory 在原 browser registration 的精确 `account` 与完整 `scope` 模板内，只允许当前精确 grant 为 enabled 的动态 actor。所有概览、人物/群列表、详情、记录和游标沿用完整 actor scope 过滤；跨角色游标报 `invalid_input`。当前模板是 `self_private` 时，群专属 `group_only` 投影仍需另行获准的 group 范围登记，角色选择不扩大 audience。

网页选择按登录用户名隔离地保存在当前标签页，刷新及记忆子页沿用；退出登录删除。切换立即清空旧结果和游标并取消请求，旧响应不能覆盖新角色。可见页面每 15 秒及重新可见时复核读取，失败即隐藏既有内容；后台授权仍在每次请求中实时复核。
