/** Same-origin web-console calls for the models section; never a service credential. */

export type SessionState = {
  authenticated: boolean;
  csrf: string;
  username?: string;
};

export type Template = {
  template_id: string;
  label: string;
  target: string;
  provider_id: string;
  model_id: string;
  verified_capabilities: string[];
  lifetime_seconds: number;
  timeout_ms: number;
};

export type Version = {
  version: number;
  revoked: boolean;
  availability: string;
  published_at: string;
  usable_until: string;
  providers: { provider_id: string; protocol: string; model_id: string }[];
  bindings: { workload: string; provider_id: string; model_id: string }[];
};

export type Target = {
  target: string;
  label: string;
  detail: string;
  protocol: string;
  workload: string;
  current_version: number | null;
  availability: string;
  usable_until: string | null;
  versions: Version[];
};

export type Management = {
  available: boolean;
  code: string;
  unlocked: boolean;
  unlock_ttl_seconds: number;
  templates: Template[];
};

export type ModelsView = { management: Management; targets: Target[] };

export type Preview = {
  template: Template;
  target: string;
  expected_version: number | null;
  version: number;
  published_at: string;
  usable_until: string;
  providers: { provider_id: string; protocol: string; model_id: string }[];
  bindings: { workload: string; model_id: string }[];
};

export type Publication = {
  target: string;
  version: number;
  expected_version: number | null;
  state: "published" | "replayed";
  deduplicated: boolean;
  usable_until: string;
};

const messages: Record<string, string> = {
  web_not_configured: "网页配置尚未启用，模型配置不可管理。",
  unauthorized: "账号或密码不正确，或管理授权已失效。",
  session_expired: "登录已过期，请重新登录。",
  forbidden: "请求未通过会话安全检查，请重新连接。",
  too_many_requests: "尝试次数过多，请一分钟后重试。",
  management_disabled: "该部署未开启网页模型管理。",
  operator_not_authorized: "当前账号没有模型配置权限。",
  management_required: "请先验证管理员密码，再执行模型配置操作。",
  not_found: "找不到该配置模板或版本。",
  version_conflict: "后台版本已变化，已重新读取；请确认后再发布。",
  idempotency_conflict: "这次发布请求与之前的请求不一致，请重新预览后再发布。",
  dependency_unavailable: "后台服务暂时不可用，请稍后重试。",
  invalid_input: "请求内容不符合当前接口要求。",
};

export class WebError extends Error {
  constructor(
    readonly code: string,
    readonly status: number,
  ) {
    super(`${messages[code] ?? "请求失败，请重新连接。"}（${code}）`);
  }
}

export async function call<T>(
  path: string,
  body: object,
  csrf: string,
  signal: AbortSignal,
): Promise<T> {
  const response = await fetch(`/api/web/${path}`, {
    method: "POST",
    credentials: "same-origin",
    cache: "no-store",
    signal,
    headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf },
    body: JSON.stringify(body),
  });
  if (!response.headers.get("content-type")?.includes("application/json"))
    throw new WebError("web_not_configured", response.status);
  const result = await response.json();
  if (!response.ok) throw new WebError(String(result.code), response.status);
  return result as T;
}

export async function session(signal: AbortSignal): Promise<SessionState> {
  const response = await fetch("/api/web/session", {
    credentials: "same-origin",
    cache: "no-store",
    signal,
  });
  if (!response.headers.get("content-type")?.includes("application/json"))
    throw new WebError("web_not_configured", response.status);
  const result = await response.json();
  if (!response.ok)
    throw new WebError(String(result.code ?? "forbidden"), response.status);
  return result as SessionState;
}
