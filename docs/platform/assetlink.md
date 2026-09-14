# TS-064 后台资产只读接入

平台提供受信 Python 应用端口 `await platform.assets.read(header, request)` 和本地 `asset-read` CLI。仅消费 AssetLibrary 固定 HTTPS `/assetlink/v1/control` 的 `libraries.list/get`、`entries.browse/get`、`assets.search`。网页未接入，没有新增公共 HTTP、网页登录、下载、预览、模型整理或写资产能力。

协议依据 AssetLibrary `ff8e8a1fc405870d5a82e44cc344c9e083df059a` 的 `contracts/assetlink/read-only-trial-v1.md`（TS063 appendix）、`web-interaction-v1.md`、control/result/error schema 和 `docs/integrations/tianshu/service-read-operator.md`。TS062方案只作消费需求参考；未修改对端或共享合同。

## 配置与实际入口

沿用 [平台后端安装](runtime.md)。以下是独立本地配置的完整最小形状，路径和 `.invalid` 地址必须换为明确的隔离部署值；样例没有凭据或默认可用连接。放在访问受保护的 `.runtime/`，不得提交。

```json
{
  "mode": "local_rehearsal",
  "storage": "sqlite_local",
  "database_path": "C:/private/platform/platform.sqlite",
  "contract_directory": "C:/YOKI/Codex/tianshu-peiban-bot/contracts/text-dialogue/v1",
  "principals": {
    "asset-reader": {
      "kind": "service",
      "service": "platform",
      "token_env": "TS_PLATFORM_ASSET_READER",
      "actions": ["asset.read"],
      "asset_connections": ["personal-assets"]
    }
  },
  "asset_connections": {
    "personal-assets": {
      "endpoint": "https://assets.example.invalid:8443/assetlink/v1/control",
      "ca_file": "C:/private/platform/assetlibrary-ca.pem",
      "token_env": "TS_ASSETLIBRARY_PERSONAL_READ"
    }
  }
}
```

`TS_PLATFORM_ASSET_READER` 是平台登记的受信应用凭据；`TS_ASSETLIBRARY_PERSONAL_READ` 是 AssetLibrary 正式本地 operator 创建独立非管理员 service principal、授库 `read_only` 后签发的 opaque token。两者必须独立，不能是浏览器管理员会话。不同连接不能复用 token 环境变量或值，也不能复用平台主体/Core dispatch 凭据。秘密由受保护的进程环境注入，不放 JSON、命令行或日志。

授权分两层：平台先认证真实 principal 并检查其显式 `asset.read` 和连接白名单；AssetLibrary 每请求查询当前服务身份及库权限。平台仅保存连接绑定，不访问资产数据库，不复制库 ACL。`origin_ref`、dialogue audience、payload 自报身份/授权都不能赋予连接或库权限。配置与绑定进入现有平台 authority policy 摘要；未配置资产能力的旧设置保持原摘要算法。配置修改应重建 Platform；凭据环境更新下一次请求生效。在途结果交付前再检查本地主体撤销和凭据变化。

请求文件 `.runtime/asset-read.json`：

```json
{"connection_id":"personal-assets","operation":"libraries.list","body":{"page_size":50}}
```

```powershell
$env:PYTHONIOENCODING = 'utf-8'
.runtime/venv/Scripts/python.exe -m services.platform --settings .runtime/asset-settings.json local --credential-env TS_PLATFORM_ASSET_READER asset-read --input .runtime/asset-read.json
```

CLI仅从已登记环境引用取得平台认证凭据；成功退出0，失败退出1。用于机器消费时显式 UTF-8，避免 Windows 默认代码页影响中文。应用端口接受相同字典或有界 UTF-8 JSON bytes；顶层只允许 `connection_id/operation/body`。它没有网页身份认证能力，不应直接把浏览器输入连到此端口。

五读 body 沿用现行协议。`entries.browse` 用 `library_id,parent_relative_path,page_size`，可带原样 cursor/sort/filter；`entries.get` 用 `library_id,entry_id`；`libraries.get` 用 `library_id`；`assets.search` 用 query/page，scope 为 all/library/directory，库与路径规则由上游验证。页最大100；不改写游标、不扩大 scope，也不保证跨页一致快照。

## 结果、取消与预算

成功结果含 `ok:true/status:200/body/request_id/upstream_request_id`，只有与本地随机 UUID 完全相等的成功关联才交付。`representation:index_metadata` 和 `original_available:not_verified` 明确只读到索引元数据，保留上游 library availability；源 offline 的授权索引仍不代表原件在线。

失败返回 `ok:false/status/code/body:null/clear_previous:true/retry:manual`。上游错误 message/details 不回显；合法错误码和状态保留。认证/解析前上游 `unknown` 仅放 `upstream_request_id`，本地 `request_id` 始终保留；不伪造上游回显。客户端没有资产正文、游标、登录或 token 缓存，每次重新读取部署凭据，401/404/网络失败不返回上次正文，也不自动重试或退回管理员会话。部署环境中的凭据不会被客户端擅自删除；操作者显式替换凭据/重连，从第一页重新发现。

