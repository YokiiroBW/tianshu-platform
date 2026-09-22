# 平台安全运行日志与只读探针

本页说明 TS-100 的运行日志契约、持久性语义和只读探针契约。事件结构本身是**已冻结合同**
`contracts/diagnostics/v1`（1.0.0），本页只描述平台侧如何实现它，不重述、不改写合同字段。

部署命令与未验证清单见[生产静态容器部署](deployment.md)。

## 1. 冻结合同与显式校验

| 项目                     | 值                                                                          |
| ------------------------ | --------------------------------------------------------------------------- |
| 合同目录                 | `contracts/diagnostics/v1`（版本 `1.0.0`）                                  |
| manifest 原始字节 SHA256 | `5d89f7a21637fd57cea4a236e17f8d8c4917799497ff44f87ee68ea614d4805f`          |
| 状态                     | `development_frozen_pending_joint_acceptance`                               |
| 校验文件                 | `README.md`、`event.schema.json`、`examples.json`、`negative-examples.json` |

启动与每次就绪检查都会**逐字节**核对 manifest 覆盖的四个文件：先算原始字节哈希，再要求
`event.schema.json` 能编译、正例通过、反例全部被拒。行尾被规范化、文件被替换或缺失都会让
`contract` 检查转红。manifest 登记的是**已发布的原始字节**（本仓库为 CRLF），因此不允许任何
形式的行尾重写——把 CRLF 改成 LF 会让哈希不匹配，这是刻意设计，不是缺陷。

合同目录来自设置 `diagnostics.contract_directory`（或环境变量
`TIANSHU_DIAGNOSTICS_CONTRACT_DIR`），`service_https` 模式下**必须存在且通过校验**，否则
`/health/ready` 返回 503。可选的 `diagnostics.expected_manifest_sha256` 用于固定某个具体修订。

## 2. 事件记录

每条记录是 12 个固定字段的 JSON 对象，写成一行 UTF-8 JSONL（LF 结尾，无 CR），**单行不超过
4096 字节**：

`schema_version`、`timestamp`、`service`、`instance_id`、`sequence`、`event_id`、`level`、
`event`、`outcome`、`correlation_id`、`duration_ms`、`error_code`。

- `instance_id`、`event_id` 为 UUID；`sequence` 在单个实例内单调递增。
- `service` 取 `platform`、`companion`、`memory`、`memory-knowledge`、`gateway`；平台自身固定写 `platform`。
- `level` 取 `DEBUG`、`INFO`、`WARNING`、`ERROR`、`CRITICAL`；`outcome` 取
  `started`、`succeeded`、`failed`、`cancelled`、`unknown`、`rejected`、`degraded`。
- `correlation_id` 是 32 位小写十六进制；`NaN`/`Infinity` 不是合法 JSON 数字，会被拒绝。
- 字段集合封闭：多一个字段、少一个字段、值不在枚举内都会被本地构造器拒绝（因此不会写出违约行）。

**全量注册、不采样**：每一次请求、每一次出站调用、每一次鉴权结果都写一条，不存在抽样开关。

### 事件名（26 个）

| 分组     | 事件                                                                                                                                                     |
| -------- | -------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 生命周期 | `runtime.starting`、`runtime.started`、`runtime.startup_failed`、`runtime.stopping`、`runtime.stopped`                                                   |
| 配置合同 | `config.loaded`、`config.load_failed`、`contract.loaded`、`contract.verify_failed`                                                                       |
| HTTP     | `http.request.started`、`http.auth.succeeded`、`http.auth.rejected`、`http.request.finished`、`http.request.unknown_path`                                |
| 出站调用 | `outbound.call.queued`、`outbound.call.started`、`outbound.call.succeeded`、`outbound.call.failed`、`outbound.call.timed_out`、`outbound.call.cancelled` |
| 日志自身 | `logging.segment_sealed`、`logging.unavailable`、`logging.capacity_exhausted`、`logging.recovered`                                                       |
| 命令行   | `cli.action.started`、`cli.action.finished`                                                                                                              |

出站调用覆盖四条真实外部路径：核心网页对话（`transport`）、静态资源上游（`assets`）、家庭设备
（`home`）、人格服务（`persona_client`）。`queued` 只在确实要发时才写；取消写 `cancelled` 而不是
`failed`，因为「没有发出」与「发出失败」不是同一件事——取消时若请求**从未发出**，`error_code`
写 `outbound_not_sent`，已经发出的则留空：日志层的结论只覆盖「发没发出」，不替业务判断结果。

