# 和风专属域名修复（与地点显示合并发布）

用户填写官方多层专属地址却收到 invalid_weather_host。根因是 host_url 只允许单层子域名。现允许 qweatherapi.com 下合法 DNS 标签组成的多层地址，规范化大小写；保留 HTTPS、无用户信息/端口/路径/参数/片段限制。复用唯一 host_url，保存及实际外部请求同时生效；没有新增凭据存储或依赖。页面示例改为 abcxyz.re.qweatherapi.com，并补可操作中文错误。

依据：https://dev.qweather.com/docs/configuration/api-host/（2026-10-03 阅读，官方示例 h2a9cf3mhs.xy.qweatherapi.com）。

天气后端 11 项通过，使用多层域名覆盖保存、重新读取、城市搜索、天气及缓存的隔离合成响应。外部连接 HTTP 5 项通过；首次因未设置 TS012_CONTRACT_DIR / TS013_TLS_PYTHON 未运行，按既有配置补齐后通过。前端天气配置桌面/手机 2 项通过；地点显示另有桌面/手机 2 项通过。TypeScript、Vite、ruff 检查与格式通过。未全量回归，未读取或调用用户真实 API Key。

本批包含先前 b10d1f8 地点显示修复；该中间候选只构建、未切换 NAS，最终镜像统一部署。无配置迁移，不改现有天气输入或角色日程。实际生产发布见协调记录 weather-host-deployment-2026-10-03。
