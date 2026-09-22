# 平台生产静态容器部署

本页是 TS-100 交付的生产静态容器基础的**唯一权威入口**：镜像怎么建、怎么起、怎么判活、怎么
优雅退出，以及**本机实际执行过哪些命令、哪些没有执行**。构建脚本存在不等于构建通过；未在本机
执行的步骤一律标注「未验证」，不写成通过。

相关页面：[运行与健康探针](diagnostics.md)（事件词汇、容量与探针契约）、[平台后端运行](runtime.md)
（本地开发与既有命令）。

## 1. 产物与边界

| 文件                                    | 作用                                                                                     |
| --------------------------------------- | ---------------------------------------------------------------------------------------- |
| `Dockerfile`                            | 两阶段镜像：`console` 阶段用锁文件构建网页，`runtime` 阶段只带 Python 与非 root 运行用户 |
| `.dockerignore`                         | 构建上下文排除规则：本地状态、测试、密钥、数据、日志、本机 `dist`                        |
| `deploy/platform/settings.example.json` | 生产设置模板：只有环境变量**名**，没有任何凭据值                                         |

镜像只承载**代码、已冻结合同、镜像内构建出的网页**；数据库、日志、TLS 私钥、凭据全部在运行时以卷
或环境变量注入。因此同一镜像可以在环境之间晋升而无需重建，也不存在「镜像里有密钥」这件事。

镜像内固定布局：

| 路径               | 内容                                     | 运行时属性 |
| ------------------ | ---------------------------------------- | ---------- |
| `/srv/tianshu`     | 代码、`contracts/`、`web/`（已构建网页） | 只读       |
| `/var/lib/tianshu` | 权威库与同路径 sidecar 台账              | 可写卷     |
| `/var/log/tianshu` | 诊断日志分段                             | 可写卷     |
| `/etc/tianshu`     | `settings.json` 与 `tls/`（证书与私钥）  | 只读挂载   |

## 2. 构建

镜像**自己**构建网页，不需要在本机先构建：`console` 阶段用 `node:24.19.0-bookworm-slim` 按
`package-lock.json` 执行 `npm ci`，再 `npm run build`（含类型检查）；`runtime` 阶段用
`python:3.12.11-slim-bookworm`，只把 `console` 阶段产出的 `dist` 复制为 `/srv/tianshu/web`。
Node.js 与 npm 只存在于构建阶段，运行阶段镜像里没有它们，也没有任何包管理器。

```bash
docker build --tag tianshu-platform:1.0.0 .
```

`.dockerignore` 刻意排除本机的 `apps/web/dist`：镜像里的控制台只能来自镜像内的构建阶段，不可能
是作者机器上遗留的产物。因此「构建成功」只由 `docker build` 自己证明，本机预先 `npm run build`
既非必要也不影响镜像内容（本地预览仍可用它）。

`COPY --from=console /build/apps/web/dist/ ./web/` 会在构建阶段没有产出时直接失败：这是刻意的，
宁可构建报错，也不发布一个打不开的控制台。

## 3. 起服务

准备一份填好的设置（见第 6 节），把它与证书挂载进去：

```bash
docker run --detach \
  --name tianshu-platform \
  --read-only \
  --tmpfs /tmp \
  --volume /srv/tianshu/etc:/etc/tianshu:ro \
  --volume /srv/tianshu/data:/var/lib/tianshu \
  --volume /srv/tianshu/logs:/var/log/tianshu \
  --publish 8443:8443 \
  --env TIANSHU_ADMIN_TOKEN \
  --env TIANSHU_CONNECTOR_TOKEN \
  --env TIANSHU_COMPANION_TOKEN \
  --env TIANSHU_MEMORY_TOKEN \
  --env TIANSHU_GATEWAY_TOKEN \
  --env TIANSHU_DIAGNOSTICS_TOKEN \
  tianshu-platform:1.0.0
```

入口固定为 `python -m services.platform`，默认子命令是
`--settings /etc/tianshu/settings.json serve --host 0.0.0.0 --port 8443`。只有 `serve` 会绑定
端口；`local` 子命令是本地隔离演练入口，**不属于部署**。

`mode` 必须是 `service_https`：该模式下所有业务与探针端口都要求真实 TLS，非 TLS 请求在到达
处理器之前就被拒绝。不要用反向代理终结 TLS 来绕过这一要求。

