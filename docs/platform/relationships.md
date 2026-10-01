# Platform 角色—人物关系管理（TS-116）

## 正式发布绑定（2026-10-01）

隔离发布绑定协调仓库 contracts/role-relationship/v1 1.0.0（根发布提交 37b086f66ca8521ec065578b9126111d97d0f112），schema LF SHA256 e96397bac2b6ad8ff9d23c023d7d3c5ba0701734b27053a05b9d0f65a7ff8ee6。配置键 candidate_schema_path 保留兼容名称，值只指向正式版本。测试固定 Memory 1f3121c9758faeb31fc9d0fe2a54974c72505d55 与 Companion 21e4ff37f1d4f4e3e9db94f8c965031a8dcb9cd6 的 Git 归档；结果见 docs/handoffs/release-2026.10.01-rc.1.md。下文候选状态、旧固定 SHA 和此前结果保留为任务历史。发布绑定没有扩大权限，也不表示 NAS 已迁移或完成真实使用验收。

## 原任务记录（示例路径和哈希已指向正式包）

2026-10-01。本功能已完成隔离本地实现与验收；尚未合入产品 main、正式发布合同或部署。Memory 唯一拥有 `(actor_id, person_id)` 的关系、分数、冻结时间及事件账本。Platform 只验证后台会话、角色及人物选择，转发操作并显示脱敏结果，没有第二套分数表或关系数据库。

## 页面及模块

陪伴 → 文字对话 → 关系管理。先选择获准角色和已确认人物，再查看关系类型、称呼、好感阶段、分数、冻结状态与最近 20 条原因历史。人物使用现有 QQ 身份目录的有界分页，每页 100；未确认人物不可凭昵称或聊天正文创建关系。关系类型不授予管理员、工具、角色或机器人回复权限，也不随分数自动变成伴侣。

`apps/web/src/features/companion/relationships/` 分离网络 DTO、请求生命周期、页面、表单、历史及局部布局；`CompanionPage.tsx` 仅懒加载入口。服务端 `relationships/` 分离配置、共享 schema 校验、TLS 适配器、用例与同源边界。`service.py` 仅装配与凭据登记；`server.create_console` 是默认应用和正式 `__main__.serve_forever` 共用的控制台工厂，公共 HTTP 与内部 TLS 监听共享同一真实会话所有者。

## 显式装配和授权

缺省不启用。以下只展示配置字段，地址、CA 和环境变量必须由部署者分别登记；示例没有可用凭据。

```json
{
  "web_relationships": {
    "enabled": true,
    "candidate_schema_path": "/contracts/role-relationship/v1/schema.json",
    "memory": {
      "base_url": "https://memory.example.invalid:9443",
      "token_env": "RELATIONSHIPS_MANAGER_TOKEN",
      "ca_file": "/certificates/approved-ca.pem",
      "timeout_seconds": 5
    }
  }
}
```

启用前必须已有 `web_memory` 和 `web_qq_profiles`，三者指向同一 Memory 地址，并分别登记读取、人物目录及管理凭据；管理凭据不得与其他业务凭据同值。配置 schema 按 LF 归一化后固定 SHA256 `e96397bac2b6ad8ff9d23c023d7d3c5ba0701734b27053a05b9d0f65a7ff8ee6`。这是显式隔离候选输入，不能视为正式发布或生产启用授权。

实际操作人来自已认证控制台 principal，须同时有既有 `role.manage`、`memory.read`、`qq.admin.view`，还须满足现有 Memory 查询证明、当前角色版本/能力和 QQ 管理目录检查。登录和角色类型不自动增加权限。Memory 管理调用方需自己的 `relationships.read/manage`、`role_admin` 和实时 Platform origin；跨人物私密查询仅为获准后台 `managed=true`，不用于群上下文。

## 同源 API 与内部合同

所有页面端口均是 `POST /api/web/relationships/<operation>`，只接受真实 Cookie、同源 Host/Origin、CSRF；拒绝浏览器 Bearer 和非同源请求。JSON 正文上限 16 KiB、读体期限 5 秒，响应沿用 no-store。

| operation | 页面正文 | 结果 |
| --- | --- | --- |
| catalog | `{}` | 当前获准角色、第一批真实人物、下一页游标 |
| people | `{after}` | 下一批人物及下一页游标 |
| view | `{role_id, role_version, person_id, people_after}` | Memory 当前私密投影与有界原因历史 |
| manage | 选择字段，加 `{client_id, command}` | Memory 已确认的私密投影 |

`command` 只有 `set_binding`、`set_freeze`、`adjust_affinity` 三种。页面提交 `expected_version`，人工调整为整数 -100..100 并要求非空原因，称呼最多 40 字符、原因最多 200。页面不能提供 pair、request_id、operator、origin、权限或源引用。服务端按真实 principal 和 client_id 构造稳定管理 ID，从当前角色/来源签发 origin，再通过实际 TLS 调用 Memory `/internal/v1/relationships/history` 或 `/manage`。

客户端显式校验 CA/主机名，关闭环境代理和重定向；总时限默认 5 秒，响应上限 32 KiB。校验封闭信封、request_id、schema、精确 pair、时间字段、冻结一致性和最多 20 条封闭历史项；上游正文/错误详情不穿透页面。响应返回前再次验证会话、配置/凭据、principal、角色 scope 及已签发来源。

## 并发、取消和隐私

CAS 冲突要求重新读取后由操作者明确修改。同键同内容由 Memory 幂等重放，同键不同内容为冲突；Platform 不在失败后自动重发管理调用。可能已发送的超时、断连、非法回执或上游 5xx 是 `result_unknown`，不能称保存成功。页面暂停写操作，显式刷新真实所有者后才允许新的人工意图。

取消草稿只还原未提交表单；取消等待只终止页面等待，操作可能已提交，必须刷新核对。角色/人物切换、离开页面及重新登录会取消旧请求并推进请求代次；迟到结果还须匹配当前 pair 才可显示。临时来源选择即使在线程完成前被取消，也会在完成后清除其临时 entry。权限/会话失效的响应清除私密投影和历史。

冻结仅影响当前角色—人物对的自动正负变化与自然衰减；解冻不补扣、不补放冻结期间事件，短期情绪仍由 Companion 正常变化。管理历史只显示日期、实际增量、固定类型/结果和人工原因，不显示源原文、服务凭据或内部来源引用。群聊表达由 Companion 的 PublicProjection 约束，不公开私聊称呼、关系类型或分数。

## 验证和交付

真实本机三服务使用固定 Memory `ba03202c44648a3820f7552629edd9eb5797d424`、Companion `ea5c044719bfa76de384b0b69bbfd7ed1439996b` 和本任务 Platform；模型、QQ 账号和发送渠道为合成替身。浏览器只走 `127.0.0.1` HTTP，内部 RPC 为临时 CA 验证的 HTTPS；不安装证书或绕过安全校验。

具体命令、实际结果、历史基线失败和截图路径见 `docs/handoffs/TS-116.md`；固定最终三 SHA 清单由根 `docs/development/role-relationship-affinity-delivery-2026-10-01.json` 保存。完整 Platform 回归没有全绿，不把专项或截图代称全量通过。生产首次关系迁移、alias/角色 sidecar 一致备份恢复、NAS 版本选择、真实 QQ/模型、长期容量与橙汐原账号绑定均未完成，必须另作集成和部署计划。
