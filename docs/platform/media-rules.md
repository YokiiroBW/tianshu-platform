# 订阅规则与可解释试算（TS-098）

`services/platform/media/rules/` 是订阅规则的**纯判定与解释**模块：给定一次归一化媒体元数据（TS-090 `normalize_bilibili`）+ 一份订阅规则文档，回答“这条投稿是否应当下载”，并给出可存储、可复核的逐规则解释。

它**不**入队、不建订阅数据库、不访问 B 站、不下载、不注册服务器路由、不读写任何配置文件。未来 TS-092 用它做来源扫描时的过滤；`requires_rule_attention` 是它交给 TS-092 暂停配置的唯一信号。

```python
from services.platform.media.rules import RuleEvaluator

async with RuleEvaluator() as evaluator:          # 调用方只装配一个实例
    policy = await evaluator.validate_policy(document)   # 形状 + 真实正则编译
    decision = await evaluator.evaluate(
        metadata, document,
        accessible=True, quality_satisfied=False,
        snapshot_revision="scan-2026-09-19-000001",
    )
```

公开入口只有这些名字（`services/platform/media/rules/__init__.py`，无导入副作用）：

| 名字 | 用途 |
| --- | --- |
| `parse_policy(document)` | 纯形状校验，返回不可变 `RulePolicy`；不编译正则、不起进程。`whitelist`/`blacklist` 的**数组类型与两表合计 ≤20 组在读取任何组字段之前**判定，超大或非数组文档不会被遍历 |
| `policy_digest(whitelist, blacklist, revision)` | 对通过校验的组元组与修订号求确定性 SHA256（接收三个已校验参数，不是整份文档） |
| `regex_rules(policy)` | 按黑名单后白名单的顺序产出 `(组位置, 规则序号, 规则)` |
| `RuleEvaluator` | 异步上下文管理器：`validate_policy`、`evaluate`、`aclose` |
| 常量与词汇 | `FIELDS`、`OPS`、`DECISIONS`、`NORMAL_REASONS`、`FAILURE_REASONS`、`RESULTS`、各 `ERROR_*` 码、预算常量 |

## 端口、设置与依赖

- **无端口、无设置、无迁移、无新依赖**。仅 Python 3.12 标准库；`regex_worker.py` 只导入 `__future__` / `json` / `os` / `re` / `sys`。
- 子进程两侧都**显式按 UTF-8** 收发协议行：父进程写 UTF-8 字节，子进程把 stdin/stdout 重配为 `utf-8`/`strict`。子进程以 `-I` 启动，`-I` 会忽略 `PYTHONUTF8`/`PYTHONIOENCODING`，所以 locale 不参与“这个模式是什么意思”的判断。
- `services/platform/media/rules/` 不导入服务器、存储、认证、任务或网络模块（`tests/backend/media_rules/test_purity.py` 用 AST 白名单固定这一点）。
- TS-090 的五个模块与 `services/platform/media/types.py` 只读复用，未改动。

## 规则文档形状

严格全键，未知键即拒绝；`document` 只读，输入不被修改。

| 键 | 约束 |
| --- | --- |
| `schema_version` | 必须 `1`（`True` 是 bool，不算 int） |
| `revision` | 整数 1..2147483647 |
| `whitelist` / `blacklist` | 数组，可空；两表**合计最多 20 组** |

| 层 | 形状 |
| --- | --- |
| group | 严格 `{id, rules}`，`rules` 1..10 项 |
| rule | 严格 `{id, field, op, value, case_sensitive}` |

- `id`（组与规则）都是 `[A-Za-z0-9][A-Za-z0-9_-]{0,63}`；**组 ID 与规则 ID 各自全策略唯一**（跨两张表检查，报错指向后出现的那一处）。
- `field` 仅 `title` / `description` / `uploader_id` / `uploader_name` / `tags`。
- `op` 仅 `equals` / `contains` / `prefix` / `suffix` / `regex`。
- `case_sensitive` 必须是 bool。
- `value` 非空字符串；普通规则 ≤4096 字符，`regex` ≤512 字符。不隐式 strip、不做模板插值、拒绝 NUL 与孤立代理字符。
- 失败一律 `RuleValidationError(code, field)`，`field` 是指向出错处的路径（如 `whitelist[1].rules[0].value`），**不回显原值、表达式或路径**。

