import { errorWords, reasonWords, stateWords } from "./wording";

/** Keep actionable wording visible; raw upstream codes remain available for diagnosis. */
export function ReasonNote({ code }: { code: string }) {
  if (!code) return null;
  const description = errorWords[code] ?? reasonWords[code] ?? stateWords[code];
  return (
    <details className="media-reason">
      <summary>{description ?? "此步骤需要处理，查看诊断信息"}</summary>
      <code>原因码：{code}</code>
    </details>
  );
}