### 滚动前的只读预检

```bash
docker run --rm \
  --volume /srv/tianshu/etc:/etc/tianshu:ro \
  --volume /srv/tianshu/data:/var/lib/tianshu \
  --volume /srv/tianshu/logs:/var/log/tianshu \
  --env TIANSHU_DIAGNOSTICS_TOKEN \
  tianshu-platform:1.0.0 \
  --settings /etc/tianshu/settings.json preflight
```

`preflight` 不构造组合根、不建库、不迁移、不写日志、不绑定端口，只读地核对设置、合同哈希、
已有权威库与 sidecar 台账、静态构建、证书有效期与凭据是否在环境中。输出是固定的 JSON
（`status`、`service`、`checks`、`reasons`、`requires_initialization`），退出码 `0` 表示
`ready`，`1` 表示尚不可用。首次部署的库还不存在时，答案是
`requires_initialization: true`——这是「还没初始化」，不是「坏了」。

## 4. 健康与就绪

| 端口                | 认证                                               | 语义                             |
| ------------------- | -------------------------------------------------- | -------------------------------- |
| `GET /health/live`  | 无                                                 | 进程与事件循环还在，仅此而已     |
| `GET /health/ready` | `Authorization: Bearer $TIANSHU_DIAGNOSTICS_TOKEN` | 业务是否真的可用；不可用返回 503 |

```bash
curl --fail --silent --show-error \
  --cacert /srv/tianshu/etc/tls/server.pem \
  https://127.0.0.1:8443/health/live

curl --silent --show-error \
  --cacert /srv/tianshu/etc/tls/server.pem \
  --header "Authorization: Bearer ${TIANSHU_DIAGNOSTICS_TOKEN}" \
  https://127.0.0.1:8443/health/ready
```

就绪文档是闭集：`{status, service, checks}`，`checks` 恰好九个固定键
（`config`、`contract`、`store`、`sidecars`、`web_static`、`tls`、`credentials`、`logging`、
`runtime`），每个值只能取 `ok`、`failed`、`not_configured`、`not_verified`、`non_durable`。
`failed`、`not_verified`、`non_durable` 都会让 `status` 变成 `not_ready`：**未知不等于健康**，
日志非持久也不等于健康。文档里不含任何路径、版本、凭据或原因码以外的信息。

`credentials` 为 `ok` 只表示**已登记的凭据变量都在环境里**，不表示对方服务可达；真实对端可用性
不在本卡范围。

镜像自带的 `HEALTHCHECK` 走的就是 `/health/live`，但**不是**用 `curl`：它调用产品自己的
`python -m services.platform healthcheck --url https://127.0.0.1:8443/health/live --ca-file
/etc/tianshu/tls/ca.pem`。这条命令用 `ssl.create_default_context(cafile=...)`，即**保持主机名校验、
要求证书链**：不校验的探针会在真正入口已经坏掉时仍然报活。因此证书必须把探针使用的主机名写进
SAN——容器内用 `127.0.0.1` 连接时，证书需要带 `IP:127.0.0.1`（或改成解析到本容器且 SAN 匹配的
名字），否则健康检查会**正确地**失败。`--ca-file` 指向挂载进 `/etc/tianshu/tls/` 的 CA/证书。

就绪检查需要凭据，因此交给编排器按上面的 `curl` 执行，不放进 `HEALTHCHECK`——重启决策只应由
活性决定。

## 5. 退出、容量与故障

- `STOPSIGNAL SIGTERM`：收到后停止接收新连接、冲刷队列、写 `runtime.stopping` 与
  `runtime.stopped`，再退出。刷新有界（5 秒），不会无限等待。
- 关闭是**有界**的：`close` 先立刻拒绝新业务，再在预算内等唯一写线程收尾；超预算时返回「未确认」
  而不是继续等待，也绝不从调用方线程去碰那个文件描述符。退出前落盘的事件由写线程负责收尾，
  超时会被计数（`unconfirmed`）并反映到就绪检查，不会被谎报成已确认。
- 业务请求的终态事件在**回答之前**确认：正常请求的 `http.request.finished` 先 `fsync` 再返回，
  因此「客户端拿到了 200」与「这条记录已经落盘」是同一件事。被取消的请求做不到这一点（任务已
  被拆除、无法再等待），此时终态记录的所有权移交给写线程，不假装已确认。