## 判定语义（冻结）

```
accessible AND NOT quality_satisfied AND NOT blacklist_match AND (whitelist_empty OR whitelist_match)
```

优先级顺序固定，被短路的规则在解释中记为 `not_evaluated`，绝不捏造 `not_matched`：

1. `accessible = False` → `skip / inaccessible`
2. `quality_satisfied = True` → `skip / quality_satisfied`
3. 黑名单任一组命中 → `skip / blacklist_match`（不再求值白名单）
4. 白名单为空 → `download / eligible`；否则任一组命中 → `download / eligible`，都不命中 → `skip / whitelist_no_match`

- **组间 OR，组内 AND**；组内规则按输入顺序短路，解释因此稳定。
- **多值字段**（`tags`、`uploader_*`）：一条规则只要任意一个值匹配即为匹配。
- **不做隐藏关联推断**：同一组内两条规则不要求匹配同一个作者或标签；需要联合条件时写成一条针对单字段的规则。
- `accessible` / `quality_satisfied` 是**调用方受信事实**，本模块不查询、不推断。
- `snapshot_revision` 为 `[A-Za-z0-9_-]{1,128}` 的不透明字符串，原样回传，不猜版本顺序。

### 字段投影

| 字段 | 读什么 |
| --- | --- |
| `title` | `original_title` —— **人工展示覆盖不改变订阅判断** |
| `description` | `original_description` |
| `uploader_id` / `uploader_name` | 仅 `role=uploader` 的作者；缺名的作者不参与名称匹配 |
| `tags` | 已归一标签 |

全空白来源字符串视为**缺失**：投影为零个值，任何操作都不匹配（包括 `regex=.*`）。不截断来源文本去改变规则真假——单个字段所有值合计超过 64 KiB（65536）UTF-8 **字节**时报 `input_too_large`，而不是悄悄截断。

预算按**字节**、上游限制按**字符**，两者不是一回事：TS-090 允许 `description` 到 65536 字符，而 65536 个中文汉字是 196608 字节。因此这不是“合法数据碰不到的防御性护栏”——21846 个 `集` 就是 65538 字节，一份**通过 TS-090 校验**的记录即可触发 `input_too_large`（`tests/backend/media_rules/test_bounds.py::ProjectionBudgetTest` 用真实归一化记录取证，不注入 mock）；65536 个 ASCII 字符恰好等于预算并仍然被完整求值。<br>反过来，`tags`（≤100 个、单个 ≤128 字符）即使全用三个字节的汉字也只有 37800 字节，所以多值合计规则的取证落在 `projection_within_budget` 上。

### 匹配细节

- 普通文本在 `case_sensitive=false` 时对**两侧**做 Unicode `casefold()` 后比较（`Straße`/`STRASSE` 等价，不引入 locale 语义）；`true` 时原样比较。
- `regex` 用标准库 `re.search`；`case_sensitive=false` 时加 `re.IGNORECASE`，**不改写表达式**（不改成 casefold 后的文本匹配）。

## 输出与失败

`RuleDecision`（frozen dataclass）必含：`item_key`、`snapshot_revision`、`policy_revision`、`policy_digest`、`decision`、`reason`、`automatic_enqueue_allowed`、`requires_rule_attention`、以及按**黑名单后白名单**排列的不可变 trace。

