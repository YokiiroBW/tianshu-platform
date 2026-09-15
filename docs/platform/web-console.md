# 网页登录与对话入口（TS-014）

沿用现有 Vite 应用，入口 `/#/companion`。Python Platform 同源提供构建静态文件和 `/api/web/*`；独立 Vite 预览未接后台时明确显示未连接。小屋仍是原来的环境预览。

## 配置和运行

先按 `runtime.md` 安装后端，按根清单构建网页。使用 `web-console.settings.example.json` 创建本机隔离设置，替换所有绝对路径与管理员账号；示例没有可用密码或模型。启动前生成密码校验值：

```powershell
.runtime/venv/Scripts/python.exe -m services.platform.web_console
```

交互输入12–256字符密码并确认。只将输出的`scrypt-v1$盐$校验值`放入本机设置的`web.password_hash`；不保存明文密码、不把设置或运行库入库。所有内部服务token由各自`token_env`读取，至少24字符且每个用途独立；没有内置默认凭据。浏览器只得到HttpOnly会话Cookie和CSRF随机值。

```powershell
.runtime/venv/Scripts/python.exe -m services.platform --settings C:/local-rehearsal/platform/settings.json serve --port 4814
```

`web.origin`必须精确匹配实际scheme/host/port。`local_rehearsal`只允许127.0.0.1；`service_https`使用现有TLS证书/私钥设置且Cookie增加Secure。静态目录必须是已构建的`apps/web/dist`绝对路径；不指向仓库根或含运行设置的目录。不存在web配置时整个网页入口503。

管理员principal必须是platform/operator，账号namespace=web，且所有网页input_entries拥有相同账号、owner、self_private。允许角色来自每个input_entry的actor_entries及当前未撤销登记；客户端不能提交person、scope、verified或source proof。撤销账号/轮换服务凭据使旧登录失效；角色/入口撤销立即缩小列表和操作范围。服务重启使全部登录失效；登录绝对期限8小时，匿名CSRF会话10分钟。Cookie SameSite=Strict，所有POST另核验精确Origin、CSRF、JSON与正文预算；内部RPC拒绝Cookie/Origin。单家庭进程内会话最多128个，密码失败5次/60秒限流。

## 开启真实对话

默认`web.dialogue_enabled=false`。只有部署已集成的Core网页接口时才设true，并配置现行`core.base_url/token_env/ca_file`（仅HTTPS）。网页显示缺少模型配置时，先通过既有受信CLI发布有效模型配置，不自动使用用户外部key。平台只检查自己存在有效发布配置，Core选择的具体config_version/模型执行仍以其真实结果为准。

- 正式`contracts/web-conversation/v1` 1.0.0 manifest LF SHA256：`e493a1b5d0f4cec8d55995553faf84042f4c33a59365d15423e57f4dc70a6c09`。和text/profile/source-sync一起部署，不使用本任务候选目录。
- Core需要platform调用身份；解析网页actor origin的principal resolver.caller=platform。向Memory解析origin的身份仍为companion，不混用。
- Core的`services.platform_sender`指向本平台HTTPS，使用独立companion服务token及`dialogue.send` action。actor entry必须增加`companion → platform / dialogue` route。原`platform → companion`和`companion → memory` route保留。
- 网页只交选择ID、原文与client_id；平台生成稳定来源身份、消息ID和命令，调用真实`Sources.register_input/dispatch`，Core响应由现有inline确认写入。入站回执不包含回复，网页只显示接收状态。
- 快照由平台签发当前actor origin，转发公开Core web-snapshot；历史分页与当前活跃轮次分开读取。每2秒有界重取，隐藏页暂停，断线清除可能过期正文。只有sent且content_state=available显示回复正文；unknown/撤回/失效正文保持null。取消复用公开cancel及expected_version，保留Core实际结果/版本。

## 模型配置管理

设置页第二段“模型与用量”由 TS-016 接入：显示 Chat 与原生配置各自的版本、绑定与失效/撤销状态，
并在显式授权后预览、发布、撤销。开关是可选顶层设置 `web_models`（默认关闭），写操作还要当前会话
再次校验管理员密码；端口为同源 `/api/web/models/*`，浏览器只提交服务器登记模板的标识和版本号。
完整设置、授权、错误语义与验证入口见[网页模型配置管理](web-models.md)。

