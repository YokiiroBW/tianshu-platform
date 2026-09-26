# PROVIDER-P1：持久供应商目录与私有密钥

基线 bc41c05808271dc03d643d567e43788978e1e587，分支 codex/provider-platform-20260926。范围仅独立后端模块、专项测试、本交接；未修改网页、HTTP路由、静态 Models 登记、根合同、其他产品或 NAS。

## 实现

`services/platform/provider_catalog.py` 提供 `ProviderCatalog(directory, create=False)`。只有显式 `create=True` 且目录原先不存在才初始化；已有目录、缺key/库、错误key、损坏密文均失败，绝不生成替代密钥或静默清空。加密采用 cryptography AES-256-GCM，随机nonce、provider_id作为AAD；密钥只进加密列，公共provider JSON和幂等回执不含密钥。回执参数摘要采用HMAC而非裸hash，避免离线猜测低熵API Key。

内部方法：

- `save(client_id, name, base_url, model_id='', protocol='openai-chat-completions', enabled=True, api_key='', provider_id=None, expected_revision=None)`：首次生成ID；编辑必须CAS。空白key保持原值；替换不回显。允许先保存供应商再读模型，空model不能测试/默认。
- `clear_key/delete(client_id, provider_id, expected_revision)`：独立操作；清除key增修订；删除、清key、任何编辑都清除受影响默认选择。
- `record_test(client_id, provider_id, expected_revision, outcome)`：仅受信执行器完成接点；只收固定状态码，不收正文、异常信息或模型列表。迟到旧revision结果拒绝。时间来自平台时钟。所有save产生新revision并清测试（名称修改也保守失效），不把旧测试重绑为新revision成功。
- `set_default(client_id, provider_id, expected_revision, expected_default_revision)`：供应商CAS与默认指针CAS双校验；要求该revision实际短回复测试成功、启用、有key和model。所有状态写入与回执在同一 `BEGIN IMMEDIATE` 事务中，多进程同键重放首结果；同键不同语义409。回执是历史动作结果，当前状态应另调view。
- `view()`：供应商去敏列表与默认指针。`default.configured` 只说明目录选择条件满足，**不是**配置已发布、授权生效或对话ready。
- `execution_context(provider_id, expected_revision)`：受信内部CAS读取，返回不可变 `ProviderExecutionContext(provider_id, revision, protocol, base_url, model_id, api_key)`。后三项repr隐藏；没有自动序列化成公共响应的方法。与G1同名字段已对齐，所有RPC鉴权/版本租期由后续合同解决。

URL仅HTTPS语法规范化，拒绝userinfo/query/fragment/控制字符和路径混淆；不查DNS、不连接、不声称目标已验证。网关必须独立DNS固定/TLS/地址策略。局域网HTTP连接类型不在首批实现。

## 备份、权限与依赖

私有目录含 `providers.sqlite` 和 `providers.key`；必须停止所有目录写者后整体备份/整体恢复，不热拷贝单文件。SQLite同步FULL，独立事务连接及时关闭。key写入fsync；部分初始化不自动续写。已有实例在每次事务复核key，库使用mode=rw，缺失文件不自动建空库。

POSIX新目录0700、文件0600；Windows必须由部署层在父目录设置仅服务/管理员可访问ACL，Python chmod不能替代ACL。目录不应纳入诊断包、公共静态文件或普通备份公开路径；需加密且权限受控的备份介质。未实现密钥轮换/在线备份/旧记录清理策略，SQLite旧页可能保留已删除的**密文**。

生产新增依赖 `cryptography==50.0.1` 由总控统一修改pyproject/lock，本任务遵照独占范围未改锁。缺包明确 `provider_crypto_unavailable`，不降级明文。实际专项运行解释器已安装50.0.1。

## 真实验证

在Windows隔离临时目录、合成密钥、真实SQLite上运行：

