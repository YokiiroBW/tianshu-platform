# 首次创建和配置账号接管

本模块只持久化一个家庭管理员的网页登录凭据。内部服务身份、权限、来源、模型供应商和令牌仍由服务器配置登记；网页不能提交 principal、角色、内部 token 或数据库路径。

## 配置

全新安装设置：

```json
"web_account": {"mode": "create", "setup_token_env": "TIANSHU_SETUP_TOKEN"}
```

此时 `web` 必须省略 `username` 和 `password_hash`，其余 origin、principal、input_entries、static_directory 等原有配置仍必需。只支持显式新安装，不能将已有配置账号改成这个模式来匿名重设。安装凭据由部署提供环境变量，使用既有 secret 格式：24–4096 个 ASCII 非空白字符，建议高熵随机值；变量名称和实际值不能复用内部业务/就绪凭据。首次创建前凭据必须可用，创建后可从环境移除，配置字段保留。公开状态只返回是否需要安装凭据，从不返回凭据值或环境变量名。

已有配置账号改为：

```json
"web_account": {"mode": "claim"}
```

保留有效 `web.username/password_hash`。用户先用原账号密码登录，再输入新用户名、新密码和原密码完成接管。账号仓储提交后所有登录与 models/home/access 密码解锁都使用新 hash；旧配置密码不再作为兜底。未启用 `web_account` 且没有账号持久文件的原部署行为不变。

## HTTP 合同

既有 `GET /api/web/session` 只在启用本模块时增加：

```json
"onboarding": {
  "state": "create_admin",
  "credential_source": "setup",
  "setup_token_required": true
}
```

| 场景 | state | credential_source | setup_token_required |
| --- | --- | --- | --- |
| 显式全新安装，尚无账户 | create_admin | setup | true |
| 未登录，有部署账号 | sign_in | deployment | false |
| 已验证原配置账号，待接管 | claim_admin | deployment | false |
| 未登录，已有持久账号 | sign_in | account | false |
| 已验证持久账号 | ready | account | false |

未认证响应不包含用户名、角色或来源列表。已认证的 `username` 来自当前有效仓储；原有 conversations/dialogue 状态继续反映实际来源授权及模型可用性，账号创建成功不表示模型或其他服务已经配置。

- `POST /api/web/setup`：`{username,password,setup_token?}`，仅显式新安装且无账户时允许。省略或错误安装凭据为 401。
- `POST /api/web/account/claim`：`{username,password,current_password}`，仅已登录旧配置账号允许，并再次验证旧密码。
- 两操作均复用已有匿名/认证 Cookie、CSRF、精确 Origin/Host、JSON 请求体、16 KiB 上限及登录限流。未知字段（例如 principal）拒绝。用户名为去首尾空格后原样的 1–128 字符且无 ASCII 控制符；密码 12–256 字符。
- 成功均返回 `{authenticated:true,csrf}` 并轮换 Cookie；前端随后重新 GET session。新凭据提交后旧会话失效。其他进程的旧会话通过每次重读的凭据来源/版本/摘要指纹失效，重启不恢复会话。

新增业务错误闭集：`setup_required` 409（未创建账号时尝试普通登录），`setup_closed` 409（创建/接管已结束或该模式不允许），`setup_unavailable` 503（待初始化时安装凭据不可用），`account_unavailable` 503（账号存储缺失、损坏或不一致）。其余沿用 `unauthorized` 401、`session_expired` 401、`forbidden` 403、`invalid_input` 400、`too_many_requests` 429，以及既有请求预算/超时/依赖错误；错误不带请求内容。诊断事件沿用安全闭集映射，不记录密码、hash、安装凭据或请求正文。

## 持久性、并发与恢复

只在显式创建/接管 POST 中创建文件，启动、状态查询、探针、preflight 都只读。数据文件为 `database_path + ".web-account.sqlite"`；防止库丢失后重新开放初始化的封条为 `database_path + ".web-account.sqlite.sealed"`。二者与主数据库位于同一持久数据卷，必须一起备份和恢复。SQLite 使用单例唯一键、`BEGIN IMMEDIATE` 和 `synchronous=FULL`；封条先 fsync，之后事务提交，再返回成功。没有明文密码。

两个独立进程同时提交也只能创建一个管理员；竞争失败可能返回 409，也可能在观测到尚未完成的磁盘状态时保守返回 503。不要将所有并发失败都假定为 409。回复丢失但事务已提交时，刷新状态并用新密码登录；不得重新开放初始化或恢复旧密码。

只有两个文件都不存在才是未初始化。已有封条但库丢失、已有账户但封条缺失、空库/空表、未知版本、损坏数据都拒绝服务，避免重新开放注册。中途断电或磁盘失败可能留下不可用的未完成文件，需显式离线检查/恢复，接口不自动删除或重建它们。完整卷连同封条同时被删除无法与真正新卷区分，必须依靠部署备份管理防止；本模块不声称能恢复丢失数据。

已初始化后移除 `web_account` 配置也拒绝启动，绝不因此复活 `web.username/password_hash`。回滚不支持只换成不认识本模块的旧二进制、删除账号文件或回滚旧设置；应保留理解本账号格式的版本，并恢复一致的代码/配置/数据备份。本轮没有提供密码找回、多用户、邀请注册或在线重置。

真正未初始化且安装凭据可用时是可访问初始化页面的有效运行态，不因没有账号库而使 readiness/部署 guard 失败；损坏的持久状态则置 sidecars 失败。preflight 不创建库，仍保留主平台数据库首次启动 `requires_initialization` 的既有语义。
