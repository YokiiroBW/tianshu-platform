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
| 启动握手预算 | 3.0 s，**包含进程创建本身**（不只是读 `ready` 行） |
| 单条 compile/search 硬预算 | **`min(50 ms, 本次请求剩余时间)`**：50 ms 是上限，不是固定值 |
| 单次 validate/evaluate 全请求上限 | 5.0 s，自**准入/校验**起算，覆盖进程创建、握手、全部 compile 与全部多值/多规则 search（清理至多另 1.0 s，用 monotonic deadline） |
| 回收上限 | 1.0 s（礼貌等待与 kill 后等待**共用**这一个 deadline，不是两次各 1 s） |
| 单字段投影合计 | 64 KiB（65536）UTF-8 字节 |
| IPC 请求行 / 响应行 | 1 MiB / 16 KiB（流 `limit=`，在无界缓存前落实） |
| 同时进行中的请求 | **2**；第 3 个立即 `busy`，无隐式等待队列（两个请求由各自真实子进程占住，见 `test_bounds.ConcurrencyTest`） |

- 哪种期限先到决定原因码：单条调用用尽自己的预算 → `regex_timeout`；请求级的 5 s 用尽（含“下一条调用已没有时间可发”）→ `evaluation_timeout`。**5 s 到期绝不会被伪报成正常 `skip`/`download`。**
- 普通文本策略**不启子进程**；含正则时每次请求最多启 1 个专属子进程，本次请求内复用，结束必回收；不同请求不共用正文或 compiled cache。
- 超限请求或协议违规**退役该 worker**（不复用为正常），并证明其真实退出；下一个请求用**新子进程**恢复。
- 超时先 `terminate/kill` 再等待退出——不是只放弃 future 让子进程继续跑。受管子进程保留到确认退出并记录真实 `returncode`，`alive` 不是“清空引用”的假象。
- 外部 `asyncio` 取消传播 `CancelledError`：终止并回收本次子进程，不返回成功、不吞异常；取消发生在创建/清理交界时按真实退出码回收，不遗留进程，验收按真实 `returncode` 而非被清空的属性。
- 重复关闭、并发关闭、取消清理都幂等：第二次关闭不再等待、不再 kill，也不丢句柄。
- `aclose()` 后拒绝新调用，只清理本实例的进程，不碰别人的进程。

## 验证入口

```bash
# 最窄（本包自己的套件）：125 个用例
python -X utf8 -m unittest discover -s tests/backend/media_rules -v
# 常规发现（确认新用例进入集合）
python -X utf8 -m unittest discover -s tests/backend -v
```

格式/静态检查只覆盖新增包与测试（Ruff 0.15.7，`line-length = 100`、`target-version = py312`）：

```bash
python -m ruff check services/platform/media/rules tests/backend/media_rules
python -m ruff format --check services/platform/media/rules tests/backend/media_rules
```

真实子进程用例需要能创建管道子进程的环境（见 `docs/handoffs/TS-098.md` 的环境边界）：当前受限沙箱拒绝 `asyncio` 的 Windows 子进程传输（`PermissionError: [WinError 5]`）。这些用例**保留原断言、不删覆盖、不改期望**，由用例自己的环境探针识别该拒绝并具名 skip，计入交付日志的 `skipped` 数（本轮 `Ran 125 tests … OK (skipped=40)`）；协调环境复跑时它们是实跑用例，期望 125/125 实跑通过。不需要子进程的用例（形状、摘要、投影、匹配、判定与解释、容量恢复、导入边界）在任何环境都实跑。
