# CONNECT-B 外部连接管理员配置交接（2026-09-27）

## 范围

后端支持管理员经同源网页登录为固定两类连接保存配置并执行一次真实只读检测：AssetLink 的 `libraries.list`，以及 Home Assistant 已登记的首个实体状态读取。固定端点、请求和返回字段见 [CONNECT-B-API.md](CONNECT-B-API.md)。没有通用 HTTP 代理或设备控制模板生成。

部署仍要明确登记 `web_external.directory`、`allowed_cidrs`、`assets_connection_id`，并把该 AssetLink ID 绑定到 `asset_connections: {managed_external:true}`、`web_assets` 与具有 `asset.read` 的独立服务主体。网页登录管理员另需显式 `external.manage`。静态 `home` 与 `web_external` 不可并用。网页填写 URL、Bearer、可选 CA、HA 实体；所有解析地址均须落在部署允许的私有 CIDR 内，保存的地址快照用于实际出站连接，当前 CIDR 策略在每次读取时重查。

## 安装、备份和恢复

以下是总控在**停服、私有目录、私有权限**下的步骤示意，不是本任务已执行的生产操作。先创建目录的父级，再运行：

```powershell
& <platform-python> -m services.platform.external_catalog init <绝对私有目录>
& <platform-python> -m services.platform.external_catalog check <绝对私有目录>
```

`init` 只允许新目录；服务启动只调用只读 `check`，缺库、key、版本 seal 或连接行 MAC 即失败关闭，不会自动修复。对目录、`external.sqlite`、`external.key` 设置仅服务账号及管理员可读写的系统 ACL；Windows 上 `chmod` 不代表 ACL 已配置。凭据和 CA 在库中 AES-GCM 加密，URL、实体清单及检测状态是有完整性校验的明文元数据。停服后把**整个目录**（含数据库和 key）作为一致单元备份，备份访问权与原目录相同。恢复时停服、整体恢复同一备份、执行 `check` 后再启动；不要只替换数据库或 key。目录损坏、key 丢失或 MAC/seal 不符时保持停服并从一致备份恢复，不能生成新 key 覆盖旧库。

回退这一功能时停服，撤销 `web_external`、`external.manage` 与 managed AssetLink 占位配置；若仍需旧连接，则按既有静态配置规范恢复 AssetLink/Home 的部署文件与凭据环境变量。确认静态配置通过预检后启动；保留旧目录的受控备份供审计。不要在运行中移动数据库，也不要把私有目录提交到 Git。

## 行为边界和验证

保存需当前密码再次解锁，窗口 15 分钟，且每次检查管理员权限和会话。全局修订号使用 SQLite CAS；旧修订冲突为 409，成功保存才返回 `applied:true` 和 `saved_unverified`。保存清除旧资产/设备页面作用域，正在读取的旧结果在回包前经修订检查拒绝；检测回执只绑定当次修订。保存与运行中适配器切换之间可能出现短暂不可用；失败响应不能解读为旧配置仍在生效，重试前先读取 `view.revision`。HA 管理配置不创建控制模板，因此管理员登记实体只开放状态读取。

实测命令（本产品 worktree；`TS013_TLS_PYTHON` 指向有 `cryptography` 的解释器）：

```powershell
$env:TS012_CONTRACT_DIR='C:/YOKI/Codex/tianshu-peiban-bot/contracts/text-dialogue/v1'
$env:TS013_TLS_PYTHON='C:/Users/Administrator/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe'
& .runtime/venv/Scripts/python.exe -m unittest discover -s tests/backend -p test_web_external.py -q
& .runtime/venv/Scripts/python.exe -m unittest discover -s tests/backend -p test_web_assets.py -v
& .runtime/venv/Scripts/python.exe -m unittest discover -s tests/backend -p test_home.py -v
```

结果：外部连接 4 项、资产页 31 项、家庭 45 项全部通过；外部连接测试在临时目录中运行真实同源 HTTP、合成 AssetLink HTTPS 和 HA HTTP，包含密码/权限、凭据轮换、修订竞争、旧读取拒绝、库篡改、私钥与公网目标拒绝。`ruff` 与 `git diff --check` 通过。没有 NAS、真实 AssetLink/HA 设备或生产数据验收。
