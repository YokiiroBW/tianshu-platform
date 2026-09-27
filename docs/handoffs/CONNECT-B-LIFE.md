# CONNECT-B Companion 生活读取联合验收（2026-09-27）

`tests/backend/test_life_joint.py` 使用协调检出 Companion `31677983798ba27b24d57925feab4774c2eec30f` 的真实 `Store`/`Life` 合成夹具，在本产品临时目录写入小说化 actor、静态状态及已发布日记，另起真实 Companion uvicorn HTTPS 进程。Platform 真实同源 HTTP 登录后，通过固定 `web_life` TLS 凭据依次调用对端四个 `/internal/v1/life-read/*` 路由；没有替换上游响应。

验证 actors 只列获准的 `actor:a`，snapshot 返回 `last_persisted`，diaries 只给已发布指针，revision 读出对应正文。旧日记版本返回 409，未获授权的 `actor:b` 返回 404；本地撤销 `life.read` 后 403。真实 Companion SQLite 各表行在读取前后相同，证明本次联测没有触发 life 写入。用于准备夹具的生产者测试辅助模块从该检出只读导入，设置 `PYTHONDONTWRITEBYTECODE=1`；源检出未改动。

```powershell
$env:TS012_CONTRACT_DIR='C:/YOKI/Codex/tianshu-peiban-bot/contracts/text-dialogue/v1'
$env:TS013_TLS_PYTHON='C:/Users/Administrator/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe'
$env:TS_COMPANION_PATH='C:/YOKI/Codex/tianshu-peiban-bot/projects/tianshu-companion'
$env:PYTHONDONTWRITEBYTECODE='1'
& .runtime/venv/Scripts/python.exe -m unittest discover -s tests/backend -p test_life_joint.py -v
```

结果 1/1 通过。此结果证明固定源版本与本地隔离数据的合同兼容，不表示 NAS 已运行该服务、生产 reader 已授权、现场证书或真实日记读取已验收。