## 家庭设备状态与受限控制

“家庭与服务 → 家庭设备”（`/#/home`）由 TS-017 接入：显示服务器登记的 Home Assistant 实体的
读数、可用性与采样时间，只读传感器没有开关动作，light/switch 在显式解锁后按登记模板执行
`turn_on`/`turn_off`。开关是可选顶层设置 `home`（缺省即无入口），控制权限是操作者 principal 的
`device.control` 加当前会话解锁，与模型管理解锁互相独立。受理回执与后续观测分开显示，结果未知
保留且不自动重发；不支持门锁、门、报警或脚本。完整设置、读数/控制语义、错误码与验证入口见
[家庭设备状态与受限控制](home-devices.md)。本轮没有真实 HA 实例或设备。

## 持久性和失败语义

`<database_path>.web-inputs.sqlite`记录网页提交去重与结果；重启后未确认请求仍为unknown，不自动重新dispatch。重复client_id不同语义409。该库不作为聊天正文历史；聊天历史由Core按当前来源范围投影。

`<database_path>.web-replies.sqlite`是独立的持久网页sender收件箱。持久提交后才返回sent；同reply_id语义重复复用稳定receipt，同键不同正文/目标/轮段拒绝。它不提供绕过Core展示权限的直接网页读取。两库为新增sidecar，不迁移现有权威库；备份/恢复需和本机Platform库一起保留，不能删掉它们后声称具备去重。请求超时、响应丢失不等于没执行；浏览器不自动重发或把取消说成已回滚。

当前会话映射与双侧来源规则不是通用多租户权限系统。没有真实账号、生产服务、设备或外部模型调用。远端Memory单项素材撤销若尚未传到Core，没有展示级证明端口；Core文档明确该限制。历史显示不能用作模型召回授权或现实日记证据。

## 实际验证入口

设置`TS012_CONTRACT_DIR`为正式text-dialogue/v1绝对路径：

```powershell
.runtime/venv/Scripts/python.exe -m unittest discover -s tests/backend -p 'test_web*.py' -v
node node_modules/@playwright/test/cli.js test --config apps/web/playwright.web.config.ts
```

浏览器测试自动启动4814端口的隔离合成Platform服务，真实执行登录/CSRF/退出，不依赖外部Core或模型。测试名为synthetic UI state fixture的用例明确拦截对端快照，只验证页面状态，不代表产品联合通过。配置与Cookie均仅测试临时目录。桌面/移动、浅深主题、键盘、断线重连、axe检查在同一套中；构建在运行前完成。

Windows工具运行：本机固定Node位于`C:/Users/Administrator/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node.exe`。若无npm入口，可使用本任务`.runtime/npm/package/bin/npm-cli.js`（npm11.6.2官方发布包，按registry integrity校验）由该Node执行原根脚本；根清单/锁未变。

真实四产品TLS联合（仅导出协调已集成Git提交到自身`.runtime/web-joint`）：

```powershell
.runtime/venv/Scripts/python.exe tests/backend/run_web_joint.py --core-commit 195511ed055eaa6fce150dc245ce2a0e68a4d6c5 --prepare-only
.runtime/venv/Scripts/python.exe -m venv .runtime/web-joint/venv
.runtime/web-joint/venv/Scripts/python.exe -m pip install -r .runtime/web-joint/requirements.txt
.runtime/web-joint/venv/Scripts/python.exe tests/backend/run_web_joint.py --core-commit 195511ed055eaa6fce150dc245ce2a0e68a4d6c5
```

固定Memory575941e、Gatewayb3b101f；启动夹具来自根已集成a472825。测试不调用TS050任务runner、不读取Core数据库。真实Chromium登录/CSRF与越权拒绝、两条续句5秒合并、实际快照和持久sender、取消、Platform HTTP会话重启/重新登录保留历史。外部模型是录制替身，NoneBot录制器不得收到web回复。临时CA只在服务客户端使用；浏览器测试上下文允许该临时证书，不修改系统信任。运行结果与截图在`.runtime/web-joint/results`，监听器及临时服务清理结果也入证据。
