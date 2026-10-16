# 节点与容器观测

`#/home/1` 是 Docker 容器三栏页，`#/home/2` 是 NAS／Linux／OpenWrt 节点页，`#/settings/11` 是节点连接设置。家庭设备仍在 `#/home/0`；游戏服尚未接入，会明确显示原因。本批只读，不提供容器控制或没有来源的历史曲线。

## 部署配置

节点执行器由部署方安装，平台只启动部署配置中的固定 argv 和 cwd。网页不能传地址、私钥、known_hosts、命令、执行目录、配置文件或状态目录。现有平台 Dockerfile **没有 SSH 客户端和节点执行器包**，仅增加下面的设置不能连接节点；联合部署须采用 TS-178 的执行器打包产物及其安装说明。

以下为路径示例，不是已安装环境。Python 环境须包含平台依赖及执行器的 `PyYAML==6.0.3`，cwd 中须有固定 `ops.infrastructure` 包。`executor-config.json` 按节点合同的 `config.schema.json` 登记节点、显式身份／known_hosts、stacks 目录及固定 `state_dir`，这些内容不进入网页。

```json
{
  "infrastructure": {
    "directory": "/var/lib/tianshu/external",
    "contract_directory": "/opt/tianshu-node/contracts/infrastructure-node/candidate-v1",
    "executor": {
      "command": [
        "/opt/tianshu/venv/bin/python",
        "-B",
        "-m",
        "ops.infrastructure",
        "--config",
        "/run/tianshu-node/executor-config.json"
      ],
      "directory": "/opt/tianshu-node"
    },
    "poll_interval_seconds": 30,
    "stale_after_seconds": 120,
    "timeout_ms": 10000
  }
}
```

`directory` 复用现有 `ExternalCatalog` 加密目录；同时配置 `web_external` 时，两者必须指向同一目录。首次安装由管理员显式执行已有 `python -m services.platform.external_catalog init <绝对目录>`，服务启动不初始化或修补密钥。仅使用基础设施功能不要求配置 AssetLibrary、HA 或 `web_external`。缺省不配置 `infrastructure` 时，不创建观测库、不启动进程或采集循环。

管理员进入节点设置后，从执行器返回的目录选择节点。下拉显示名称、类型与能力，不要求手填 `node_id`。网页只保存节点引用、显示名称和是否采集。目录读取失败时保留上次目录并标出最近失败；执行器缺失时显示安装／部署原因。已登记但后来从目录移除的节点仍可停用。

本批合同固定于协调仓库 `18e4fe0476ce9cc0900de8d8e0731b4c1baa2d81`；运行及合成 SSH 夹具固定于 `12ccb1d894c15813ebe649e327071836ed5ea815`，两者 schema 相同。平台按 LF 规范化后的固定摘要核验 request/response schema，不自动接受可变工作目录中的协议。候选合同的生产发布仍由协调者安排。

执行器环境仅保留 PATH、SYSTEMROOT、WINDIR、PROGRAMDATA、TEMP、TMP、LANG、LC_ALL。Windows OpenSSH 需要 PROGRAMDATA；实际合成 SSH 联验已确认。服务凭据、PYTHONPATH 和用户 SSH 环境不传给执行器。执行器必须自行使用配置中的显式 key／known_hosts 并禁用隐式 SSH 配置和 agent。

Windows 进程先以 [CREATE_SUSPENDED](https://learn.microsoft.com/en-us/windows/win32/procthread/process-creation-flags) 启动，加入 kill-on-close Job 后才恢复主线程。这样 venv 启动器及后续 Python／SSH 后代都继承同一 Job，父进程先退出仍能回收；保护或恢复失败会结束挂起根，不能继续运行无保护执行器。POSIX 使用独立进程会话并回收整个进程组。

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
- `TS177_NODE_EXECUTOR_ROOT`：从固定 `12ccb1d…` Git 归档的执行器根目录，包含共享 `tests/infrastructure/{helpers.py,synthetic_ssh.py}`；联合夹具额外依赖 `paramiko==4.0.0`。

联合夹具仅监听 127.0.0.1，临时生成测试 key／known_hosts，以真实 OpenSSH 访问合成的 150 容器 NAS 和 OpenWrt。它验证 Platform HTTP → 实际进程执行器 → OpenSSH → 合成设备，不能代表真实 NAS／路由器。

前端沿用类型检查和 Vite build；`node node_modules/@playwright/test/cli.js test --config apps/web/playwright.infrastructure.config.ts` 使用本检出的 `.runtime/venv/Scripts/python.exe` 启动隔离的真实 Platform HTTP，执行器使用固定合同响应夹具。浏览器覆盖桌面／手机、明暗主题、Axe、130 容器、迟到响应与并发 CAS；这组页面测试不是实际 SSH 联验。结果分别写入忽略目录 `.runtime/infrastructure-browser-results` 和 `.runtime/infrastructure-browser-report`，不清理通用壳测试的产物。

本批实际结果、已核实的旧壳断言失败与截图位置见 [TS-177 交接](../handoffs/TS-177.md)。
