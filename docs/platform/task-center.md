# 任务中心与平台操作记录

网页“设置 → 任务”（`/#/settings/0`）是本平台已经真实发生的操作的唯一聚合入口。它只读地投影
**各责任模块自己已经持久化的台账**，不新建第二套执行库，也不把未配置、未接入或读不到的东西
显示成成功。

可见页面每 15 秒重新读取首屏。成功轮询同时替换列表与分页游标，开始新的遍历；已展开的后续页可再次加载。这样新增超过一页时不会沿旧游标跳过中间记录，改变状态后不匹配当前筛选的旧记录也不会留在新快照中。隐藏页面暂停轮询，恢复可见时重新读取；连接失败时明确保留上次成功快照。

## 数据从哪里来

| 来源 id           | 代表性       | 记录来自                                                                                                                                                                                           |
| ----------------- | ------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `platform.home`   | 设备控制     | `<db>.home-controls.sqlite` 的 `control_intents`（与读数表一起）                                                                                                                                   |
| `platform.models` | 模型配置发布 | 权威库 `audit`（`config.publish` / `native_config.publish` / 各自 `revoke`）与 `configs` / `native_configs`，加 `<db>.web-models.sqlite` 的 `publication_intents` 里**没有变成权威版本**的领取记录 |

未接入的产品不会被猜成空列表：

| 来源 id              | 状态            | 理由码                |
| -------------------- | --------------- | --------------------- |
| `companion.core`     | `not_connected` | `no_task_contract`    |
| `assets.remote`      | `not_connected` | `no_task_contract`    |
| `resources.download` | `not_connected` | `no_task_contract`    |
| `platform.dialogue`  | `not_connected` | `no_operation_ledger` |

本卡不跨产品读数据库：没有正式任务合同的产品就写“未接入”，不伪造记录。

## 同一套状态词汇

五个状态，任何来源都必须落到其中之一，不允许把“已受理”“执行中”“已观测”“未知”“失败”
折叠成一个绿点：

| 状态          | 含义                           | 家庭设备映射            | 模型发布映射                                                |
| ------------- | ------------------------------ | ----------------------- | ----------------------------------------------------------- |
| `accepted`    | 已被受理，执行结果还没有依据   | `prepared` / `accepted` | —                                                           |
| `in_progress` | 已认领或已发出，结果未定       | `observing` / `sending` | —                                                           |
| `observed`    | 有后续观测或权威记录作为依据   | `observed`              | `published`                                                 |
| `unknown`     | 结果无法确认；平台不会自动重发 | `unknown`               | `audit_without_authority` / `corrupt` / `intent_unresolved` |
| `failed`      | 执行方明确拒绝或明确失败       | `rejected`              | —                                                           |

每条记录另外带：`stage`（更细的阶段词）、`code`（责任模块自己的原因码）、`source_state`
（`current` / `origin_missing` / `source_missing`）、`attention`、`evidence`、`module.page`。
后端只给技术词，中文标签与解释性文字由网页持有（沿用平台既有约定）。

`updated_at` 是“结果落库”的时间，没有结果时就是受理时间；`pending` 表示结论还没有落定，
因此一条已经拿到回执、但还没有观测的设备控制会同时是 `settled=true` 与 `pending=true`——
“回执已落库”和“结论未定”是两件事。

## 分页与游标

- 排序固定为一个不可变位置：`(整数秒, task_id)` 倒序；同一秒内按 id 排序，位置永不重算。
- 每页 `page_size` 默认 10，允许 1–50。响应给 `page.next_cursor` 与 `page.has_more`。
- 游标是 base64url 的 JSON，只含版本号、筛选摘要、各来源各自的位置和水位线；不含任何记录内容。
- 游标绑定排序与筛选：换了 `status` 或 `source` 再带旧游标会得到 409 `cursor_conflict`；游标格式
  不合法是 400 `invalid_input`。
- 水位线保证遍历严格有序且不重复：比游标更新的记录（遍历期间新写的）会出现在下一次“第一页”，
  不会插到遍历中间，也不会重复。
- 每个来源各读 `limit + 1` 行再做 k 路归并，家庭与模型两条流互不影响。

## 详情是按需重读

列表是快照，`POST /api/web/tasks/detail` 是这一刻的重新读取：记录已经被撤销、来源登记被移除、
内容摘要不一致等都会在详情里如实反映，不会拿列表里的旧结论糊过去。未知 `task_id` 是 404。

## 取消

本卡**不提供取消**，两个真实的理由码：

- `executor_does_not_cancel`（设备控制）：已经发出的指令无法撤回，平台也不会自动重发；要改变
  状态请到“家庭与服务 → 家庭设备”重新操作。
- `recorded_fact`（模型发布）：发布是既成事实，产品做法是发布新版本而不是抹掉记录。

记录里始终带 `cancel: {supported: false, code: ...}`，网页只解释、不放一个按不动的取消按钮。

## 端口与授权

- `POST /api/web/tasks/view`：一页记录 + 各来源状态。
- `POST /api/web/tasks/detail`：一条记录的实时重读。

两者都只用同源网页会话：真实 Cookie、CSRF、Origin / Sec-Fetch-Site 校验与读后会话复核，
**不需要** `models/unlock` 或 `home/unlock`（这里没有任何写权限）。未登录 401 `unauthorized`，
会话被撤销 `session_expired`。没有新增 `/internal/v1/*` 端口。