- `decision` ∈ `download` / `skip` / `rule_error`。
- 正常 `reason` ∈ `eligible` / `inaccessible` / `quality_satisfied` / `blacklist_match` / `whitelist_no_match`。
- 运行失败 `reason` ∈ `regex_timeout` / `regex_worker_failed` / `evaluation_timeout` / `busy` / `input_too_large`；此时 `decision=rule_error`、`automatic_enqueue_allowed=false`、`requires_rule_attention=true`。**绝不按“黑名单没命中”放行**。
- trace 每项只含组/规则 ID、字段名、操作名、`matched` / `not_matched` / `not_evaluated` / `error` 与固定原因；**不复制标题、简介、模式或完整值**。（`GroupTrace.list_kind` 标记该组来自 `whitelist` 还是 `blacklist` —— 这是为了解释自足而额外记录的**结构**字段，不是判定输入。）

`policy_digest(whitelist, blacklist, revision)` 对**已校验的三个参数**——两张表的组元组与修订号，按黑名单后白名单排列——按 UTF-8、`sort_keys=True`、`ensure_ascii=False`、紧凑分隔符、无尾换行求 SHA256；数组按输入顺序。它是内部规则标识，不是跨语言发布合同。（`RuleDecision.policy_digest` 是它在 `evaluate`/`validate_policy` 中算出的结果。）

`validate_policy` 与 `evaluate` 都会先完成**整个策略**的结构检查与正则语法编译，失败的规则不会潜伏到“反正这次没命中”的位置：

- 非法正则 → `RuleValidationError(invalid_regex, <list>[i].rules[j].value)`（`validate_policy` 与 `evaluate` 一致）。
- 进程/预算问题 → `RuleEvaluationError(固定 code/field)`；`evaluate` 把它变成 `rule_error` 决定，`validate_policy` 抛出。

## 正则执行与预算

仅用 Python 标准库独立子进程，主进程**不执行**用户 `re.compile` / `re.search`。

| 项 | 值 |
| --- | --- |
| worker 启动 | `sys.executable -I -u <包内受信脚本绝对路径>`，无 shell、无用户参数 |
| 协议 | 单行 JSON，**两侧显式 UTF-8**：`seq` 必须是正整数（bool、float、字符串一律拒绝）且与回包一致；`handle` 必须是 `int`；每类回包的字段集合精确匹配，未知/重复字段、非 UTF-8 行、超限行都算协议违规 |
| 深度嵌套行 | **小于 16 KiB 也拒绝**：5000 层嵌套数组只有 10000 字节，却会让 `json.loads` 触发 `RecursionError`。父进程在**握手行与回包行两个入口**都把 `RecursionError` 当作“读不懂的行”处理（与非法 JSON 同类），退役 worker 并给出固定错误码，不让解析器异常外逃；子进程侧同样拒收深层请求并回 `invalid_request`。`limit=` 只挡行长，挡不住这个 |
| 握手行 | 恰好 `{"ready": true, "protocol": 1, "pid": <本子进程 pid>}`：字段集合精确匹配，`ready` 必须是 `true`，`protocol` 必须是**整数** `1`（`true`、`1.0`、`"1"` 都拒绝），`pid` 必须是本 worker 所创建子进程的 pid（别的进程写来的 `ready` 不算握手） |
| 重复键 | **两端都拒绝**：父进程用 `object_pairs_hook` 拒收子进程回包里重复的键，子进程同样拒收请求里重复的键（回 `invalid_request`）。重复键是协议违规，不是“最后一个值生效” |
| 启动握手预算 | 3.0 s，**包含进程创建本身**（不只是读 `ready` 行） |
| 单条 compile/search 硬预算 | **`min(50 ms, 本次请求剩余时间)`**：50 ms 是上限，不是固定值 |
| 单次 validate/evaluate 全请求上限 | 5.0 s，自**准入/校验**起算，覆盖进程创建、握手、全部 compile 与全部多值/多规则 search（清理至多另 1.0 s，用 monotonic deadline） |
| 回收上限 | 1.0 s，**公共关闭的等待不超过它**。一个 worker 一生只有**一个清理任务**：第一个发起退役的调用方建它，之后所有调用方（第二次关闭、被取消的请求、创建失败后的交接、evaluator 扫尾）都等**同一个任务**，且**没有任何路径会替换它或重算 deadline**——因此 N 个并发退役只花一份预算，不存在“先等创建预算、再新开回收预算”，也不会把已经公布的绝对 deadline 往后推 |
| 单字段投影合计 | 64 KiB（65536）UTF-8 字节 |
| IPC 请求行 / 响应行 | 1 MiB / 16 KiB（流 `limit=`，在无界缓存前落实） |
| 同时进行中的请求 | **2**；第 3 个立即 `busy`，无隐式等待队列（两个请求由各自真实子进程占住，见 `test_bounds.ConcurrencyTest`）。名额覆盖**“在飞请求 + 已结束但仍未收尾的资源”**：一个请求结束了、它的子进程还活着，名额**照样算它一份**，不会因为请求对象结束就提前归还 |

