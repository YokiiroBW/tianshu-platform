# CONNECT-B NAS 日志超时只读审查（2026-09-28）

## 任务与证据边界

总控报告：NAS 上 Platform 运行约 12 分钟后，Docker stderr 出现 `tianshu-diagnostics: log output unavailable (log_flush_timeout)`，网页返回 503 `dependency_unavailable`，Gateway 配置续期也受影响；最后一条日志为 02:24:49.864 的 `http.request.started`。现场日志目录约 4.7 MB／1 GiB，原配置未设置 `diagnostics.durability_timeout_ms`。这些是总控提供的现场证据；CONNECT-B 未连接 NAS、读取现场日志或操作服务。

审查基线为本 B worktree `7f5d1c724bff22c91116bb3d7fd079a6b84c988a` 与已集成的 Platform `main` `60d17f28c3cef96274c054b05532fa2d21234a1c`。两者的 `services/platform/diagnostics.py` 和 `diagnostics_config.py` 无差异。本次仅审查与测试，没有修改产品代码。

## 结论

`diagnostics_config.py` 对缺省 `durability_timeout_ms` 采用 250ms，允许显式配置 250–5000ms；`__main__.py::build_sink` 把同一预算用于入站准入和终态日志确认。`diagnostics.py` 在已接收事件超出确认预算时记 `log_flush_timeout`、转 `unavailable` 并拒绝新业务；`flush` 对此状态返回失败。`_park` 等待显式恢复或停服，`recover()` 仅在真实 fsync 成功后恢复 `durable`，业务服务入口没有调用它。因此超时后持续 503、受控重启后才恢复，符合当前有意的失效关闭语义，不是“目录未满就应自动变绿”的代码缺陷。

4.7 MB／1 GiB 排除本次目录容量上限被占满；单凭 `log_flush_timeout` 不能分辨是单次 fsync、队列等待还是线程调度超过 250ms。当前证据支持“NAS 延迟与默认预算不适配”作为最可能解释，**不足以认定底层磁盘故障或代码缺陷**。不引入自动重试、不吞日志、不绕过准入。本轮没有修复提交。

总控随后报告已在受控停服后把 Platform 预算设为 5000ms，重新 bootstrap/recreate Gateway，十服务新代次 ready，浏览器首页刷新正常；6 分钟／121 次 HTML 200 持续检查当时仍在进行。CONNECT-B 未独立核验这些现场结果。若 5000ms 下复发，应先采集单次 fsync、排队及 owner 线程耗时，再判断是否需要代码或存储修复。

## 实际验证

在 B worktree 使用已有 `.runtime/venv/Scripts/python.exe` 执行以下 PowerShell 命令。首次命令把 `PYTHONPATH` 只设为仓库根，诊断测试导入 `runtime_fixture` 失败；它列出的 `SinkTests` 也不是实际类名，因此该次**没有**验证恢复用例。预算模块的两项已单独运行并通过。随后将 `tests/backend` 加入 `PYTHONPATH`，改为真实的 `DurabilityTests`、`OwnerTests`、`SlowDiskTests` 类名执行。

```powershell
$env:PYTHONPATH=(Get-Location).Path
.runtime/venv/Scripts/python.exe -m unittest tests.backend.test_nas_log_budget tests.backend.test_runtime_diagnostics.SinkTests.test_recovery_stays_refused_until_a_real_write_succeeds -v
```

结果：`test_nas_log_budget` **2/2 通过**；`test_runtime_diagnostics` 导入报 `ModuleNotFoundError: No module named 'runtime_fixture'`，整次命令退出失败。改正后的恢复专项：

```powershell
$env:PYTHONPATH=((Get-Location).Path + [IO.Path]::PathSeparator + (Join-Path (Get-Location).Path 'tests/backend'))
.runtime/venv/Scripts/python.exe -m unittest tests.backend.test_runtime_diagnostics.DurabilityTests.test_a_repaired_sink_recovers_and_the_gap_stays_reconcilable tests.backend.test_runtime_diagnostics.DurabilityTests.test_a_sink_that_is_still_broken_does_not_claim_recovery tests.backend.test_runtime_diagnostics.OwnerTests.test_recovery_stays_refused_until_a_real_write_succeeds -v
```

结果：**3/3 通过**。实际验证了坏 sink 不假称恢复，恢复必须经过真实写入，保留原事件与可核对故障记录。超时专项：

```powershell
$env:PYTHONPATH=((Get-Location).Path + [IO.Path]::PathSeparator + (Join-Path (Get-Location).Path 'tests/backend'))
.runtime/venv/Scripts/python.exe -m unittest tests.backend.test_runtime_diagnostics.SlowDiskTests.test_an_admission_that_cannot_be_confirmed_refuses_the_work tests.backend.test_runtime_diagnostics.SlowDiskTests.test_a_terminal_event_is_confirmed_before_the_answer_and_never_claims_it_early -v
```

结果：**2/2 通过**。两项均实际观察到 `log_flush_timeout`，准入或终态不能在超预算时假称持久。总计有效验证 **2 + 3 + 2 = 7 项通过**；未运行全量套件，也未在 NAS 重放故障。

## 交接

总控继续现场持续检查与故障证据保全；若新 5000ms 预算仍失败，再依据实测 fsync/队列延迟定位。B worktree 不需 cherry-pick 产品补丁；本文件供归档，不代表生产业务链验收。
