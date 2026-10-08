import type { RuleResult } from "./types";
import { reasonWords } from "./wording";

const fields: Record<string, string> = {
  title: "标题",
  description: "简介",
  uploader_id: "UP 主 ID",
  uploader_name: "UP 主名称",
  tags: "标签",
};
const operations: Record<string, string> = {
  equals: "等于",
  contains: "包含",
  prefix: "开头为",
  suffix: "结尾为",
  regex: "正则匹配",
};
type TraceRule = {
  rule_id: string;
  field: string;
  op: string;
  result: string;
  reason: string;
};
type TraceGroup = {
  group_id: string;
  list_kind: string;
  result: string;
  reason: string;
  rules: TraceRule[];
};
function groups(trace: unknown): TraceGroup[] {
  if (!trace || typeof trace !== "object") return [];
  const entries = (trace as Record<string, unknown>).trace;
  return Array.isArray(entries) ? (entries as TraceGroup[]) : [];
}

export function RuleResults({
  results,
  revision,
}: {
  results: RuleResult[];
  revision: number;
}) {
  return (
    <section className="media-rule-results" aria-label="规则试算结果">
      <h4>试算结果 · 规则版本 {revision}</h4>
      {!results.length && <p className="muted">这次预览没有可试算的分 P。</p>}
      {results.map((result) => (
        <article key={result.cid} className="media-rule-result">
          <p>
            <strong>分 P {result.cid}</strong> ·{" "}
            {result.decision === "download"
              ? "匹配"
              : result.decision === "skip"
                ? "跳过"
                : "试算失败"}
          </p>
          <p
            className={
              result.requires_rule_attention ? "media-warning" : "muted"
            }
          >
            {reasonWords[result.reason] ?? result.reason}
          </p>
          <details>
            <summary>查看逐条规则解释</summary>
            {groups(result.trace).length ? (
              groups(result.trace).map((group, index) => (
                <div className="media-rule-trace" key={group.group_id}>
                  <p>
                    <strong>
                      {group.list_kind === "blacklist" ? "黑名单" : "白名单"}组{" "}
                      {index + 1}
                    </strong>{" "}
                    · {reasonWords[group.result] ?? group.result}
                  </p>
                  <p className="muted">
                    {reasonWords[group.reason] ?? group.reason}
                  </p>
                  <ol>
                    {(group.rules ?? []).map((rule) => (
                      <li key={rule.rule_id}>
                        {fields[rule.field] ?? rule.field} ·{" "}
                        {operations[rule.op] ?? rule.op}：
                        {reasonWords[rule.result] ?? rule.result}（
                        {reasonWords[rule.reason] ?? rule.reason}）
                      </li>
                    ))}
                  </ol>
                </div>
              ))
            ) : (
              <p className="muted">没有需要逐条执行的规则。</p>
            )}
            <details>
              <summary>诊断数据</summary>
              <pre>{JSON.stringify(result.trace, null, 2)}</pre>
            </details>
          </details>
        </article>
      ))}
    </section>
  );
}
