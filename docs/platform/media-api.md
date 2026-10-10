# 视频订阅同源 API（2026-10-09）

所有操作为 `POST /api/web/media/<operation>`，JSON，请求沿用网页登录的 Cookie、Origin 和 X-CSRF-Token；无第二次密码解锁。错误返回现有 `{code}`，400 输入错误、401 会话失效、409 版本/幂等冲突、503 上游或运行依赖不可用。媒体正文总预算 6 MiB，流式计数后解析，包含规则最大 20 组×10 条×4096 字符及 JSON 转义余量；超限报413 `budget_exceeded`，不截断。账号 Cookie 仅在 import 请求出现，响应、台账、任务日志不返回 Cookie、签名媒体 URL、文件路径或私有引用。

## 通用值

`Quality = {mode: "best"|"exact", quality_id: string|null, allow_fallback: boolean}`，best 的 quality_id=null；exact 为 B 站质量编号（例如 "80"）。指定画质不可取得时默认失败，不静默降级。

`Rules = {schema_version:1,revision:number,whitelist:Group[],blacklist:Group[]}`。
`Group={id:string,rules:Rule[]}`；`Rule={id,field,op,value,case_sensitive:boolean}`。field 为 title/description/uploader_id/uploader_name/tags；op 为 equals/contains/prefix/suffix/regex。复用现有 TS-098 规则与硬超时。

`Source={kind:"favorite"|"collection"|"series"|"uploader",id:string}`。持久值 favorite/uploader 为数字 ID；collection/series 为 `MID:SID`。save 的 id 也接受对应 HTTPS B 站链接：空间 `/MID/favlist?fid=FID`、`/MID/video`、`/MID/lists/SID?type=season|series`，旧 `/MID/channel/collectiondetail|seriesdetail?sid=SID`；只解析已知主机和路径，不请求任意 URL。类型不符报 `source_kind_mismatch`，响应回归一值。

`Account={account_id,label,state:"ready"|"auth_required"|"revoked",revision,checked_at:number|null,code}`。

`Subscription={subscription_id,label,revision,source,account_id:string|null,target_id,initial_sync:"future_only"|"history",quality,rules,interval_seconds:number,state:"active"|"paused"|"auth_required"|"rule_error",baseline_ready:boolean,last_scan_at:number|null,next_scan_at:number,code,counts:{members:number,queued:number}}`。

`Job={job_id,subscription_id:string|null,media_key,bvid,cid,selected_cids:string[],layout:"single"|"multipart",revision:number,title,creator,state,stage,code,progress:number|null,created_at:number,updated_at:number,target_id,quality,actual_quality:string|null,cancel_requested:boolean,can_retry:boolean,can_cancel:boolean,asset_receipt:object|null,library_results:object[]}`。progress 范围 0..1；一个 Job 是所选 P 的完整投稿包，cid 为首个所选 CID，selected_cids 为全部所选 CID（增量会并入已有成品 CID）。订阅从仅1P即固定multipart剧集布局；手动单P可single，同目标已存在single后改订阅会报 `layout_change_requires_migration`。

Job.state/stage 来自持久台账：queued/downloading/validating/metadata_ready/publishing/asset_indexed/library_verifying/completed/published/auth_required/waiting_metadata/retry_wait/cancelled/failed/unknown。published 表示已发布且未配置媒体服务器；completed 只表示所配置服务器均核验通过。

## 端点

