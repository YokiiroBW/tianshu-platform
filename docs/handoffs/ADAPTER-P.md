# ADAPTER-P · Platform 后台交接

基线：`88fb778974f7050f3b4aa3c84a47b847d997e349`。本任务在独立 Platform 工作树实施；Core 配套见 Companion 的同名交接。提交为当前任务分支 HEAD（固定 SHA 在总交接中记录）。

## 交付

- `/api/web/bot-adapters/{view,probe,create,enable,disable,reconcile}` 复用原 Cookie/CSRF/Origin 与 `/api/web/bots/unlock`。`reconcile` 只接受 `{id,expected_revision,client_id}`，从持久 pending 读取原 desired/revision/request_id；网页不能指定这些权限与协议字段。
- 私有目录 `bot-adapters.sqlite` 与 `bot-adapters.key` 为同一备份单元。整行 AES-GCM 封存，含插件密钥、CA、地址、内部 Bot token；view 不回显凭据。探测草稿另以该 key 短时加密并绑定登录会话随机 nonce。
- 插件客户端只调用固定 `/tianshu/adapter/v1` 路径，真实 DNS/IP 校验与固定地址解析器、HTTPS 证书验证、私网 HTTP 显式开关、无重定向、5 秒和 64KiB 上限。仅实际 `/capabilities` 的 QQ 账号可创建。
- QQ 账号、会话裸数字 ID 与作者裸数字 ID 在探测/创建阶段验证；Core 与插件 binding 保留 `{kind,id}` 原值，Platform Sources channel 以 `kind:id` 生成 `channel_conversation_id`，与真实宿主事件/回复目标一致。旧候选若曾把 `group:...`/`private:...` 存作 id，需在隔离环境清理候选数据后重新创建，不可直接启用。
- 动态 Sources/Origin entries 从部署批准 actor、显式会话/作者和平台既有 service principal 构造；隔离 `adapter:bot:*` / `binding:bot:*` 命名空间，不接受浏览器 principal/entry/slot。沿用 Bots 原入站去重、唯一 owner、send/claim/ACK。旧 Bots 管理 API 不展示或修改动态连接。
- 三方版本管理：创建 rev1 disabled；enable/disable 提升 revision，先在同一 SQLite 事务保存意图与 client_id/fingerprint 回执，停用随后立即封闭本地，再协调 Core/插件；任何写结果不明保留 unknown，本地封流。重启无条件封闭所有动态 Bots 行（含 unknown/disabled），后台每 2 秒及 view 只读查询双端 status；确认一致才恢复 ready。`reconcile` 是管理员显式恢复同一个幂等写，不创建新阶段或测试发送。SDK send 结果未知只查 status，未确认时 ACK unknown，不二次发送。创建、启停和显式恢复的回执与意图同事务提交，重放只读当前 row；停用意图一经持久化，Bots 入站/claim/回复准入拒绝该连接。Pump 不跨外部 RPC 持管理全局锁，每次新入站/SDK send 前核对最新意图；已发起的调用可结算，旧 pump 观测用修订 CAS 拒绝覆盖新停用。

## 部署 bootstrap 示例（由总控统一装配，不让网页用户编辑）

在既有 Platform settings 中新增/保留以下片段；凭据只登记环境变量，不写 JSON：

```json
{
  "bot_connections": {"principal": "connector", "slots": {}},
  "bot_adapter_self_service": {
    "directory": "C:/data/tianshu/private-bot-adapters",
    "allowed_cidrs": ["127.0.0.0/8", "192.168.0.0/16"],
    "actors": [{"id": "actor:a", "label": "已批准角色"}]
  },
  "core": {
    "base_url": "https://core.example.internal:8765",
    "token_env": "TS_PLATFORM_TO_CORE_TOKEN",
    "ca_file": "C:/data/tianshu/core-ca.pem"
  }
}
```

