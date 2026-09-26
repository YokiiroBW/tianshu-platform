import { webFetch } from "../../app/sessionTransport";
export type Session = {
  authenticated: boolean;
  csrf: string;
  username?: string;
  conversations?: { id: string; label: string; actors: string[] }[];
  dialogue?: { available: boolean; code: string; model: string };
};

const errors: Record<string, string> = {
  web_not_configured: "网页登录尚未配置。请由本机管理员配置账号与服务入口。",
  core_web_not_connected: "回复通道尚未接通，暂时不能发送消息。",
  unauthorized: "账号或密码不正确，或登录权限已失效。",
  session_expired: "登录已过期，请重新登录。",
  forbidden: "请求未通过会话安全检查，请重新连接。",
  too_many_requests: "尝试次数过多，请一分钟后重试。",
  dependency_unavailable: "后台服务暂时不可用，请稍后重新连接。",
  model_not_configured: "模型尚未配置，暂时不能发送。",
  version_conflict: "后台版本已变化，请刷新后再操作。",
  scope_changed: "会话授权范围已变化，请重新连接。",
  budget_exceeded: "对话内容超过当前读取预算。",
};

export async function request<T = Session>(
  path: string,
  signal: AbortSignal,
  body?: object,
  csrf?: string,
) {
  const response = await webFetch(`/api/web/${path}`, {
    method: body ? "POST" : "GET",
    credentials: "same-origin",
    cache: "no-store",
    signal,
    headers: body
      ? { "Content-Type": "application/json", "X-CSRF-Token": csrf ?? "" }
      : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!response.headers.get("content-type")?.includes("application/json"))
    throw new Error(
      "网页登录服务未连接，请从配置好的平台同源入口打开。当前页面仅为网页预览。",
    );
  const result = await response.json();
  if (!response.ok)
    throw new Error(
      `${errors[result.code] ?? "请求失败，请重新连接。"}（${result.code}${result.current_version ? `，当前版本 ${result.current_version}` : ""}）`,
    );
  return result as T;
}