| operation | 请求 | 返回 |
|---|---|---|
| view | `{}` | `{configured,capabilities:{provider:"bilibili",source_kinds:string[]},engine:{state,version,code},accounts:Account[],targets:[{target_id,label,state}],subscriptions:Subscription[],jobs:Job[]}` |
| resolve | `{url,account_id:string|null}` | `{preview_id,expires_at:number,video:{bvid,title,description,creator:{mid,name},cover_url:string|null,parts:[{cid,index,title,duration_seconds:number|null,formats:[{quality_id,label}]}]}}` |
| enqueue | `{preview_id,part_cids:string[],account_id:string|null,target_id,quality,client_id}` | `{jobs:Job[],replayed:boolean}` |
| subscriptions/save | `{subscription_id:string|null,expected_revision:number,label,source,account_id:string|null,target_id,initial_sync,quality,rules,interval_seconds:number,client_id}` | `{subscription:Subscription,replayed:boolean}` |
| subscriptions/control | `{subscription_id,expected_revision:number,action:"pause"|"resume",client_id}` | `{subscription:Subscription,replayed:boolean}` |
| subscriptions/scan | `{subscription_id,recompute:boolean,client_id}` | `{subscription:Subscription,queued:number,replayed:boolean}`（完整扫描；失败不更新成员基线和水位） |
| rules/preview | `{preview_id,rules}` | `{results:[{cid,decision,reason,automatic_enqueue_allowed,requires_rule_attention,trace:object}]}` |
| jobs/detail | `{job_id}` | `{job:Job,events:[{state,code,at:number}],metadata:{title,description,original_title,original_description,title_overridden:boolean,description_overridden:boolean,issues:string[],cover_available:boolean}}` |
| jobs/list | `{states:string[]\|null,cursor:string\|null,page_size:number}` | `{jobs:Job[],page:{has_more:boolean,next_cursor:string\|null}}`；page_size 1..100，states 为持久状态枚举，null 全部，[] 空集；游标绑定排序后的筛选集合，跨筛选复用报409 `cursor_conflict`；数据库按 `(整数created_at,job_id)` 倒序keyset LIMIT，view仍返回最新100个，订阅列表完整返回无静默截断 |
| jobs/metadata | `{job_id,title:string,description:string,expected_revision:number,client_id}` | `{job:Job,replayed:boolean}`（展示覆盖，保留来源原文；waiting_metadata恢复暂存和发布。封面从已批准B站原图获取，不接任意图片URL） |
| jobs/refresh_metadata | `{job_id,expected_revision:number,client_id}` | `{job:Job,replayed:boolean}`；waiting_metadata/failed/published/completed 可重新解析固定来源，旧快照保留在私有历史台账，所选CID必须仍存在；允许恢复来源作者缺项，不伪造MID；已发布成品走绑定owned base的版本更新 |
| jobs/control | `{job_id,action:"cancel"|"retry",client_id}` | `{job:Job,replayed:boolean}` |
| accounts/import | `{label,cookie,client_id}` | `{account:Account,replayed:boolean}`（先检查当前本人会话） |
| accounts/check | `{account_id}` | `{account:Account}` |
| accounts/revoke | `{account_id,expected_revision:number,client_id}` | `{account:Account,replayed:boolean}` |
| accounts/qr/start | `{label}` | `{qr_id,url,expires_at:number}`（url 为真实 B 站二维码链接） |
| accounts/qr/poll | `{qr_id,client_id}` | `{state:"waiting"|"scanned"|"expired"|"ready",account:Account|null}` |

`client_id` 是 UUID，重放同键同操作返回原事实；不同请求报 `idempotency_conflict`。前端用请求取消/切页忽略迟到回包，后台作业仅 jobs/control 的 cancel 才取消。订阅 pause 不取消已入队作业。

view 不配置时返回 configured=false、空集合及 engine.state=not_configured。无需假数据。媒体账号过期时暂停受影响扫描/下载，显式重新导入或扫码恢复。

订阅间隔 60..604800 秒。future_only 先记完整成员及 CID 基线，后续扫描重核所有当前成员，发现既有 BV 新 P；旧视频新加入收藏按成员变化处理。删除后重加不重复下，跨订阅实际去重后计数。普通规则/名称保存不重算历史；明确 recompute 才按当前规则历史重评。显式质量策略变化会重评已有成品，不导入 future_only 旧基线中未下载的历史视频。

resolve 画质来自固定 yt-dlp 引擎 metadata-only 提取，实际下载同一引擎；自有 B 站接口负责账号、订阅成员和原始快照。下载前后复核当前 CID→P 映射，源 CID 删除或过程中映射变化拒绝成品。

`targets[].servers=[{server_id,label,kind:"emby"|"jellyfin"}]`（只登记名称，无服务器地址/令牌）。`Job.library_results=[{server_id,kind,state:"verified"|"unavailable"|"mismatch"|"pending",code,item_id:string|null}]`。
`rules/preview.results[].trace` 为 TS-098 的 `RuleDecision`：`{item_key,snapshot_revision,policy_revision,policy_digest,decision,reason,automatic_enqueue_allowed,requires_rule_attention,trace:[{group_id,list_kind:"blacklist"|"whitelist",result,reason,rules:[{rule_id,field,op,result,reason}]}]}`。snapshot_revision 是原始快照内容 SHA256。先黑名单后白名单，trace不带匹配原文。

metadata/refresh_metadata 的版本冲突统一报409 `version_conflict`。二维码 waiting/scanned/expired 不缓存为导入回执；同 client_id 持续轮询可观察阶段变化，ready 才持久导入幂等事实。每次轮询至少间隔2秒。
