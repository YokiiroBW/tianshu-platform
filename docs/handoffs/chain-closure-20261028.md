# 同账号多会话 processing 回调修复

基线 `5b7ea997b3c0e2e7bd3f015c4856ed29b67eb6fa`；分支 `codex/chain-closure-20261028`；产品与测试候选提交 `177e2eb25a104a9d58bfb71f53040baa481ddbf6`。本交接为后续文档提交，产品源保持该候选。

## 触发与修复

隔离原生联动中，同一 OneBot 实例/账号登记两个不同私聊，真实 `probe → create → enable` 都为 ready。Companion 调用 `POST /internal/v2/observation-source/processing` 时，两项都返回 `processing_enabled=false / frame_unavailable`，后台 Memory 材料回执因此不能完成。缺少 `observation.verify` 的早期测试配置 403 属另一项 fixture 问题，未算作本产品故障。

`observation_processing.current` 的普通绑定回退原来按整个实例/账号候选数判断唯一性。现在在原 account 候选集合内，按已校验的原生 `conversation_id` 精确匹配 `{kind, id}`，并排除 observation 目录项；唯一匹配才进入原策略检查。陌生会话仍参考完整 managed 集合而拒绝，同一会话出现重复登记仍拒绝。角色、enabled、pending、ready/degraded、policy version 检查与 observation 原路径保持。

产品仅修改 `services/platform/observation_processing.py`，文件 SHA256 为 `974b8529d34c6c2c57469b35b988724b504d131374a45796c40a6c15be509703`。

## 回归与实际验证

新增回归复用 `test_bot_adapters.py` 的原录制 SDK/Core peer、原 Platform 账本和真实 loopback HTTP。两个私聊以及同 ID 的群聊/私聊均从实际 probe/create/enable 登记；覆盖两项同时允许、角色不符、单独禁用、陌生会话、重复会话、SDK 未应用造成的 pending 拒绝。没有直接灌 catalog/SQL 业务结果。fixture 单独声明 Companion 的 `observation.verify`。

1. 修复前先运行新增两私聊节点，精确基线产品返回 `[False, False]`，预期 `[True, True]`，**1 failed / 0.795s**，确认与原生复现相同。
2. 首轮相关 unittest：**53 ran / 11 errors / 1 skipped / 15.462s**。11 个 error 来自既有 `test_observation_segments.core_response` 未接收 `sources.dispatch` 的 `session_factory`。从 Git 精确导出未改的 `5b7ea997`，单例再次复现相同 TypeError（0.705s）；记录在忽略目录 `.runtime/chain-closure-baseline-5b7ea997/baseline-reproduction.log`。
3. 经协调者追加授权，仅为该原 helper 加显式 `*, session_factory=None`，原响应与断言不变。此前受阻的 observation segments **11 passed / 5 subtests passed / 13.28s**，记录 `.runtime/chain-closure-segments.xml`。
4. 新增三个 HTTP 节点与既有 function 式 role runtime 节点：**15 passed / 4.75s**（其中角色 12 个），记录 `.runtime/chain-closure-targeted.xml`。首轮其余通过结果直接复用；本条与首轮包含重合节点，不累加为测试总数。
5. 三个修改 Python 文件的 `ruff format --check`、`ruff check`，以及 `git diff --check` 通过。源码 diff 已集中检查。

首轮唯一 skip 是需 `TS_ADAPTER_JOINT_CORE=1` 的实际 Core TLS joint，未把它记为通过。工具沿用 TS-380 的共享 Python、verification-r3 合同及 pytest vendor，ruff 为既有 TS-311 q21 环境；没有安装新依赖。原生六 owner 的完整后续联动、安装 wheel 后的 Linux/PG 验证与正式发布由协调者接续。本任务未执行 SSH、真实 QQ/模型、生产读写、配置变更或部署。