- 哪种期限先到决定原因码：单条调用用尽自己的预算 → `regex_timeout`；请求级的 5 s 用尽（含“下一条调用已没有时间可发”）→ `evaluation_timeout`。**5 s 到期绝不会被伪报成正常 `skip`/`download`。** 判定发生在 `_call` 内：`budget = min(50 ms, 剩余请求时间)`，`TimeoutError` 时 `budget < 50 ms` 即请求期限先到，否则是调用期限。
- 普通文本策略**不启子进程**；含正则时每次请求最多启 1 个专属子进程，本次请求内复用，结束必回收；不同请求不共用正文或 compiled cache。
- 请求预算按**真实工作**取证，不靠缩短常量：`test_bounds.RequestBudgetTest` 的累计用例跑满**策略上限**（20 组 × 10 条 = 200 条规则，其中每组 9 条命中、1 条不命中，所以 200 条全部真的求值，白名单最终不命中），标签取 TS-090 允许的最大投影（100 个值），每条搜索约 6 ms。这样总工作量约 13 s、远超 5 s 请求预算，而**单条调用**离 50 ms 调用预算很远——把成本摊到多次短搜索上，是为了不让平台的 15.6 ms 定时粒度把一次 30 ms 睡眠变成接近 50 ms、从而让这个用例实际测到调用预算。
- 反方向的用例同样保留：一次请求内的 24 次快速成功查找必须全部跑完并给出正常决定（`test_the_full_five_second_budget_finishes_many_quick_search_values`）。两者一起固定“5 s 是上限而不是拒绝线”。
- 超限请求或协议违规**退役该 worker**（不复用为正常），并证明其真实退出；下一个请求用**新子进程**恢复。
- 两条升级路径，区别是“孩子还有没有机会自己退”：
  - **硬失败**（预算用尽、取消、协议违规、超限）先 `terminate/kill` 再等待，**第一个 await 之前就发信号**——正在回溯匹配的子进程不会因为管道关闭而停下，清理预算要花在等它真的退出上；
  - **主动关闭**（`aclose` / `close_quietly`）先关 stdin，给子进程一段**有上限的宽限**（≤0.2 s，且算在同一个 1.0 s 清理预算内），仍不退再 kill。这样正常退出会被记成真实 `0`，而不是每次关闭都以 `-9` 收场。
  - 两条路径**不是两个清理**：硬失败若撞上已经开始的温和关闭，就在**故障发生的那一点**把信号补上（同一个清理任务读的是实时的“已升级”标志），**不会**等宽限期满才动手。用例把取消放在宽限开始后 50 ms，并断言“取消到孩子真的死掉”的耗时落在故障点而不是宽限末端（`test_a_hard_failure_escalates_a_graceful_close_at_the_failure_point`）。
