import { webFetch } from "./sessionTransport";

// CONNECT-B-API.md: every integration read is a same-origin, session-bound POST.
// The browser never chooses an upstream address, service token or reader identity.
const descriptions: Record<string, string> = {
  web_not_configured: "网页登录尚未启用。",
  not_configured: "这个连接尚未由部署端配置。",
  knowledge_not_configured: "项目知识连接尚未由部署端配置。",
  life_not_configured: "生活资料连接尚未由部署端配置。",
  knowledge_read_required: "当前账号没有项目知识读取权限。",
  life_read_required: "当前账号没有角色生活读取权限。",
  knowledge_credential_missing: "项目知识服务凭据缺失，请由部署管理员检查。",
  life_credential_missing: "角色生活服务凭据缺失，请由部署管理员检查。",
  memory_not_configured: "记忆浏览尚未由部署端配置。",
  memory_read_required: "当前账号没有记忆浏览权限。",
  memory_credential_missing: "记忆服务凭据缺失，请由部署管理员检查。",
  memory_identity_not_ready:
    "当前账号的记忆身份映射尚未就绪，请由部署管理员核对。",
  upstream_forbidden: "上游服务拒绝了读取授权，请由部署管理员检查。",
  unauthorized: "登录已失效，请重新登录。",
  session_expired: "登录已过期，请重新登录。",
  forbidden: "当前账号或服务身份没有读取权限。",
  operator_not_authorized: "当前账号没有读取权限。",
  dependency_unavailable: "上游暂时无法读取，请稍后重试。",
  timeout: "读取超时；本次没有取得数据。",
  invalid_upstream: "上游返回了无法使用的数据。",
  version_conflict: "资料在读取期间发生变化，请重新读取列表。",
  project_conflict: "项目在读取期间发生变化，请重新读取。",
  stale_evidence: "资料来源已经变化，请重新读取。",
  invalid_cursor: "续读位置已经失效，请从第一页重新读取。",
  cursor_stale: "资料在分页期间发生变化，请从第一页重新读取。",
  scope_changed: "读取范围已经变化，请从第一页重新读取。",
  too_many_requests: "同时读取过多，请稍后重试。",
  project_uninitialized: "项目尚未建立资料目录。",
  budget_too_small: "本次读取预算无法容纳一个完整结果。",
  budget_exceeded: "结果超出服务端读取预算。",
  not_found: "该条目不存在，或当前身份不能读取。",
};

export class IntegrationError extends Error {
  constructor(
    readonly code: string,
    readonly status: number,
  ) {
    super(`${descriptions[code] ?? "读取失败，请重试。"}（${code}）`);
  }
}

export async function integrationPost<T>(
  path: string,
  body: object,
  csrf: string,
  signal: AbortSignal,
): Promise<T> {
  const response = await webFetch(`/api/web/${path}`, {
    method: "POST",
    credentials: "same-origin",
    cache: "no-store",
    signal,
    headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf },
    body: JSON.stringify(body),
  });
  if (!response.headers.get("content-type")?.includes("application/json"))
    throw new IntegrationError("web_not_configured", response.status);
  const result = await response.json();
  if (!response.ok)
    throw new IntegrationError(
      String(result?.code ?? "invalid_upstream"),
      response.status,
    );
  return result as T;
}

export function readFailure(cause: unknown) {
  if (cause instanceof Error) return cause.message;
  return "连接中断；本次没有取得数据。";
}

export function deploymentFailure(code: string) {
  return [
    "not_configured",
    "knowledge_not_configured",
    "life_not_configured",
    "operator_not_authorized",
    "unauthorized",
    "forbidden",
  ].includes(code);
}