轮询用前端 15 秒的可见性暂停定时器，不引入新的事件总线。

## 会话失效、断线与迟到回答

网页把每一次读取失败分成两类，绝不混为一谈：

| 失败                         | 判据                                                                | 网页行为                                                                                     |
| ---------------------------- | ------------------------------------------------------------------- | -------------------------------------------------------------------------------------------- |
| 会话已失效（过期/撤权/CSRF） | HTTP 401 或 403（`unauthorized` / `session_expired` / `forbidden`） | 清空记录、详情、来源与分页游标，重读一次会话，显示“登录已失效，请重新登录”与登录表单         |
| 读取失败（断线、后台不可用） | 传输错误或 5xx                                                      | 保留已读到的快照，明确标注“下面仍是上次成功读取的快照；恢复后这里会自动更新”，不显示登录表单 |

“退出登录后不再显示任何操作记录”不能只在导航离开再回来时才成立：另一个标签页退出登录（或会话
过期、权限被撤销）时，仍然停在旧记录上的这个标签页会在下一次轮询拿到 401/403，此时记录、已展开
的详情与分页游标都属于已经失效的会话，必须一起清掉；会话失效后不再轮询。

迟到的回答不能写回页面。每个请求带三个身份：它问的筛选、它所属的会话代号、它所在的槽位（列表 /
详情 / 轮询）。回答回来时若不是这三个身份中的任何一个当前值，就直接丢弃：

- 换筛选后，上一个筛选的迟到回答不会把旧记录写回（筛选在触发的同一刻记录，早于任何 await）；
- 会话失效后，还在路上的轮询或详情回答不会把过期视图带回来，离开页面也会取消它们；
- 同一个槽位的新请求会取消上一个，因此“加载更多”和“重新读取”不会与更早的读取互相覆盖；
- 轮询读的是最新一页，因此它带回的“面板从未显示过”的记录属于列表最前面（按后台返回的顺序），
  已经显示的记录保持原位并换成更新的说法；一条记录永远不会被列两次。

浏览器回归覆盖这些情况：真实另一标签页退出（记录归零、详情消失、不再轮询、迟到的旧回答无效、
重新登录后记录仍在）、合成会话过期替身、轮询带回的新记录落在最新位置且不重复、真实请求被中断
后的断线提示与恢复、以及真实迟到回答与筛选切换的竞争。

## 开发模式（StrictMode）下的挂载

开发模式（`npm run dev`）里的 `main.tsx` 用 React `StrictMode`，开发模式的 effect 会按
setup → cleanup → setup 复演一遍；生产构建不经过这一遍，所以“构建产物里能读出来”并不等于开发
路径也能读出来。这里的约定是成对的，别把它反过来：

- cleanup 只做两件事：把这个挂载标记为不活跃、取消它自己发起的读取。它**不**写任何“永久关闭”
  的标记；
- 每个 setup 重新标记为活跃。旧 setup 的请求靠身份判断被丢弃（已取消、已被替换、不属于当前
  会话代号），因此复演不会把旧回答写回来；
- “这个筛选的第一页读过了没有”也是一个 setup 的属性：被取消而没得出结论的读取不算读过，
  下一个 setup 会重新问一次。筛选变化、切走再回来、以及开发模式复演，在这里是同一件事。

没有这一条，开发模式会永远停在“正在读取操作记录…”：复演取消了自己的第一次读取，而“已问过”
的标记又让第二次 setup 不再重试。回归在 `apps/web/tests/task-center-dev.spec.ts`
（配置 `apps/web/playwright.tasks-dev.config.ts`：真实合成后台 + 真实 Vite 开发服务器），
覆盖首次挂载（未登录 → 登录表单，已登录 → 记录）与切走再回来。

## 验证

```powershell
$env:TS012_CONTRACT_DIR="C:\YOKI\Codex\tianshu-peiban-bot\contracts\text-dialogue\v1"
.runtime/venv/Scripts/python.exe -m unittest discover -s tests/backend -p "test_tasks.py" -v   # 最窄 18 项
.runtime/venv/Scripts/python.exe -m unittest discover -s tests/backend                        # 完整 186 项
node node_modules/@playwright/test/cli.js test --config apps/web/playwright.tasks.config.ts     # 构建产物 8 项
node node_modules/@playwright/test/cli.js test --config apps/web/playwright.tasks-dev.config.ts # 开发模式 1 项
node node_modules/@playwright/test/cli.js test --config apps/web/playwright.web.config.ts       # 受影响套件
```

浏览器套件用 `tests/backend/run_web_fixture.py`（网页 4814、合成 HA 4817）与构建后的
`apps/web/dist`，因此先 `npm run build`。它是单项目、单 worker：记录只增不减，桌面与移动在同一
会话里切视口验证。

## 已知边界

- 没有正式任务合同的产品（Core 对话与写作、资产远端、订阅与下载、网页对话）只显示“未接入”。
- 来源台账读不到时显示“无法读取”，并保留另一条流已经读到的记录；绝不把读失败显示成空。
- 模型发布的“领取但没有变成权威版本”只按领取记录本身的证据显示，不推测发布是否发生过。
- 没有真实生产部署、真实模型供应商或真实设备；这些记录来自本机隔离的合成后台。