- **关闭只有两种结局：成功，或具名失败。** `aclose()` 正常返回当且仅当本次关闭**真的把该收的资源都收干净了**（创建不可能再产出一个活孩子、已产出的孩子有真实退出证据）；等到预算用完还没能确认，就抛既有的 `RuleEvaluationError(regex_worker_failed, "regex_worker")`——**不新增错误码、不新增返回形状、不回显表达式或正文**，也**不会无限期地等下去**（公共等待 ≤1 s）。`close_quietly()` 对异常保持安静，但**对所有权不安静**：它返回布尔值，`False` 表示这次没有收干净，调用方不能把它当成“已归还”。**公共的有界返回与真实的回收是两件事**：`settled` 读的是传输层的实时 `returncode`，孩子真退出后名额立刻回来，而“关闭在 1 s 内返回”本身**不等于**“孩子已经不在了”。
- **创建与关闭相撞时，窗口是“被建模”的，不是被声明消失的。** `create_subprocess_exec` 已经产生真实子进程、但还没把它交回 worker 的那一小段里，worker 看不到这个孩子。因此：关闭会为**交接**等待整个共享的 1.0 s 预算（不是只等 0.2 s 宽限），等待期间 `alive` 保持 `true`、`settled` 保持 `false`、`returncode` 保持 `None`（`closed` 只表示“不再接新活”，**不用来宣称“没有活着的子进程”**）；预算内交接到达时立刻 kill 并真实回收（`test_a_handover_released_inside_the_budget_keeps_the_one_shared_cleanup`：交接在 100 ms 后到达时，清理任务对象与它公布的绝对 deadline 都不变，关闭照常成功）；预算用完仍未交接时 `aclose()` **具名失败**，未收尾的状态**留在自己的所有权里**（`alive` 仍为真、不伪造退出码）。迟到的交接到达后由**创建任务自己的失败收尾**接住：它加入那个已经存在的清理任务，在**创建自己的预算内** kill 并真实回收这个迟到孩子，然后宣告 settled——不新开清理、不重算 deadline、**不重开第二个公共等待**、不补发新请求或新进程，也**不会把已经记录下来的失败追溯改写成成功**。evaluator 只丢弃 `settled` 的 worker，未 settled 的对象留在托管集合里由下一次关闭再扫，不会因为“先 pop 再 close”而丢失。
- **明确不承诺的部分**：如果句柄停在 worker 之外（例如 `create_subprocess_exec` 还没交回、进程对象只有创建任务持有），worker **既不能 kill 它也不能 `wait` 它**。这种情况下产品承诺的是**有界且诚实的失败**（公共关闭在 1 s 内具名报错、`alive`/`settled`/`returncode` 如实、所有权不丢），而**不是**“一秒内一定把它杀干净”——任何声称后者成立的说法都是没有证据的承诺。交接一旦到达，上面的迟到收尾就会补上真实的 kill 与回收。
- 受管子进程保留到确认退出并记录真实 `returncode`；等不到退出的子进程保持 `returncode is None`，**不伪造**退出码。`alive` 也不是“清空引用”的假象：它只看“孩子是否可能还在跑”（持有的子进程未退出，或交接仍未完成），不看 `closed`。
- 外部 `asyncio` 取消传播 `CancelledError`：在**握手期、请求期、创建期、交接期、清理期**任一处取消，都终止并回收本次子进程，不返回成功、不吞异常，且**取消优先于清理结果**（清理没做完也照样抛出取消）。清理期取消是最容易被写错的一处：清理会先把子进程 kill 并**回收完**，再把 `CancelledError` 重新抛出——只做其中一半都是缺陷（停止等待会漏掉一个还在跑的进程，吞掉取消会让调用方以为请求正常结束）。判断“这是调用方的取消”还是“传输层自己取消了一个内部等待”用 `Task.cancelling()`，不靠异常本身猜。创建期取消由创建任务自己的完成回调兜底（`test_regex_process.CreationBarrierTest` 把创建卡在 `create_subprocess_exec` 上取证；`test_a_close_inside_the_handover_window_is_not_reported_as_settled` 专门卡在“孩子已存在、尚未交回”的那一刻）。
- 重复关闭、并发关闭、取消清理都幂等：第二次关闭不再等待、不再 kill，也不丢句柄；并发关闭共用同一个清理任务与同一个 deadline，后到的等待者**读到的是同一个已完成的结果**（包括失败）。清理任务**自己抛异常**时不会被当成成功：`aclose()` 报 `regex_worker_failed`，worker 不宣称 settled（`test_a_cleanup_that_raises_is_not_a_successful_close`）。
- 请求结束但资源未收尾时，`evaluate` **不会**给出 download/skip：它按固定形状返回 `rule_error` + `regex_worker_failed`（`automatic_enqueue_allowed=false`、`requires_rule_attention=true`），`validate` 则具名报错——判定结果有了、孩子的退出证据还没有，就不是可以交付的结论（`test_a_verdict_is_not_delivered_for_an_unsettled_request`）。名额同理：请求结束不等于名额归还（`test_bounds.ConcurrencyTest.test_a_request_that_ended_with_a_live_child_still_spends_its_slot`）。
- `aclose()` 后拒绝新调用（`RuleEvaluationError(closed, "evaluator")`，是配置错误而不是“无法判定”的条目结果），只清理本实例的进程，不碰别人的进程；worker 在**第一个 await 之前**就登记到实例的托管集合，所以关闭是屏障而不是竞态。集合里只有 `settled` 的 worker 会被丢弃：创建始终没交回子进程的 worker 留在集合里由下一次关闭再扫，`aclose()` 不会为了“集合清空”而丢掉一个还有孩子要交代的对象。