`C:/YOKI/Codex/tianshu-peiban-bot/.runtime/nas-a1-r1-venv/Scripts/python.exe -m unittest discover -s tests/backend -p test_provider_catalog.py -v`

19项通过：重启与完整拷贝恢复/回执、双独立实例CAS与同键并发、删除/停用/失败测试默认失效、key替换/空白保持/独立清除、过期测试拒绝、默认CAS、无model保存、CAS不可用None绕过、密钥不进公共投影/repr/磁盘明文、缺key/库/错误key/损坏密文拒绝、回执触发器失败原子回滚、加密失败无部分写入、URL语法。首次测试发现SQLite初始化连接未显式close导致Windows文件占用，已用closing修复且全专项通过。

ruff check及format --check只覆盖两个新增Python文件；未重跑无关网页/旧后端全套。没有真实供应商请求、网络联合、生产权限验证或NAS执行。

## 给协调者的最小装配需求

1. 管理HTTP必须复用现Cookie/Origin/CSRF及models解锁，服务端复核管理员授权后调用本模块；模块无公共入口，不能直接暴露execution_context/record_test给浏览器。设置key只接受HTTPS同源正文，不回显，不记录原始请求/异常。
2. `Models.__init__` 深拷贝settings.providers，`_validate_providers`只接受启动登记；目录不能独自驱动发布。需受信动态登记/发布适配，保留旧路径。`publish`/`_store_publication`已有固定版本和幂等摘要，可作为发布意图对账接点，不能绕其鉴权。
3. `Models.snapshot`检查 `caller.config_versions`、origin、撤销及过期。需协调签发精确版本grant，绑定provider_id/revision与config_version；不能把所有版本开放或把private DB给网关。现有快照寿命1–86400秒，持久默认与快照租期应分开，续租生成新有界版本并只影响新turn。
4. 旧turn版本固定后的私有凭据历史/撤销策略尚待合同。本模块只保留当前配置，execution_context拒绝旧revision；后续不能拿新key/new URL替换旧版本执行上下文。停用/删除及时撤销已发grant同样需协调服务接点，目录清默认不足以单独撤销在途请求。
5. 真实接通G1执行后才能record_test成功；模型枚举不是生成成功。选择默认之后仍需平台版本发布、gateway精确授权、companion新turn固定及自动续期联合验收。

本任务交付的是内部持久模块，供应商自助网页与真实对话闭环未完成、未部署。

## 2026-09-26 后端接线续交

在上述基线后，本工作树接入 `provider_management.py`、`provider_authority.py`、管理员 HTTP 和内部 select/runtime 路由；增加显式 `providers-init`、只读完整备份 preflight、测试请求持久占用与结果落账、动态精确版本/一小时租期、旧静态版本空间保护。`cryptography==50.0.1` 已加入 `pyproject.toml` 和完整 hash-locked runtime；原文“未改锁”和“未接 HTTP”只描述第一次 P1 交付，不再描述当前工作树。浏览器页面和部署配置未改。

验证：目录 19 项、旧模型页 26 项、根联合套件 4 项通过；Windows 隔离 wheel 构建、锁定依赖安装、`pip check`、已安装 CLI 通过。最终证据与部署接线见根 `docs/handoffs/PROVIDER-BACKEND-2026-09-26.md`。Linux 镜像/NAS/真实供应商未运行。

### 审查修复续交

修正排队后管理授权：provider view 在 LocalWork 执行前和回包前重新核验真实登录 authority 与模型管理解锁；写工作者重新核验当前 principal，撤销后不继续提交。短回复测试的目录 verdict 与 `client_id` replay receipt 合并在一个 SQLite 事务，故障注入证明回执写失败时目录状态也回滚；重启后的成功重放不再遇到“成功已落库但 attempt 永久 pending”。连接中断、超时和无效响应保留固定原因码，但测试结果记 `unknown` 且错误 envelope 标明执行状态未知，避免自动重发暗示。当前专项目录 20 项、根联合 6 项通过；完整审查证据见根交接。
