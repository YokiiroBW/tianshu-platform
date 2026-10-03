# 生活页真实天气与位置

生活页的天气配置使用平台现有 `web_external` 加密目录。API Host、API Key 和各角色的位置写入 `external.sqlite` 的 `weather` 记录；凭据复用 AES-GCM 存储，网页只得到 `credential_configured`，不会回传 Key。备份仍是原目录中的数据库和密钥文件，没有新增 sidecar 或生产数据库迁移。旧版本平台不识别 `weather` 记录，配置后回滚必须同时恢复部署前的 external 目录备份。

网页的天气设置中填写和风控制台的专属 API Host 与 API Key，再搜索和选择城市。无需把密钥发到聊天中。Host 支持和风分配的多层专属域名（例如 `https://abcxyz.re.qweatherapi.com`），仍仅允许 HTTPS 与 `qweatherapi.com` 下有效的 DNS 子域名，不允许端口、用户信息、接口路径或查询参数，API Key 使用 `X-QW-Api-Key` 标头；不跟随重定向，不从环境代理转发凭据。API Key 模式已实现，JWT 签发/自动续期不在本轮范围。

所有天气接口复用网页登录、同源、CSRF 和生活服务的逐角色 `life.read` 核验。配置与位置变更额外复用已有 `external.manage` 管理员权限，没有新解锁步骤。外部目录尚未安装时返回 `weather_store_unavailable`，不会在请求中另建明文存储。

## HTTP 接口

均为 `POST /api/web/weather/` 下的 JSON 请求，均包含 `actor_id`。

| 操作        | 其他输入                                                                                                     | 返回                                             |
| ----------- | ------------------------------------------------------------------------------------------------------------ | ------------------------------------------------ |
| `state`     | 无                                                                                                           | 配置和选中位置；不含凭据                         |
| `configure` | `host`, `credential: {action: keep/replace/clear, value?: string}`, `expected_revision`, `client_id`（UUID） | 更新后的 state                                   |
| `locations` | `query`                                                                                                      | `{items: Location[]}`                            |
| `location`  | `location_id`, `expected_revision`, `client_id`                                                              | 再经 GeoAPI 核实的位置及 state                   |
| `current`   | 无                                                                                                           | state + `weather`, `fetched_at`, `stale`, `code` |

state 的 `server_time` 是服务端当前 Unix 秒数，前端可据此用单调时钟推进；`location.tz` 来自 GeoAPI 的实际 IANA 时区，并包含 `utc_offset`、经纬度以及省市国家。日期和时钟按该时区显示，不假设浏览器或 NAS 所在时区。位置只影响网页真实天气/时钟，不隐式更改已生成日程的业务时区。

天气使用官方当前推荐的 `/weather/v1/current/{latitude}/{longitude}`（坐标保留两位），不新增即将弃用的 v7 调用。输出温度为摄氏字符串，包含 `temp`, `feels_like`, `text`, `icon`, `wind_scale` 和 `attributions`。v1 未提供观测时间，因此 `observed_at: null`；`fetched_at` 只表示实际获取时间。界面需显示返回的归因信息。

成功数据缓存五分钟，同一城市并发读取复用一次获取；失败重试间隔一分钟。单次请求八秒超时、解压响应上限 128 KiB、缓存最多 128 项。失败可保留旧数据，但返回 `stale: true` 与真实失败码，绝不把旧数据说成刚刚更新。无配置/无位置时 `weather: null`。配置变化清除本实例旧缓存，其他实例通过 catalog 的 revision 避免复用旧配置。

## 验证与局限

`python -m unittest discover -s tests/backend -p test_web_weather.py -v` 使用隔离加密目录和合成和风响应，覆盖凭据不回传/不明文落盘、角色读授权、管理员写权限、位置时区、缓存/失败旧值、修订冲突、官方域名限制和请求标头/禁止重定向。没有用户的真实和风 API Key，未执行真实和风账号联验。没有把合成读数或本地验证当作 NAS 实机成功。

官方依据：[城市搜索](https://dev.qweather.com/docs/api/geoapi/city-lookup/)、[实时天气 v1](https://dev.qweather.com/docs/api/weather/weather-current/)、[构建 API 请求](https://dev.qweather.com/docs/configuration/api-config/)、[元数据归因](https://dev.qweather.com/docs/resource/metadata/)。
