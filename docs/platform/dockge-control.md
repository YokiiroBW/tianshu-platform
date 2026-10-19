# Dockge 控制后台（TS-206）

在原“家庭与服务 → 容器”页内接入现有节点执行器。平台负责持久受理、后台调度、连接修订与管理员 HTTP；节点负责具体 Docker/Compose 行为、受控目录、归属、保护与版本恢复。HTTP 请求和 SSH 长操作分离。前端合同见 [Dockge 控制 HTTP](dockge-control-http.md)。

## 配置与复用

旧 `infrastructure-node.v1` 配置、schema 摘要、命令与只读入口保持。控制协议固定为根提交 `89f93d41a5eccb04740c1e8710968d03af7c533a` 的 `contracts/infrastructure-control/candidate-v1`；部署时另打包该版本。可选控制段复用现有节点 ID 与 ExternalCatalog 登记，只有部署方登记执行器命令，不另设网页凭据系统。

```json
{
  "infrastructure": {
    "directory": "/deployment-owned/external",
    "contract_directory": "/deployment-owned/contracts/infrastructure-node/candidate-v1",
    "executor": {
      "command": ["/deployment-owned/python", "-B", "-m", "ops.infrastructure", "--config", "/deployment-owned/node.json"],
      "directory": "/deployment-owned/node-package"
    },
    "control": {
      "contract_directory": "/deployment-owned/contracts/infrastructure-control/candidate-v1",
      "executor": {
        "command": ["/deployment-owned/python", "-B", "-m", "ops.infrastructure.control", "--config", "/deployment-owned/control.json"],
        "directory": "/deployment-owned/node-package"
      }
    },
    "timeout_ms": 10000,
    "poll_interval_seconds": 30,
    "stale_after_seconds": 120
  }
}
```

以上均为路径占位示意，没有可直接执行的生产默认值。节点控制配置由 TS-205/208 登记，引用已有 observation_config 和 SSH 凭据来源；平台不读取或保存 SSH 私钥。缺少 control 段显示“未配置”，旧只读功能继续使用。

`Executor.exchange` 是两个协议唯一的受限进程交换，保留原环境白名单、JSON 大小、超时、进程树及 Linux supervisor/Windows Job Object 生命周期。`ControlClient` 只增加独立版本与固定 schema 校验，不另写 subprocess 或 SSH 实现。Web 控制入口复用现有 Cookie/CSRF/Origin 和 `external.manage`；普通动作直接受理。只有 `control/plan` 的 JSON 体上限为 1 MiB，其余入口限额沿用原值；compose_content 解码后最多 262144 字符。

## 领域归属与持久性

- `control.py`：后台工作与发送/恢复规则；`control_repository.py`：SQLite 原子领取、租约、幂等与同目标串行；`control_client.py`：节点版本与响应语义；`control_projection.py`：脱敏用户投影；`control_web.py`：请求验证与缓存读取。原 runtime 只装配生命周期与跟踪自身 LocalWork 完成。
- 控制表写入既有 `<database_path>.infrastructure.sqlite`，沿用其持久目录和备份范围，没有新的 tmpfs 目录或第二套任务执行库。节点自己的 relay journal、远端 journal 和配置版本也必须由部署登记到持久备份范围，不能只备份平台台账。
- `control_operations` 保存 client_id、完整目标、连接修订、内部待执行 payload 和公开回执；`control_plans`/`control_plan_uses` 按 node_id + plan_id 绑定计划和唯一执行。内部 payload 可能包含新配置输入，属于私有可备份数据库，绝不通过操作记录、任务中心、错误或运行日志投影。
- 普通页面轮询只读取缓存；首次或显式刷新登记后台读取，同目标已有读取时复用。库存与详情的失败保留上次样本但标明陈旧，不把解析错误或命令错误视为空成功。
- 任务中心 `platform.infrastructure` 只读本模块 action/execute 台账，中文动作与已核对目标名、完整 ID 详情和 acceptance/command/runtime/health 证据来自同一记录；入口是现有 `#/home/1`。普通刷新不会填满任务列表。