## 验证入口

```bash
# 最窄（本包自己的套件）：143 个用例
python -X utf8 -m unittest discover -s tests/backend/media_rules -v
# 常规发现（确认新用例进入集合）
python -X utf8 -m unittest discover -s tests/backend -v
```

格式/静态检查只覆盖新增包与测试（Ruff 0.15.7，`line-length = 100`、`target-version = py312`）：

```bash
python -m ruff check services/platform/media/rules tests/backend/media_rules
python -m ruff format --check services/platform/media/rules tests/backend/media_rules
```

### 真实子进程与替身证据的边界

真实子进程用例需要能创建管道子进程的环境（见 `docs/handoffs/TS-098.md` 的环境边界）：当前受限沙箱拒绝 `asyncio` 的 Windows 子进程传输（`PermissionError: [WinError 5]`）。这些用例**保留原断言、不删覆盖、不改期望**，由用例自己的环境探针识别该拒绝并具名 skip，计入交付日志的 `skipped` 数（本轮 `Ran 143 tests … OK (skipped=48)`，48 条 skip **全部**是这一个原因；协调环境复跑时它们是实跑用例，期望 143/143 实跑通过）。

**不需要**子进程的用例在任何环境都实跑，本轮共 95 条：

- 形状、摘要、投影、匹配、判定与解释、导入边界；
- **容量**：`busy` 与“取消后容量真的回来”在关闭之前取证（关闭会让一切返回 `closed`，在关闭之后取证等于什么都没证明）；
- **协议解析**：握手行与回包行交给**真实解析入口**（`start` 与严格读取器）判定，用一个记录式传输替身，不需要真管道，因此不会因环境而 skip——包括**小于 16 KiB 的 5000 层嵌套行**在两个入口都被拒为具名错误并真实回收；
- **生命周期逻辑**：`kill` 先于 `wait`、真实退出码、重复关闭、无法回收时不伪造退出码、取消在创建/交接/握手/请求/清理五处的回收、创建与关闭相撞后不留活子进程、**交接窗口不谎报为已 settled 且并发关闭只花一份预算**、**未收尾的资源照样占名额**（请求结束但孩子还活着时第 3 个请求立即 `busy`，释放并真退出后名额只回来一次）、**硬失败在故障点升级已经开始的温和关闭**（不等满宽限）、**共享清理不被替换且 deadline 不被推后**、**清理自身抛异常不算成功**、**孩子未收尾时不交付 download/skip 判定**——这些用记录式替身取证，替身会记录每一次 `write`/`kill`/`wait` 的顺序，并且像真实传输一样：kill 会让正在进行的 `wait` 结束。

替身证明的是**顺序与状态机**，不能替代真实子进程：真实 `returncode`、真实管道拒绝、真实回溯匹配是否被杀掉，只能在协调环境实跑时确认。本轮协调环境已就这些行为单独复跑探针（见 `docs/handoffs/TS-098.md` 的验收记录）。