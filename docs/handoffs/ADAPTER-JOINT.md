# ADAPTER-JOINT 双宿主联合验收

## 目标与基线

在集成候选上验证真实 NoneBot、AstrBot 宿主经插件 RPC、Platform、Core 的闭环，以及重复事件、未知发送、停用和恢复。Platform 基线为 `68b9d3b3d059fd5e1194775577798c70d18d0c67`（包含 P 修复 `04f9c874c701cb8fa20fe31e18646013aee99f81`）；Companion 基线为 `e85987147b1109c8a41b3782e6fc3ccc33e0e249`（包含 H 修复 `6e2c475` 与交接 `27ce2f8`）。本任务只新增本文件和 `tests/backend/test_bot_adapters_host_joint.py`，没有改动产品实现或依赖。

## 实际运行边界

测试在 H 隔离 venv 中导入 NoneBot 2.5.0、OneBot 2.4.0、AstrBot 4.27.3。NoneBot 经正式插件装载和 matcher 接收事件；AstrBot 将集成候选插件源码复制到临时插件目录，经正式 `PluginManager` 装载。测试断言 Platform、Core、NoneBot 插件模块路径属于集成候选，避免旧 editable 安装。Platform 和 Core 分别运行实际 HTTP/ASGI 服务，双方经临时自签 CA 的 TLS 与服务令牌通信；插件 RPC 用真实 HTTP。数据、证书、密钥和宿主运行目录均在临时目录。QQ Bot SDK、Memory 和模型 Gateway 是明确的隔离夹具，未连接真实 QQ、模型或生产数据。

两种宿主各自验证：probe、创建、启用；合成 QQ 事件入队，`revision=1` 且 `conversation_id=private:7` 与 Platform Sources channel 一致；Platform pump 认可并确认事件；Core 经真实来源解析和合成模型生成 `Synthetic reply`；Platform claim，插件仅调用一次合成 SDK，Platform 账本 `sent`，Core 后续 reconciliation 的 reply `sent`。相同 SDK message ID 再入站时没有第二个 Core turn 或 SDK 调用。第二条消息的合成 SDK 抛出 `TimeoutError`，Platform 账本为 `unknown`，后续 pump 不重发。随后重建 Platform HTTP 服务和对象，持久化账本仍为 `sent/unknown`，连接先封闭为 `unknown`；对真实 Core 与插件状态 RPC 的 reconcile 使其恢复 ready。停用后 Core binding 移除，宿主新事件不入队。再模拟插件已实际应用启用、但 Platform 丢失 HTTP 响应，状态为 `unknown`；显式 reconcile 经双方状态查询恢复 ready，SDK 调用次数不增加。

## 验证

在 `tests/backend` 工作目录、设置 `TS012_CONTRACT_DIR=C:/YOKI/Codex/tianshu-peiban-bot/contracts/text-dialogue/v1` 和 `PYTHONPATH=C:/YOKI/Codex/tianshu-peiban-bot/worktrees/ADAPTER-INTEGRATION/tianshu-platform` 后，运行：

```powershell
& 'C:/YOKI/Codex/tianshu-peiban-bot/worktrees/ADAPTER-H/tianshu-companion/.runtime/adapter-venv/Scripts/python.exe' -m unittest test_bot_adapters_host_joint -v
```

结果：2 项通过，31.907 秒。`ruff check`、`ruff format --check` 均通过。首次联合运行实际发现两处线协议不一致：H 发 `revision=2` 而 Core 绑定为 1；P 的 channel conversation ID 曾为 `7`，宿主发 `private:7`。上述固定候选分别由 H `6e2c475` 和 P `04f9c87` 修复后重测通过。Core 默认约 5 秒聚合窗口，测试等待实际时钟，没有缩短产品窗口。

## 未覆盖与下一步

本测试重启的是 Platform 服务与持久化所有者，未在同一联合测试中重启 NoneBot/AstrBot 或 Core 进程；它们各自的独立恢复测试仍须作为对应产品证据引用。测试不证明真实 QQ 平台连接、真实模型质量、NAS/局域网部署、长期负载或生产迁移。协调者可将此文件与测试纳入集成候选并按串行合并流程审查；如需实机接入，应另行取得实际环境与凭据授权。