## 执行与恢复

新 client_id 先在 `BEGIN IMMEDIATE` 中写受理意图；相同 client_id/相同语义返回原记录，不同语义冲突。多个进程共享 SQLite 时以 owner/lease 比较交换领取，发送前持久写 sent 位并复核原连接修订。同栈容器动作和栈动作排队；无法取得可靠栈映射的容器保守占本节点串行位，节点仍独立执行项目 OS 锁校验。

发送后不因超时、HTTP 取消、页面离开或进程重启重新 submit，只向原 node_id、原 operation_id 查询 `operation.get`。即使发送记录已落盘而实际尚未出进程，恢复也保守读回，不猜测未发送。收到并校验的 conflict/unsupported/invalid 明确拒绝可以终态失败；失联及无法判断的 unavailable 保持未知。

`execution_active` 为 true/null 时保留同栈占位；节点明确 false 才可释放。运行观测与命令状态分别展示，未配置 healthcheck 映射 none，不显示为健康；absent/mixed 原样保留。节点已终态但 command unknown 的操作保持 unknown，可以刷新原操作，不转成一次新的动作。连接修订改变后旧队列不向新登记发送，旧未知也不向新配置查询。

执行结束自动排后台控制库存、相关详情及原只读容器刷新。自身本地 SQLite 工作经现有 LocalWork 执行，取消不能释放仍在运行的事务；基础设施 close 等待自己提交的本地工作完成，避免“已关闭”后还有数据库文件被占用。

创建、配置变更、镜像更新、恢复均先生成具体计划，再提交 plan_id 执行；节点在执行时再次核对完整输入与归属，外部编辑令旧计划失效。配置编辑使用节点 opaque 标记保留秘密，`.env` 不经过浏览器。计划差异和镜像/端口/挂载摘要经过节点脱敏后才投影；新输入中的秘密同样由节点屏蔽。恢复只涉及配置与运行版本，不能声称回滚已写入的数据。

受 guard 保护的平台服务和 Dockge 执行依赖返回维护归属与不可用原因，普通控制拒绝，不停止 guard、不发通用 Compose 替代命令。未配置专用维护接点时不能伪装成功。

## 隔离验证

私有 venv 使用项目既有 requirements-dev.txt；固定合同环境由协调者提供。后端用例是实际本地 HTTP、真实会话/CSRF、真实 SQLite、多实例以及固定执行器子进程，合成节点不连接 Docker、SSH 或 NAS：

```powershell
$env:TS012_CONTRACT_DIR='<固定合同包>/text-dialogue/v1'
$env:TS177_NODE_CONTRACT_DIR='<固定合同包>/infrastructure-node/candidate-v1'
$env:TS205_CONTROL_CONTRACT_DIR='<固定89f93d4合同>/contracts/infrastructure-control/candidate-v1'
$env:PYTHONPATH=(Get-Location).Path
.runtime/venv/Scripts/python.exe -m unittest discover -s tests/backend -p test_infrastructure_control.py -v
.runtime/venv/Scripts/python.exe -m unittest discover -s tests/backend -p test_infrastructure.py -v
.runtime/venv/Scripts/python.exe -m unittest discover -s tests/backend -p test_tasks.py -v
```

浏览器夹具（独占检查过的新本地端口，默认 4837）支持 TS207_WEB_DIST 或 --web-dist 指向前端独立构建，不占用旧 4827，也不复用未知进程：

```powershell
.runtime/venv/Scripts/python.exe tests/backend/dockge_fixture.py --port 4837 --register-node --web-dist '<前端检出>/apps/web/dist'
```

仅夹具账号 `synthetic-admin` / `synthetic-local-password-014`，节点 fixture-nas，普通栈 fixture-web，受保护栈 tianshu-platform。没有 --register-node 时可走原页面登记流程。夹具启动输出 synthetic_dockge_ready，退出清理自己的临时设置与数据库。此夹具与浏览器路由替身不同，但仍是合成节点协议；实际 TS-205 节点实现、Docker 隔离栈和 NAS 实机验收必须分别记录，不能据此宣称上线。
