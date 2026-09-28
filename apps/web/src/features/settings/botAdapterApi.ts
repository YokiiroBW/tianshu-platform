import { webFetch } from "../../app/sessionTransport";

export type AdapterKind = "astrbot" | "nonebot";
export type AdapterAccount = { id: string; platform: string; label: string };
export type AdapterConnection = {
  id: string;
  name: string;
  adapter: AdapterKind;
  address: string;
  account_id: string;
  conversation: { kind: "group" | "private"; id: string };
  allowed_authors: string[];
  actor_id: string;
  enabled: boolean;
  revision: number;
  state: "draft" | "disabled" | "ready" | "degraded" | "unknown";
  last_error: string | null;
  last_checked_at: string | null;
};
export type AdapterView = {
  available: boolean;
  unlocked: boolean;
  actors: { id: string; label: string }[];
  connections: AdapterConnection[];
};
export type AdapterProbe = {
  draft_id: string;
  expires_at: string;
  protocol: string;
  instance_id: string;
  accounts: AdapterAccount[];
};

const messages: Record<string, string> = {
  web_not_configured: "此部署尚未提供机器人适配器管理接口。",
  management_disabled: "此部署尚未启用机器人适配器管理。",
  not_configured: "此部署尚未启用机器人适配器管理。",
  operator_not_authorized: "当前账号没有机器人接入管理权限。",
  management_required: "管理员解锁已失效，请重新输入密码。",
  session_expired: "登录已过期，请重新登录。",
  unauthorized: "验证失败。请检查插件连接密钥或管理员登录状态。",
  forbidden: "当前操作未获授权，请检查管理员权限。",
  invalid_input: "输入不符合服务端要求，请检查地址、账号和允许范围。",
  not_found: "草稿或连接已失效，请刷新后重新检测。",
  draft_expired: "检测草稿已过期，请重新检测连接。",
  version_conflict: "连接已在另一窗口改变，请刷新后核对。",
  idempotency_conflict: "本次请求与已有记录冲突，请刷新后核对。",
  dependency_unavailable:
    "插件或后台暂时不可达，请检查地址、插件运行状态与网络。",
  busy: "插件暂时忙碌，请稍后手动重试。",
  protocol_mismatch: "插件协议版本不兼容，请更新插件。",
  unsupported_version: "插件版本不兼容，请更新插件。",
  invalid_upstream: "插件返回了无法识别的内容，请确认地址指向天枢插件。",
  connection_failed: "无法连接插件。请检查地址和插件运行状态。",
  timeout: "连接超时，请检查插件地址和网络。",
};

export class AdapterApiError extends Error {
  constructor(
    readonly code: string,
    readonly status: number,
    readonly executionState: string | null,
  ) {
    super(`${messages[code] ?? "操作未完成，请检查后台状态。"}（${code}）`);
  }
}

export async function adapterPost<T>(
  operation: string,
  body: object,
  csrf: string,
  signal: AbortSignal,
): Promise<T> {
  const response = await webFetch(`/api/web/bot-adapters/${operation}`, {
    method: "POST",
    credentials: "same-origin",
    cache: "no-store",
    signal,
    headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf },
    body: JSON.stringify(body),
  });
  if (!response.headers.get("content-type")?.includes("application/json"))
    throw new AdapterApiError("web_not_configured", response.status, null);
  const result = await response.json();
  if (!response.ok)
    throw new AdapterApiError(
      String(result?.code ?? "invalid_upstream"),
      response.status,
      typeof result?.execution_state === "string"
        ? result.execution_state
        : null,
    );
  return result as T;
}