鉴权结果只在**确实做过鉴权并且成功**时才写 `http.auth.succeeded`：公共静态页、未知路径、鉴权前的
输入拒绝（如内容类型不对）、以及只是带了某个 `Cookie` 或 `Authorization` 头的请求都**不写**它
（保持「未尝试」）；凭据被拒写 `http.auth.rejected`，未知路径另有 `http.request.unknown_path`。
判定证据只有两个，且都由真正做鉴权的代码显式给出：控制台**校验通过操作员密码**，或请求**出示了
本部署签发、未过期且指纹匹配的会话**；内部端口则要求该 Bearer 真的通过了鉴权代码。没有证据就
没有结论——头部的存在从来不是证据。

## 3. 相关性

入站请求的 `X-Tianshu-Correlation-Id` 若合法（32 位小写十六进制）则沿用，否则生成新的；出站调用
一律带上同一个值。因此「一次用户动作」在日志里是一条可串起来的链，而不是靠时间戳猜测。
响应同样回带该头，便于把客户端现象与日志记录对上。

## 4. 持久性、容量与故障

日志目录下按实例分段：`{service}-{instance_id}-{index:04d}.jsonl`，单段上限 64 MiB，段满即封存
并写一条 `logging.segment_sealed`。目录总量上限默认 1 GiB（可配 32 MiB–64 GiB，越界即拒绝设置）。

| 状态          | 含义                                            | 就绪影响                               |
| ------------- | ----------------------------------------------- | -------------------------------------- |
| `durable`     | 队列已落盘并 fsync，可对外确认                  | `logging: ok`                          |
| `recovering`  | 写失败后正在探测恢复，**仍不接受新业务**        | `logging: failed`，**永不 ready**      |
| `non_durable` | 开发/演练模式（无日志目录），只在标准错误上输出 | `logging: non_durable`，**永不 ready** |
| `unavailable` | 目录不可写、写失败、队列满、冲刷超时            | `logging: failed`，**永不 ready**      |

关键规则：

- **先 fsync 再确认**。`emit_durable` 返回成功意味着记录已在磁盘上；`_synced` 只在 fsync 返回后
  才推进。因此「已确认」不等于「可能还在内存里」。
- **一个写线程独占文件**。开段、写入、fsync、封存、关闭都由同一个线程完成；调用方只等待结果，
  从不去碰文件描述符。慢盘只让调用方等到自己的上界，不会阻塞事件循环，也不会让别人替它写。
- **等待分「被唤醒」与「已确认」两件事**。写失败会唤醒等待者并让它们**失败返回**，而不是让它们
  以为写成功了：`admit_durable` / `finish` / `close` 只有在确认（真实 fsync 返回）后才返回真值。
- **终态先落盘再回答**。业务请求的 `http.request.finished` 在响应发出前确认，因此客户端拿到
  200/4xx 时该记录已在磁盘上。出站调用的终态（`outbound.call.succeeded/failed/timed_out/cancelled`）
  同样在返回前确认，上界是 `terminal_timeout`（默认 250ms）。
- **取消的终态由 sink 负责，不由被取消的任务负责**。取消路径先 `hand_over(sequence)` 把这条记录的
  责任交给 sink，再在上界内 `settle`。因此：二次取消只结束等待、**不结束责任**；上界到期就
  **准确转不可用**并把该序号计入 `unconfirmed`；已发生的外部副作用不重发，业务结果不变。
- **关闭是结算，不是系统调用结果**。`close`/`flush` 只有在「所有已受理事件已 fsync **且**分段已
  封存」时才为真：空文件封存成功不能把未写入的队列说成成功，封存失败永远不为真，失败也不会被
  后续重复 `close`/`flush` 覆盖。超预算返回假只表示「未能确认」，与「已持久」是两件事，二者不
  互相冒充。
- **未确认事件保留可核对状态**。写失败、队列满、冲刷/关闭/终态超时都会把**尚未确认的序号**计入
  `unconfirmed`（按不同序号计数，只增不减）；这些事件仍留在队列里等待显式恢复，既不靠丢队列，
  也不靠无限等待来「变绿」。
- **有界队列 8192**。入队满则 `logging.unavailable` + `log_queue_full`，同样不静默丢记录；被拒绝
  的写入不会消耗序号，已受理的记录也不会因为后来的拒绝被丢弃。
- **重发只重发本实例已写过的记录**。`retransmit` 校验实例、服务与序号身份，只补写既有记录，绝不
  推进水位线、也绝不替还在排队的记录背书。
