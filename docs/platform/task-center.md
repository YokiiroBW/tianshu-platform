# 任务中心与平台操作记录

网页“设置 → 任务”（`/#/settings/0`）是本平台已经真实发生的操作的唯一聚合入口。它只读地投影
**各责任模块自己已经持久化的台账**，不新建第二套执行库，也不把未配置、未接入或读不到的东西
显示成成功。

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

## 验证

```powershell
$env:TS012_CONTRACT_DIR="C:\YOKI\Codex\tianshu-peiban-bot\contracts\text-dialogue\v1"
.runtime/venv/Scripts/python.exe -m unittest discover -s tests/backend -p "test_tasks.py" -v   # 最窄 17 项
.runtime/venv/Scripts/python.exe -m unittest discover -s tests/backend                        # 完整 185 项
node node_modules/@playwright/test/cli.js test --config apps/web/playwright.tasks.config.ts     # 浏览器 3 项
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
