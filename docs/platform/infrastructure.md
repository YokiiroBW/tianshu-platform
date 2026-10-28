# 节点与容器观测

`#/home/1` 是 Docker 容器三栏页，`#/home/2` 是 NAS／Linux／OpenWrt 节点页，`#/settings/11` 是节点连接设置。家庭设备仍在 `#/home/0`；游戏服尚未接入，会明确显示原因。本批只读，不提供容器控制或没有来源的历史曲线。

## 部署配置

工作台的“天枢主机资源”复用此模块已有的主机采样缓存。可在 `infrastructure` 设置中增加 `"platform_node_id": "已登记节点ID"`，明确指定平台所在机器；未填写时显示待绑定，不会自动取目录中的第一台节点。只读 `POST /api/web/infrastructure/host` 接受空对象，返回 `configured` 与该节点的 `node` 观测，沿用同源会话、CSRF 和 `external.manage` 权限。页面每 3 秒读取缓存，不启动额外 SSH、采集器或后台任务。过时、缺失和读取失败分别呈现，未知指标不显示成零。

节点执行器由部署方安装，平台只启动部署配置中的固定 argv 和 cwd。网页不能传地址、私钥、known_hosts、命令、执行目录、配置文件或状态目录。现有平台 Dockerfile **没有 SSH 客户端和节点执行器包**，仅增加下面的设置不能连接节点；联合部署须采用 TS-178 的执行器打包产物及其安装说明。

以下为 TS-180 联合部署路径约定，不表示该路径已经上线。平台 wheel 使用既有 `/opt/tianshu-venv`；执行器另用 `/opt/tianshu-infrastructure-venv` 和 `PyYAML==6.0.3`，镜像须另装 OpenSSH。执行器启动器把固定包加入自身导入路径，平台不修改 PATH 或注入 PYTHONPATH。`executor.json` 按节点合同的 `config.schema.json` 登记节点、显式身份／known_hosts、stacks 目录及固定 `state_dir`，这些内容不进入网页。

```json
{
  "infrastructure": {
    "directory": "/var/lib/tianshu/external",
    "contract_directory": "/opt/tianshu-infrastructure-node/contracts/infrastructure-node/candidate-v1",
    "executor": {
      "command": [
        "/opt/tianshu-infrastructure-venv/bin/python",
        "-I",
        "/opt/tianshu-infrastructure-node/run_node.py",
        "--config",
        "/etc/tianshu/infrastructure/executor.json"
      ],
      "directory": "/opt/tianshu-infrastructure-node"
    },
    "poll_interval_seconds": 30,
    "stale_after_seconds": 120,
    "timeout_ms": 30000
  }
}
```

`directory` 复用现有 `ExternalCatalog` 加密目录；同时配置 `web_external` 时，两者必须指向同一目录。首次安装由管理员显式执行已有 `python -m services.platform.external_catalog init <绝对目录>`，服务启动不初始化或修补密钥。仅使用基础设施功能不要求配置 AssetLibrary、HA 或 `web_external`。缺省不配置 `infrastructure` 时，不创建观测库、不启动进程或采集循环。

管理员进入节点设置后，从执行器返回的目录选择节点。下拉显示名称、类型与能力，不要求手填 `node_id`。网页只保存节点引用、显示名称和是否采集。目录读取失败时保留上次目录并标出最近失败；执行器缺失时显示安装／部署原因。已登记但后来从目录移除的节点仍可停用。

本批合同固定于协调仓库 `18e4fe0476ce9cc0900de8d8e0731b4c1baa2d81`；运行及合成 SSH 夹具最终固定于 `172d20d37b12f88a15ec214bd6ee2ba8ea880339`，schema 相同。平台按 LF 规范化后的固定摘要核验 request/response schema，不自动接受可变工作目录中的协议。候选合同的生产发布仍由协调者安排。

TS-182 部署候选执行器更新为 `47564d07257a43ad99dde63d6ea96cf8c169be5a`，包 SHA256 `9aac3dc090a0317804b5787dd5136b4916ee686b718a11b18b74e976806cebf5`，沿用上述 schema。它修正真实 DSM/Docker 的可选 Health、dial-stdio 输入 EOF 和 stats 流水采样；每批最多 8 个 SSH 流水组，仅对实际运行对象获取 stats。平台继续每批最多 200 个 ID 顺序调用，单请求部署为 30 秒，整轮仍为 90 秒。

