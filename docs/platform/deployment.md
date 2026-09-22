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
已有权威库与 sidecar 台账、静态构建、证书有效期与凭据是否在环境中。它调用的是入口**同一个**纯校验
（含发布生命周期等由各领域模块自己拥有的规则，例如 `config_max_lifetime_seconds` 用的是 `Models`
的纯函数），因此「预检说 ready、启动却被拒」不会同时成立。输出是固定的 JSON
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
  超时会被计数（`unconfirmed`）并反映到就绪检查，不会被谎报成已确认。关停结论是**结算**：只有
  「已受理事件全部 fsync **且**分段已封存」才为真，空文件封存成功不能把未写入的队列说成成功，
  封存失败永远不为真，失败也不会被后续重复 `close`/`flush` 覆盖。
- 业务请求的终态事件在**回答之前**确认：正常请求的 `http.request.finished` 先 `fsync` 再返回，
  因此「客户端拿到了 200」与「这条记录已经落盘」是同一件事。被取消的请求与出站终态走**有界确认**：
  责任先交给写线程，再在终态上界（默认 250ms）内确认，而上界只有一个；**等待本身被第二次取消时
  就在当刻保守结算**（计入 `unconfirmed`、sink 转 `unavailable` 并阻止新业务、注销已终止的等待者），
  因此「没人再等」不会被读成「已确认」；上界到期同样准确转不可用并把该序号计入 `unconfirmed`，
  既不假装已确认也不重发已发生的副作用。
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

## 8. TS-111 正式构建输入（2026-09-22，覆盖第 1/7 节旧构建说明）

镜像现在为 console / builder / runtime 三阶段。保留 Node 24.19.0、Python 3.12.11，
所有 FROM 绑定 linux/amd64 的官方子 manifest digest（不是镜像 ID）；实读 Registry 的
index/manifest/config 摘要及版本环境见 `scripts/build/base-images.json`。摘要固定并不代表拉取过镜像层。
官方来源：[Node](https://hub.docker.com/_/node)、[Python](https://hub.docker.com/_/python)。

console 显式安装 npm 11.6.2，消费根 package.json/package-lock.json、apps/web 的源码与配置，
执行 npm ci 和 npm run build。runtime 只复制该阶段 dist；本机 dist、依赖目录不会入上下文。
builder 固定 setuptools 80.9.0 / wheel 0.45.1，关闭产品构建隔离中的动态解析，先生成非 editable wheel。
`runtime.lock` 仅将既有 requirements-dev.txt 的14个第三方运行包原版本冻结并添加wheel哈希，排除ruff；
哈希来源为固定网关基线 ec20f95e 的 uv.lock，已核该固定Git输入。没有修改pyproject或开发锁/产品依赖版本。
全新 runtime venv 用 --require-hashes / --only-binary 安装后再 --no-deps 安装产品wheel，
pip check 与 `python -I -m services.platform --help` 失败即阻断构建；runtime 不带构建后端或测试工具。

本地复核入口（默认只打印计划；scope必须是本产品.runtime下全新目录，不复用测试环境）：

```text
python scripts/build/check_install.py --scope .runtime/ts111-install-new
python scripts/build/check_install.py --scope .runtime/ts111-install-new --execute
```

它使用调用者Python创建两个新venv，并在源码之外运行已安装module及console入口、四个子命令help，
严格比较运行包完整集合和导入来源，结果为scope/evidence.json。实际本地Python为3.12.14，
与镜像3.12.11补丁版本不同；Windows wheel安装不证明Linux wheel能运行。

产品Git不含contracts目录。协调者必须先在**新私有构建context**放入固定产品源码，
逐包核协调根原manifest字节摘要并复制合同，不能直接用旧根Git合同或规范化行尾。
`python scripts/build/snapshot_contract.py --source <协调根/contracts/包/v1> --target <新context/contracts/包/v1> --manifest-sha256 <协调冻结原字节SHA256> --execute`
为该步骤提供入口（无execute只输出计划）。它验证所有成员后才创建新目标，按manifest声明的hash_basis验核，
但复制始终使用原字节；拒绝错hash、缺失、越界路径及覆盖。逐包分别调用；不自动批准依赖或candidate。
本轮7正式包仅在.runtime中验核复制，续期manifest固定c5017724187c1386b647fcc5b41ab3cb1702f27d6192f3a87fcefb23e7a5a61c。

Linux由协调在显式新合成scope、确认本地Docker上下文后执行（本轮未执行）：

```sh
docker build --platform linux/amd64 --pull --tag tianshu-platform:ts111-review <verified-context>
docker image inspect tianshu-platform:ts111-review --format '{{.Os}}/{{.Architecture}} {{.Id}} {{.Config.User}}'
docker run --rm --network none --read-only --cap-drop ALL --security-opt no-new-privileges --memory 256m --cpus 1 --entrypoint python tianshu-platform:ts111-review -I -m pip check
docker run --rm --network none --read-only --cap-drop ALL --security-opt no-new-privileges --memory 256m --cpus 1 tianshu-platform:ts111-review --help
```

image inspect 的 Id 是本地构建身份，不能填作registry digest。以上只做安装/入口检查，不配置真实数据，
不证明鉴权ready、挂载权限、容器健康检查、SIGTERM或NAS通过；这些由DEP-G统一接线验证。
本地实际：新venv wheel及完整依赖校验通过；6命令入口通过；Node/npm正式安装与网页构建通过（既有>500kB chunk提示）；
9构建输入边界+13镜像定义专项通过0skip。Linux镜像层拉取/build/run、UID/GID实效与NAS均未执行。

计数纠正（协调授权文档窄修）：应用安装集合为14个第三方运行依赖+产品=15；平台runtime另含pip 25.0.1，仅属安装工具，不计入应用依赖。父实现9b45d813fd8c8ff88ec68f34d94d2f096ff22120及其验证证据保持；未重跑无变化安装/构建。