- **恢复是显式的**。目录重新可写后先进入 `recovering`（仍拒绝业务），**只有一次真实 fsync 成功**
  才回到 `durable` 并写 `logging.recovered`；未真正恢复前不会声称恢复，也不会在恢复期间放行业务。
- 冲刷与关闭都有上界（默认 5 秒），不会无限等待磁盘。

## 5. 秘密不进日志

记录里**没有**消息正文、请求体、响应体、URL 查询串、凭据值、cookie 或路径。错误只写**固定
原因码**（32 个已登记码，如 `unauthorized`、`version_conflict`、`log_capacity_exhausted`），
未知异常统一折叠为 `internal_error`。这一层由 `diagnostics.safe_code()` 收口：任何非登记字符串
都不会进入 `error_code` 字段。设置读取失败时终端只输出固定码，不回显路径、异常文本或文件内容。

## 6. 只读探针

| 端口                | 认证               | 写日志 | 语义                     |
| ------------------- | ------------------ | ------ | ------------------------ |
| `GET /health/live`  | 无                 | 否     | 事件循环还在             |
| `GET /health/ready` | 就绪凭据（Bearer） | 否     | 业务是否可用；不可用 503 |

- **探针自身纯只读**：不写日志、不建库、不迁移、不改任何文件。数据库一律以 `mode=ro` 打开
  （从不用 `immutable`，那会读到一个可能与现实不符的快照），并设 `PRAGMA query_only=ON`。
  未通过就绪凭据的探针请求同样不写日志——探针不是可被用来放大日志量的入口。
- **就绪文档是闭集**：`{status, service, checks}`；`checks` 恰好九个固定键，值只能取
  `ok`、`failed`、`not_configured`、`not_verified`、`non_durable`。不含路径、版本、凭据或自由文本。
- `not_configured` 表示部署**刻意**没有该能力（例如没有登记家庭设备时没有
  `.home-controls.sqlite` 台账），不是故障；`not_verified` 表示**未知**，并且和 `failed` 一样
  阻塞就绪——未知不等于健康。
- 探针有 2 秒预算与 1 秒缓存：拿不到锁或超预算时，所有检查报 `not_verified` 而不是阻塞等待。
  缓存记的是**本次检查开始的时刻**，不是结果到达的时刻：过了预算或过了 TTL 的迟到结果落地即为
  陈旧，下一次请求会真的重新检查，而不会把旧快照当成新鲜绿灯。并发调用**不会**再起第二次核对：
  后来者直接得到 `not_verified`，取消也不会把核对堆起来，核对线程结束后 owner 立刻释放。
- 未配置就绪凭据时，`/health/ready` 返回 503 而不是「通过」：**没有配置不等于通过**。
  就绪凭据必须独立于任何业务身份凭据，复用会被启动时拒绝；凭据**每次请求**都从环境读取，因此
  轮换立即生效，空值/空白值与业务凭据值同样返回 503（而不是放行）。

### 九个检查项

| 键            | 核对内容                                                                                       |
| ------------- | ---------------------------------------------------------------------------------------------- |
| `config`      | 设置可解析、`diagnostics` 段合法、发布生命周期等启动纯校验（与 `Models` 同一函数，不复制规则） |
| `contract`    | 诊断合同原始字节哈希、schema、正反例                                                           |
| `store`       | 权威库可只读打开、`user_version` 受支持、权威表齐全                                            |
| `sidecars`    | 本部署**实际拥有**的 sidecar 台账可读（未启用的能力不索要台账）                                |
| `web_static`  | 静态构建完整：入口存在且其引用的资源都在                                                       |
| `tls`         | 证书与私钥配对、当前时间在有效期内                                                             |
| `credentials` | 已登记的凭据变量都在环境里（不表示对端可达）                                                   |
| `logging`     | 日志状态（`durable` 才是 `ok`；`recovering` 与 `unavailable` 都是 `failed`）                   |
| `runtime`     | 运行状态（`running` 才是 `ok`）                                                                |

## 7. 明确不做

- 不做日志聚合、采集、远端传输、保留期删除或压缩：本卡只保证「写得出、可核对、不骗人」。
- 不做采样、不做级别开关、不做运行时改日志目录。
- 不把日志当审计权威：权威状态是权威库与 sidecar 台账，日志目录不参与恢复。
- 不提供公开的日志读取端口：日志只落在挂载卷上，由运维侧读取。