- 日志目录达到上限（默认 1 GiB，可配 32 MiB–64 GiB）时：**拒绝新业务并置 `ready=false`**，
  而不是丢掉记录继续假装成功。已发出的外部副作用不因日志失败而重发。
- 写失败后进入 `recovering`：该状态**不接受**新业务、就绪检查为 `failed`，只有一次真实
  `fsync` 成功才回到 `durable`；恢复期间既不谎报持久，也不丢弃已受理的记录。
- 日志目录或权威库所在卷不可写时：`logging` / `store` 检查转红，`/health/ready` 返回 503。
- 数据库与 sidecar 台账必须与 `/var/lib/tianshu` 一起备份；它们是权威状态，日志目录不是。

## 6. 设置模板

`deploy/platform/settings.example.json` 是模板，下列占位符**必须替换**后才能启动：

| 位置                                                | 替换为                              |
| --------------------------------------------------- | ----------------------------------- |
| `principals.*.token_env`                            | 已登记的身份凭据变量名（保持大写）  |
| `principals.admin.account.immutable_account_id`     | 真实操作者账号                      |
| `entries.config-entry.account.immutable_account_id` | 同上                                |
| `entries.config-entry.channel.binding_id`           | 真实绑定 ID                         |
| `entries.config-entry.actor_id`                     | 真实 actor ID                       |
| `web.origin`                                        | 真实对外源（`https://` 开头）       |
| `web.username`                                      | 操作者名                            |
| `web.password_hash`                                 | 由 `web_console.password_hash` 生成 |

模板中的 `password_hash` 是**形状合法但不可登录**的占位值（全零），不会被误当成真实口令；
`providers` 为空、`web.dialogue_enabled` 为 `false`、`native_config_http` 为 `false`，即默认
不打开任何未配置的能力。

`diagnostics.ready_token_env` 指向的就绪凭据必须**独立于**任何业务身份凭据：复用管理员或某个
服务身份的变量会被启动时拒绝（`invalid_input`）。

## 7. 本机实际执行结果与未验证清单

作者机器上**没有 Docker**，因此以下区分是硬事实，不是保守措辞：

| 项目                                                  | 状态                                                                           |
| ----------------------------------------------------- | ------------------------------------------------------------------------------ |
| `tsc -p apps/web/tsconfig.json --noEmit`              | 已执行，退出码 0                                                               |
| `vite build --config apps/web/vite.config.ts`         | 已执行，退出码 0，产出 `apps/web/dist`                                         |
| 真实构建产物由平台以 `service_https` 提供并检查       | 已执行（真实 HTTP/TLS 套件，见下）                                             |
| 镜像定义、忽略规则、设置模板的静态核对                | 已执行（`tests/backend/test_runtime_container.py`）                            |
| `healthcheck` 子命令（TLS 校验、超时、非 JSON 响应）  | 已执行（进程内真实 TLS，见 `tests/backend/test_runtime_health.py`）            |
| `docker build`（含镜像内 `npm ci` + `npm run build`） | **未验证**：本机无 Docker，套件显式 skip                                       |
| `docker run`、容器 `HEALTHCHECK`、`SIGTERM` 退出      | **未验证**：同上                                                               |
| 一次性 `preflight` 容器不写数据                       | **未验证**：同上（进程内 `preflight` 只读性已单独验证）                        |
| Linux 上的 `SIGTERM` 路径                             | **未验证**：本机 Windows 无 `loop.add_signal_handler`，回退分支未在 Linux 执行 |
| 生产 NAS、真实证书链、真实账号、生产库恢复            | **未验证**：本卡不做，也不部署                                                 |

上述未验证项在交付记录中标注 `needs_validation`，并由协调方在具备容器运行时的环境中执行。
**不要**把本节读成「已通过」：`Dockerfile` 存在不等于构建通过。

可复现的验证命令（隔离环境，无 Docker 也能跑）：

```bash
.venv/Scripts/python.exe -m unittest discover -s tests/backend -p 'test_runtime_*.py' -v
```

该命令在本机结果为 `Ran 176 tests ... OK (skipped=4)`；跳过项就是上表中标注未验证的容器运行时
项目，跳过原因是明确写出的，不当作通过。