执行器环境仅保留 PATH、SYSTEMROOT、WINDIR、PROGRAMDATA、TEMP、TMP、LANG、LC_ALL。Windows OpenSSH 需要 PROGRAMDATA；实际合成 SSH 联验已确认。服务凭据、PYTHONPATH 和用户 SSH 环境不传给执行器。执行器必须自行使用配置中的显式 key／known_hosts 并禁用隐式 SSH 配置和 agent。

Windows 进程先以 [CREATE_SUSPENDED](https://learn.microsoft.com/en-us/windows/win32/procthread/process-creation-flags) 启动，加入 kill-on-close Job 后才恢复主线程。这样 venv 启动器及后续 Python／SSH 后代都继承同一 Job，父进程先退出仍能回收；保护或恢复失败会结束挂起根，不能继续运行无保护执行器。

Linux 每次调用先启动独立的短生命周期 supervisor，在创建执行器前设置 [child subreaper](https://man7.org/linux/man-pages/man2/PR_SET_CHILD_SUBREAPER.2const.html) 和 [PDEATHSIG](https://man7.org/linux/man-pages/man2/PR_SET_PDEATHSIG.2const.html)，前后核对平台父 PID。真实执行器的 stdout/stderr 和正常退出码直接透传。取消、执行器先退出或平台父突然退出时，supervisor 持续终止已收养的孩子并 wait 至 ECHILD 后退出；即使 SSH 创建独立 session，也会被收养并回收。平台不提前杀死该收养者，不把整个平台设置成 subreaper，不影响音乐进程的所有权。其他 POSIX 保留原有进程组回收。

## 采集、缓存与数据含义

采集由非公共 Platform HTTP 服务的生命周期管理，关闭服务时取消并回收。浏览器只读缓存或排队刷新，页面关闭不停止后台采集。`test` 与 `refresh` 在短请求中返回 `queued/in_progress`；不等待整轮 SSH。重复刷新合并为当前一轮，保存／停用连接取消旧轮并丢弃旧修订结果。

默认每轮结束后 30 秒再次采集，允许 5–3600 秒；新鲜度默认 120 秒且不小于采集间隔。平台最多 2 个外部执行并行、每节点 1 个、最多登记 64 个节点。单次执行器请求 0.5–30 秒，另有 1 秒回收余量；整轮节点采集上限 90 秒。进程 stdout 最多 4 MiB、stderr 最多 64 KiB，超时或输出超限会回收进程树。退出码 0 仅表示返回了协议结果，业务成功必须看 `status`。

容器和 Compose 以每页 200 项继续读取，最多 50 页／10000 个身份；清单变化时重新开始一次，继续变化则保留明确失败。每个合并操作缓存上限 16 MiB，触及上限不会声称完整。网页列表默认每页 100 项，搜索、状态、健康和应用筛选由平台在已取得的清单上执行。容器全部使用完整 64 位 ID。

`<database_path>.infrastructure.sqlite` 只保存每个节点／操作的最近尝试及最后可用观测，不是连接或凭据库。新失败保留旧观测与原采样时间，页面分别展示“上次读数”和“最近尝试”；离线、过时、未采样和部分结果不会显示成完整当前状态。多页／多批观测取最早采样时间，任何一批 partial、缺项或指标原因都会保留到汇总。该库是可重建缓存，备份时按 SQLite 一致性方式处理；原 `external.sqlite` 与 `external.key` 仍须成对备份。

主机 CPU 按全部主机核心计算；容器 CPU 以单核为 100%，150% 不会截成 100%。首次采样、重置和过长间隔的差值显示未知。原始／扣缓存内存及缓存来源分别保留；没有来源不当作零。网络／块 IO 由连续累计计数计算速率，共享网络命名空间不伪造逐容器流量。文件系统逐挂载显示，不跨挂载加成 NAS 总容量。OpenWrt 展示实际 board、system、WAN／接口来源；逐终端流量没有注册来源时明确“尚未接入”。

运行状态与健康检查分别显示，“未配置检查”和“检查未知”不会变成健康。日志按需排队，固定最近 1 小时、最多 200 行／64 KiB，显示截断与来源时间；最多 8 个排队／运行日志请求、16 个内存预览，重启后清除。预览不替代已有完整持久日志。

可选节点离线只改变该节点的观测，不加入整个平台 health／ready 的依赖。基础设施观测不是设备动作，不新增任务中心操作台账；已有家庭控制、模型发布和媒体任务继续由原模块负责。

## 网页端口与权限

同源 `POST /api/web/infrastructure/` 下的端口为：

| 后缀                   | 请求内容                                            | 作用                       |
| ---------------------- | --------------------------------------------------- | -------------------------- |
| `view`                 | `{}`                                                | 目录、节点摘要及全局修订   |
| `catalog/refresh`      | `{}`                                                | 排队重读脱敏目录           |
| `save`                 | `node_id,label,enabled,expected_revision,client_id` | 保存目录引用与启用状态     |
| `test`、`refresh`      | `node_id`                                           | 合并／排队节点采集         |
| `node`                 | `node_id`                                           | 主机、网络和 Compose 观测  |
| `containers`           | `node_id`，可选筛选和页码                           | 容器清单及指标             |
| `container`            | `node_id,container_id`                              | 确切容器的端口、挂载和指标 |
| `logs`、`logs/refresh` | `node_id,container_id`                              | 读取／排队日志预览         |

全部复用真实 Cookie、Origin、CSRF、会话失效检查与 `operator + external.manage` 权限，异步返回前再复核会话。保存复用 ExternalCatalog 的全局 CAS 修订和 UUID 幂等回执；其他管理员修改后旧表单 409，页面保留草稿并可“重新读取设置”。后台轮询不会偷偷推进草稿的 expected_revision。没有新增密码解锁层。

## 验证入口

后端最窄运行 `python -B -m unittest discover -s tests/backend -p test_infrastructure.py -v`；真实执行器联合运行同目录的 `test_infrastructure_joint.py`。显式设置：

- `TS012_CONTRACT_DIR`：平台已冻结发布的 text-dialogue/v1 目录；不能用摘要不符的历史副本。
- `TS100_DIAGNOSTICS_CONTRACT_DIR`：原始字节 manifest 为 `5d89f7a…` 的 diagnostics/v1 发布目录，不规范化行尾。
- `TS177_NODE_CONTRACT_DIR`：固定节点合同目录。
- `TS177_NODE_EXECUTOR_ROOT`：从固定 `172d20d…` Git 归档的执行器根目录，包含共享 `tests/infrastructure/{helpers.py,synthetic_ssh.py}`；联合夹具额外依赖 `paramiko==4.0.0`。

联合夹具仅监听 127.0.0.1，临时生成测试 key／known_hosts，以真实 OpenSSH 访问合成的 150 容器 NAS 和 OpenWrt。它验证 Platform HTTP → 实际进程执行器 → OpenSSH → 合成设备，不能代表真实 NAS／路由器。

前端沿用类型检查和 Vite build；`node node_modules/@playwright/test/cli.js test --config apps/web/playwright.infrastructure.config.ts` 使用本检出的 `.runtime/venv/Scripts/python.exe` 启动隔离的真实 Platform HTTP，执行器使用固定合同响应夹具。浏览器覆盖桌面／手机、明暗主题、Axe、130 容器、迟到响应与并发 CAS；这组页面测试不是实际 SSH 联验。结果分别写入忽略目录 `.runtime/infrastructure-browser-results` 和 `.runtime/infrastructure-browser-report`，不清理通用壳测试的产物。

本批实际结果、已核实的旧壳断言失败与截图位置见 [TS-177 交接](../handoffs/TS-177.md)。

## TS-182 生产连接与验收

生产 `service_https` 的内部 TLS listener 与 `web_access` 公共 listener 共用同一个控制台和基础设施实例；采集生命周期只挂内部应用。配置了 `web_access` 后，内部端口不提供网页/API，公共端口不提供 `/health/*` 或内部 RPC。驱动必须传保存的公共 origin，不能把内部 HTTPS 端口当网页端口。当前已读回部署映射为 `http://192.168.31.210:18446` → 容器 `8080`；这个地址是显式环境输入，不是驱动默认值。使用 HTTPS 时仍校验 CA 和主机名，不关闭 TLS 验证。

上线必须复用 `/var/lib/tianshu/external` 中原有 `external.key` 与数据库，以及原 `web_account` 账号文件/封印和 principals 权限；不执行 init、claim 或权限补写。节点执行器目录只登记 `nas-main / 主 NAS / synology`。用户登录后在 `#/settings/11` 从目录选择并保存/启用，再到 `#/home/1` 和 `#/home/2` 查看真实读数。OpenWrt 本轮不登记或重试。执行器 key、known_hosts 分别只读挂载到 `/run/tianshu-infrastructure/identity`、`/run/tianshu-infrastructure/known_hosts`，采样状态固定 `/var/lib/tianshu/infrastructure-sampling` 的 128 MiB、UID/GID 10001 本地 tmpfs，配置仍在 `/etc/tianshu/infrastructure/executor.json`。不挂 Docker socket 或整套用户 SSH 目录。

`tests/backend/verify_infrastructure_deployment.py --input <私有JSON> --output <私有回执JSON>` 使用公共 origin、真实 Cookie/Origin/CSRF，检查全量网页分页、所有 ID 的指标行和尾部指标、连续主机差值、Compose 及显式目标的限定日志。输入示例：

```json
{
  "origin": "https://console.example.invalid",
  "node_id": "nas-main",
  "username_env": "TIANSHU_VERIFY_USERNAME",
  "password_file": "/private/authorized-password",
  "phase": "initial",
  "register_if_missing": false,
  "timeout_seconds": 180
}
```

也可提供 `username_file`、显式用户名或 `password_env`，密码禁止直接放入 JSON。私有 CA 可通过绝对 `ca_file` 指定。仅明确设置 `register_if_missing:true` 时允许保存缺失节点，默认不写连接。已有停用节点不会被驱动启用。可选 `log_container_id` 必须为已批准的完整 64 位 ID；本次实机仅允许最终 platform 容器。回执不含账号、地址、完整容器 ID、日志正文或秘密，日志仅记录字节数/截断/摘要。`phase:after_restart` 不保存、不刷新、不取日志，等待两次完整后台采集时间推进，验证无需页面触发也持续工作。

`node apps/web/tests/infrastructure-deployment.mjs --input <私有JSON> --output <私有目录>` 用同样的显式 origin/账号引用登录，检查桌面浅色和手机深色的 Docker/节点页面，生成截图和脱敏计数；没有路由替身或 Cookie 导入。Chromium 使用正常信任库，不采用 Python 的 `ca_file`，私有 CA 须由运行环境原本信任。脚本也导出 `inspectAuthenticated(page,input,output,suffix)`，供已有明确授权的登录页面调用，不导出会话。截图只保存在私有目录。本轮用户将亲自登录生产页面，协调者用该既有页面验收；不会为运行驱动获取密码、导出 Cookie 或创建测试账号。

候选 Linux 镜像中的最窄后端检查使用 `tests/backend/test_infrastructure_deployment.py`。把固定测试目录及完整发布合同只读挂载至 `/validation`，不挂生产设置/数据库；设置 `TS012_CONTRACT_DIR=/validation/contracts/text-dialogue/v1`、`TS100_DIAGNOSTICS_CONTRACT_DIR=/validation/contracts/diagnostics/v1`、`TS177_NODE_CONTRACT_DIR=/validation/node-contract`、`TS182_REQUIRE_INSTALLED=1`。节点测试合同须包含固定响应 examples；TS-180 运行包刻意不带测试响应，可单独从固定节点提交归档，不扩运行包。诊断合同保留原字节 manifest `5d89f7a2…`。入口为：

```sh
/opt/tianshu-venv/bin/python -I -B -c 'import sys,unittest;sys.path.insert(0,"/validation/tests/backend");suite=unittest.defaultTestLoader.discover("/validation/tests/backend",pattern="test_infrastructure_deployment.py");result=unittest.TextTestRunner(verbosity=2).run(suite);sys.exit(not result.wasSuccessful())'
```

这两项检查使用安装后的 wheel，验证 supervisor 随 wheel 存在，以及实际内部 TLS/公共 HTTP、真实会话、201 容器分页/尾部指标、原合成账号/外部连接保留和重启后两次后台采集；执行器仍是明确合同响应夹具，不称实机。依赖只有候选镜像既有平台 Python 依赖；无需 pip、Paramiko、节点 PyYAML、Docker socket 或外网。容器用 `--network none --read-only --tmpfs /tmp:rw,size=32m,mode=1777 --memory 256m --user 10001:10001` 即可；DSM 的 `--cpus`/`--pids-limit` 不受支持，不作为此次命令前提。

Linux 进程定点用例 `test_infrastructure_posix_lifecycle.py` 另需固定节点 transport 的根目录 `TS182_NODE_TRANSPORT_ROOT`，只调用其 `run_bounded` 启动合成 sleep，不用真实 SSH。协调者的 stdin 归档入口把固定平台/节点源码和测试展开到临时目录，五项分别覆盖取消、执行器先退出、平台父先退出、协议/退出码透传及父 PID 启动窗口。最终实际结果与归档摘要见 [TS-182 交接](../handoffs/TS-182.md)。
