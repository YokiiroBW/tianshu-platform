export const stateWords: Record<string, string> = {
  queued: "等待下载",
  downloading: "正在下载",
  validating: "校验成品",
  metadata_ready: "元数据已就绪",
  metadata: "检查元数据",
  publishing: "正在发布",
  asset_indexed: "资产已入库",
  library_verifying: "媒体服务器核对中",
  completed: "核对完成",
  published: "发布完成",
  auth_required: "需要重新登录 B 站",
  waiting_metadata: "等待补全元数据",
  retry_wait: "等待重试",
  cancelled: "已取消",
  failed: "失败",
  unknown: "结果未知",
  active: "订阅运行中",
  paused: "已暂停",
  rule_error: "规则需要修正",
  ready: "可用",
  revoked: "已撤销",
  not_configured: "未配置",
  available: "可用",
  unavailable: "暂不可用",
  waiting: "等待扫码",
  scanned: "已扫码，请在手机确认",
  expired: "二维码已过期",
};
export const sourceWords: Record<string, string> = {
  favorite: "收藏夹",
  collection: "合集",
  series: "系列",
  uploader: "UP 主投稿",
};
export const reasonWords: Record<string, string> = {
  eligible: "符合规则，可以下载",
  inaccessible: "当前账号无法访问",
  quality_satisfied: "已有成品满足目标质量",
  blacklist_match: "命中黑名单，跳过",
  whitelist_no_match: "未命中白名单，跳过",
  regex_timeout: "正则执行超时，请修正规则",
  regex_worker_failed: "正则试算暂不可用",
  evaluation_timeout: "试算超时",
  busy: "试算服务繁忙",
  input_too_large: "来源字段超出试算预算",
  matched: "命中",
  not_matched: "未命中",
  not_evaluated: "未执行",
  error: "出错",
  missing_title: "需要补全视频标题",
  missing_description: "需要补全视频简介",
  missing_cover: "尚未取得来源封面",
  connection_failed: "媒体服务器连接失败，请稍后重试",
  scan_complete: "来源成员已完整扫描",
  published_no_media_servers: "发布完成，目标库未配置媒体服务器",
  package_ready: "元数据和成品已准备完成",
  metadata_incomplete: "元数据尚未完整，请补全资料或重新解析来源",
  rule_short_circuit: "组内前面的规则已决定结果，本条无需继续执行",
  priority_short_circuit: "前面的规则组已决定结果，本组无需继续执行",
  blacklist_matched: "黑名单已命中",
  whitelist_matched: "白名单已命中",
  error_short_circuit: "前面的规则执行失败，后续规则未执行",
};
export const errorWords: Record<string, string> = {
  budget_exceeded: "请求超过媒体接口的 6 MiB 上限，请缩减本次输入。",
  media_not_configured: "此部署尚未登记视频订阅服务。",
  media_disabled: "此部署尚未启用视频订阅服务。",
  unauthorized: "网页登录已失效，请重新登录。",
  session_expired: "网页登录已过期，请重新登录。",
  forbidden: "本次操作未通过会话安全检查，请刷新账号状态。",
  operator_not_authorized: "当前账号没有视频订阅管理权限。",
  auth_required: "B 站会话已失效，请在账号页重新登录。",
  account_not_found: "这个 B 站账号已不存在，请重新选择。",
  invalid_input: "请检查填写的链接、来源和规则。",
  invalid_url: "请输入支持的 B 站视频链接。",
  unsupported_source: "这个来源类型目前不可用。",
  preview_expired: "解析预览已过期，请重新解析链接。",
  preview_not_found: "解析预览已失效，请重新解析链接。",
  quality_unavailable: "所选画质目前不可取得，请调整画质或重新登录。",
  target_unavailable: "目标媒体库暂不可用，请检查账号与媒体库。",
  revision_conflict: "记录已被其他操作更新，请重新读取后修改。",
  version_conflict: "记录版本已改变，请重新读取后修改。",
  idempotency_conflict: "这次请求与原操作不一致，请重新读取后操作。",
  dependency_unavailable: "后台或上游暂不可用，请稍后重试。",
  engine_unavailable: "下载引擎暂不可用。",
  cursor_conflict: "任务筛选已改变，请重新读取任务列表。",
  qr_expired: "二维码已过期，请重新生成。",
  qr_poll_too_fast: "扫码状态仍在等待更新，请稍后查看。",
  layout_change_requires_migration:
    "这个视频已按单视频发布。订阅需要分 P 剧集目录，请选择独立媒体库。",
  rule_error: "规则试算失败，请修正规则后启用。",
  rule_invalid: "规则格式或正则表达式不合法，请检查规则。",
  too_many_requests: "尝试过于频繁，请稍后重试。",
  not_found: "找不到这条记录，请重新读取。",
};
export function timestamp(seconds: number | null | undefined) {
  if (seconds == null || !Number.isFinite(seconds)) return "尚无记录";
  return new Date(seconds * 1000).toLocaleString("zh-CN", { hour12: false });
}
export function tone(state: string): "blue" | "yellow" | "red" | "gray" {
  if (
    [
      "completed",
      "published",
      "ready",
      "available",
      "active",
      "asset_indexed",
    ].includes(state)
  )
    return "blue";
  if (["failed", "auth_required", "rule_error", "unavailable"].includes(state))
    return "red";
  if (
    ["paused", "cancelled", "revoked", "unknown", "not_configured"].includes(
      state,
    )
  )
    return "gray";
  return "yellow";
}
export function word(state: string) {
  return stateWords[state] ?? reasonWords[state] ?? errorWords[state] ?? state;
}
export function qualityWord(value: string | null) {
  if (!value) return "待下载确认";
  return (
    (
      {
        "127": "8K",
        "120": "4K",
        "116": "1080P 60帧",
        "112": "1080P 高码率",
        "80": "1080P",
        "64": "720P",
        "32": "480P",
        "16": "360P",
      } as Record<string, string>
    )[value] ?? value
  );
}