`connector` 必须是既有 `service=platform` 主体，含 `source.register/source.dispatch/mapping.prepare`；网页 operator 需 `bot.manage`，Core 要有同一 actor 的真实 role。`core.token_env` 与 Core `callers.platform.token_env` 指向同一个受管环境凭据。目录须持久且只供服务账号访问；丢失 key 或 SQLite 文件时拒绝恢复，不重置已启用连接。Core 的 `bot_binding_management_enabled:true` 与 platform_sender 见配套交接。

## 隔离验证入口

```powershell
$env:TS012_CONTRACT_DIR='C:/YOKI/Codex/tianshu-peiban-bot/contracts/text-dialogue/v1'
.runtime/venv/Scripts/python.exe -m unittest discover -s tests/backend -p test_bot_adapters.py -v
$env:TS_ADAPTER_JOINT_CORE='1'
.runtime/venv/Scripts/python.exe -m unittest discover -s tests/backend -p test_bot_adapters.py -v
```

第二条要求隔离测试 venv 同时安装 Companion 包与其锁定开发依赖（仅测试运行环境，无产品依赖变更），会在临时目录生成本地证书，启动真实 Core ASGI/TLS 与录制插件 HTTP 对端。测试覆盖管理员解锁、错误密钥、真实探测、创建/启停、未知写 status 恢复、丢绑定显式 reconcile、revision/client_id、重启封流、意图与回执事务回滚、创建/启用取消重放、停用持久后崩溃、慢 send 并发停用与跨连接管理、DNS 校验期间停用阻止新 send、入站去重与 SDK unknown 仅一次发送。这里的插件是录制 HTTP 对端，并非 H 的真实宿主插件或 QQ SDK；真实 H/NAS/机器人消息由总控后续联合验收。

实际受影响回归（同一隔离环境，`TS_ADAPTER_JOINT_CORE=1`、`PYTHONPATH` 指向本工作树 `tests/backend`）：`python -m unittest test_bots test_bot_adapters test_web_console test_web_dialogue test_http test_sources test_source_https -q`，65 项运行通过、5 项按既有外部条件跳过。完整 `unittest discover -s tests/backend -q` 实际运行 893 项，14 failure / 79 error / 185 skip，**不能记作通过**。代表性原因已在相同解释器、依赖、合同与运行上下文的原始 `88fb778` 解压检出中复现：媒体 `regex_worker_failed`，以及 `test_build_inputs` 比对发现 `runtime.lock` **含有** `cffi/cryptography/pycparser` 而 `requirements-dev.txt` 不含这三项。脱敏代表性退出码与栈见本工作树忽略目录 `.runtime/platform-current-failures.log`、`.runtime/platform-baseline-failures.log`（两次均 exit 1）；未逐项证明全量其余 91 个失败/错误的根因。未修改依赖锁或放宽断言。

部署依赖核验：Dockerfile builder 从 `scripts/build/runtime.lock` 用 `--require-hashes` 安装运行包，再安装产品 wheel 并执行 `pip check`；该运行锁已经含 `cryptography==50.0.1`、`cffi==2.0.0`、`pycparser==2.23`，`pyproject.toml` 也早已声明 cryptography。本次加密目录未引入镜像缺失的新依赖。没有在本机执行 Docker build，因此 Linux wheel 实际安装与镜像启动仍待总控环境验证。

## 风险与下一步

- Core/插件状态不能核实时持续 unknown 且本地封流；管理员用网页“核对并恢复”恢复原阶段，若仍无法确认则需修复对端或凭据。恢复原 enable 可能恢复正常流量，但 reconcile 自身不发测试消息。
- 原已在途的 SDK 调用可能在管理员点停用前已开始，晚到结果只归档；新 send 由本地 Bots.disable 先行阻止。
- 未在真实 AstrBot/NoneBot、真实 QQ、NAS 容器或生产凭据上执行；总控需用 H 固定插件候选与 U 固定页面做联合验收。
