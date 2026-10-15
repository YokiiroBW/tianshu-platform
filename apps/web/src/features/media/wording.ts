import { videoQualities } from "./streamPolicy";

export const storageReasonWords: Record<string, string> = {
  storage_scanning: "正在后台统计分类占用。",
  storage_subscription_unavailable: "订阅媒体目录暂无法完整统计。",
  storage_emby_unavailable: "Emby 媒体库暂不可读，容量尚未完整统计。",
  storage_path_unmapped: "部分 Emby 媒体目录尚未映射，容量暂未知。",
  storage_path_unavailable: "部分媒体目录暂不可读，容量尚未完整统计。",
  storage_measurement_inconsistent:
    "分类统计与磁盘容量不一致，暂不展示分类比例。",
  storage_capacity_unavailable: "暂未取得磁盘容量。",
  storage_logical_estimate: "按文件逻辑大小估算，分类占用为近似值。",
};

export const stateWords: Record<string, string> = {
  queued: "等待下载",
  downloading: "正在下载",
  validating: "校验成品",
  metadata_ready: "元数据已就绪",
  metadata: "检查元数据",
  publishing: "正在发布",
  asset_indexed: "媒体已入库",
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
  library_files_verified: "媒体服务器已收录文件",
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
  watch_later: "稍后再看",
};
export const destinationModeWords = {
  source: "按来源自动归类",
  custom: "自定义目录",
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
  field_missing: "字段值未知，取反也不会视为命中",
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
  invalid_destination_subdirectory:
    "请检查整理子目录：使用相对目录，不含空层级、.、..、保留文件名或不兼容字符。",
  destination_not_supported:
    "这个目标的目录由资产库管理，整理子目录请留空，或选择媒体服务器目标。",
  directory_overlap: "整理目录不能与暂存目录或其他目标重叠",
  target_mapping_in_use:
    "已有任务使用此目录映射；更换目录或库请添加新的连接，原连接可继续更新密钥",
  target_managed_by_config: "此连接通过部署配置维护",

  unsafe_library_path: "媒体库目录包含链接或不安全路径，请检查目录配置。",
  source_changed: "成品文件已变化，请重新校验后重试。",
  target_exists: "目标目录已有不同文件，已保留原文件，请检查后重试。",
  scope_changed: "媒体库配置已变化，请检查任务对应的目标库。",
  budget_exceeded: "请求超过媒体接口的 6 MiB 上限，请缩减本次输入。",
  media_not_configured: "此部署尚未登记视频订阅服务。",
  media_disabled: "此部署尚未启用视频订阅服务。",
  unauthorized: "网页登录已失效，请重新登录。",
  session_expired: "网页登录已过期，请重新登录。",
  forbidden: "本次操作未通过会话安全检查，请刷新账号状态。",
  operator_not_authorized: "当前账号没有视频订阅管理权限。",
  auth_required: "B 站会话已失效，请在账号页重新登录。",
  account_not_found: "这个 B 站账号已不存在，请重新选择。",
  subscription_account_immutable:
    "此订阅跟随该账号的稍后再看，其他账号可新建订阅。",
  invalid_input: "请检查填写的链接、来源和规则。",
  invalid_url: "请输入支持的 B 站视频链接。",
  unsupported_source: "这个来源类型目前不可用。",
  preview_expired: "解析预览已过期，请重新解析链接。",
  preview_not_found: "解析预览已失效，请重新解析链接。",
  quality_unavailable: "所选画质目前不可取得，请调整画质或重新登录。",
  quality_unverified:
    "暂时无法核实成品是否满足音视频策略，请读取任务状态后重试。",
  quality_mismatch: "实际音视频流不符合所选策略，请检查任务详情并调整策略。",
  preview_account_conflict:
    "试算账号与订阅登记账号不同，请使用相同的 B 站账号重新解析。",
  source_budget_exceeded: "来源成员扫描超时或超出范围，请稍后重新试算。",
  target_unavailable: "目标媒体库暂不可用，请检查账号与媒体库。",
  revision_conflict: "记录已被其他操作更新，请重新读取后修改。",
  job_not_deletable:
    "任务仍在执行，暂时不能删除记录。请等待任务结束后重新读取。",
  version_conflict: "记录版本已改变，请重新读取后修改。",
  idempotency_conflict: "这次请求与原操作不一致，请重新读取后操作。",
  dependency_unavailable: "后台或上游暂不可用，请稍后重试。",
  engine_unavailable: "下载引擎暂不可用。",
  cursor_conflict: "任务筛选已改变，请重新读取任务列表。",
  qr_expired: "二维码已过期，请重新生成。",
  qr_poll_too_fast: "扫码状态仍在等待更新，请稍后查看。",
  layout_change_requires_migration:
    "这个视频已按单视频发布。订阅需要分 P 剧集目录，请选择独立整理子目录或媒体库。",
  rule_error: "规则试算失败，请修正规则后启用。",
  rule_invalid: "规则格式或正则表达式不合法，请检查规则。",
  too_many_requests: "尝试过于频繁，请稍后重试。",
  not_found: "找不到这条记录，请重新读取。",
  invalid_music_id: "歌曲或歌单标识无效，请重新读取。",
  music_auth_required: "网易云登录已失效，请重新扫码登录。",
  music_account_unavailable: "网易云账号已断开，请重新连接。",
  music_upstream_unavailable: "网易云暂时无法连接，请稍后重试。",
  music_upstream_invalid: "网易云返回的数据不完整，本次未替换已保存歌单。",
  music_upstream_rejected: "网易云未能返回所请求的内容，请检查账号状态后重试。",
  music_scan_incomplete: "歌单未读取完整，已保留上次同步结果。",
  music_source_changed: "读取期间歌单发生变化，请重新同步。",
  music_snapshot_too_large: "歌单超过本次读取范围，已保留原记录。",
  music_rate_limited: "网易云请求暂受限制，请稍后重试。",
  music_subscription_paused: "此歌单已暂停，请先恢复同步。",
  music_directory_absolute: "请填写音乐目录的完整路径。",
  music_directory_unavailable: "音乐目录暂不可访问，请检查目录路径。",
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
    videoQualities.find((quality) => quality.quality_id === value)?.label ??
    value
  );
}
