import { webFetch } from "../../app/sessionTransport";
import { errorWords } from "./wording";

export class MediaError extends Error {
  constructor(
    readonly code: string,
    readonly status: number,
  ) {
    super(
      `${errorWords[code] ?? "本次操作未完成，请检查当前状态后重试。"}（${code}）`,
    );
  }
}
export async function mediaCall<T>(
  operation: string,
  body: object,
  csrf: string,
  signal: AbortSignal,
): Promise<T> {
  const serialized = JSON.stringify(body);
  if (new TextEncoder().encode(serialized).byteLength > 6 * 1024 * 1024)
    throw new MediaError("budget_exceeded", 413);
  const response = await webFetch(`/api/web/media/${operation}`, {
    method: "POST",
    credentials: "same-origin",
    cache: "no-store",
    signal,
    headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf },
    body: serialized,
  });
  if (!response.headers.get("content-type")?.includes("application/json"))
    throw new MediaError("dependency_unavailable", response.status);
  const result = await response.json();
  if (!response.ok)
    throw new MediaError(
      result.code ?? "dependency_unavailable",
      response.status,
    );
  if (!result || typeof result !== "object")
    throw new MediaError("dependency_unavailable", 502);
  return result as T;
}