调用方须每次用本次结果替换旧状态，失败清页/详情/游标。账号、连接、查询范围或排序变化时先取消旧 task，清空状态，然后从第一页查询；分页按 entry_id 去重。取消采用标准 `asyncio.Task.cancel()`，传播 `CancelledError`，关闭响应和会话且不交付迟到结果；调用方在取消处理里清显示状态。此后台入口不维护网页请求代次或 UI 状态，网页接入仍需另行实现。

- 请求 UTF-8 完整 AssetLink envelope 最大64KiB；CLI文件有界读取，超限/非法 JSON 保留本地关联。
- 响应最大1MiB，先检查 Content-Length，再分块累计，超限关闭流而非截断为成功；不自动解压，不跟随重定向，不使用环境代理，不发送 Cookie/Origin/CSRF/Sec-Fetch。
- 每调用总预算5秒，TLS 校验固定 CA 与主机名；网络/流读取共用剩余预算，解析和最终授权后检查期限。平台本地授权库忙时即时503，避免额外等待5秒。
- 1MiB是平台消费预算，并非上游所有合法数据均小于1MiB。当前138+1个合成文件及100行分页通过；任意超长路径数据可能被平台明确拒绝。扩大预算须协调，不能据此宣布50万资产/P95通过。

## 可复现验证

所有命令在本任务 worktree；无需安装前端或运行 WebGL。工具参数不接受活库/生产服务地址。

```powershell
& C:/Users/Administrator/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe -m venv .runtime/venv
.runtime/venv/Scripts/python.exe -m pip install -r requirements-dev.txt
$env:TS012_CONTRACT_DIR = 'C:/YOKI/Codex/tianshu-peiban-bot/contracts/text-dialogue/v1'
$env:TS013_TLS_PYTHON = 'C:/Users/Administrator/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe'
$env:TS012_GATEWAY_SRC = 'C:/YOKI/Codex/tianshu-peiban-bot/projects/tianshu-model-gateway/src'
$env:PYTHONDONTWRITEBYTECODE = '1'
.runtime/venv/Scripts/python.exe -m ruff format --check services/platform tests/backend
.runtime/venv/Scripts/python.exe -m ruff check services/platform tests/backend
.runtime/venv/Scripts/python.exe -m unittest discover -s tests/backend -p test_assets.py -v
.runtime/venv/Scripts/python.exe -m unittest discover -s tests/backend -v
.runtime/venv/Scripts/python.exe tests/backend/run_assetlink_integration.py --asset-repo C:/YOKI/Codex/tianshu-peiban-bot/projects/assetlibrary --dotnet C:/Users/Administrator/AppData/Local/Temp/V01-014-tooling-and-tests/tooling/dotnet/dotnet.exe --postgres-bin C:/Users/Administrator/AppData/Local/Temp/V01-014-tooling-and-tests/tooling/postgresql/pgsql/bin
```

`test_assets.py` 是真实 TLS socket + 明确合成上游的故障注入：身份/连接、无写操作、坏CA、重定向、大小边界、错误/unknown/错误关联、离线标记、总5秒、取消、迟到结果、本地主体在途撤销与本地锁忙。

联合驱动只读 Git 导出固定 AssetLibrary revision 到本任务 `.runtime/assetlibrary`，逐源文件核对导出字节后构建。所有对端 bin/obj、NuGet/SDK缓存和平台测试桥都在本任务 `.runtime`；不对协调检出做 editable build。平台拥有的 C# 桥通过反射复用固定版本内部测试夹具启动真实 TrialHostFactory；服务签发/授撤权复用其 operator fixture，未改源码、未用SQL灌权限。临时PG集群应用真实迁移并创建六个最小模块登录；只有初始化/清理使用夹具管理SQL。真实库查询由实际 AssetLibrary 执行。

驱动覆盖真实五读、两主体两库、100+37分页、中文、scope拒绝、CLI、混合身份和64KiB拒绝、源离线/网络离线与重连、库撤权后的下一页/get/list/search、服务凭据撤销/主体禁用/重启。原件hash/mtime、Host/连接、PG/六登录、临时私密文件清理后才输出成功记录；开始和结束均核对对端源码未变。证据写 `.runtime/ts064-*/acceptance.json`，pipe内token不写日志。静态HTML只满足Host配置；HIBP是唯一外部认证替身，不构成真实浏览器、NAS、生产或50万规模证据。

本轮交付见 [TS-064交接](../handoffs/TS-064.md)。后续网页接入需独立真实用户认证、连接绑定与页面状态管理，本轮不宣称资产网页已连接。
