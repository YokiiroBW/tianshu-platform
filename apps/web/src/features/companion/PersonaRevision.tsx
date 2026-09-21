import { StatusRail } from "../../components/StatusRail";
import {
  changeLabels,
  fieldLabels,
  shortDigest,
  shortTime,
  stateLabel,
  type CompareField,
  type CompareView,
  type RevisionView,
} from "./personaTypes";

type Props = {
  revision: RevisionView | null;
  comparison: CompareView | null;
  busy: boolean;
  /** A revision this page was asked for but the producer does not answer for. */
  unavailable: string;
};

function Value({ label, value }: { label: string; value: string | null }) {
  return (
    <div>
      <dt>{label}</dt>
      <dd
        className={
          value === null || value === "" ? "persona-absent" : undefined
        }
      >
        {value === null || value === "" ? "这一版没有这个字段" : value}
      </dd>
    </div>
  );
}

/** One field of a comparison: what changed, and both sides as the producer reported them. */
function Changed({ name, field }: { name: string; field: CompareField }) {
  return (
    <div className="persona-change">
      <StatusRail
        tone={field.change === "unchanged" ? "gray" : "yellow"}
        label={`${fieldLabels[name] ?? name} · ${changeLabels[field.change]}`}
      >
        <dl className="persona-facts">
          <Value
            label={`基线${field.presence.left ? "" : "（缺）"}`}
            value={field.left}
          />
          <Value
            label={`对照${field.presence.right ? "" : "（缺）"}`}
            value={field.right}
          />
        </dl>
      </StatusRail>
    </div>
  );
}

export function PersonaRevision({
  revision,
  comparison,
  busy,
  unavailable,
}: Props) {
  if (unavailable && !busy) {
    return (
      <StatusRail tone="red" label="已选版本不可用">
        <p>
          {unavailable}
          这里不会显示成空内容：请从历史里另选一版，或重新打开第一页。
        </p>
      </StatusRail>
    );
  }
  if (comparison) {
    const names = ["persona", "tone", "style", "address"];
    return (
      <section className="persona-revision" aria-label="版本比较">
        <h3>
          比较 {shortDigest(comparison.left.revision_id)} 与{" "}
          {shortDigest(comparison.right.revision_id)}
        </h3>
        <p className="muted" role="status">
          {comparison.content_identical
            ? "两版的四个字段内容相同。"
            : "两版的字段内容不同，下面逐项列出。"}
          {comparison.additional_fields_present
            ? `另有本页不解释的字段：${comparison.additional_field_names.join("、")}。`
            : "没有本页不解释的字段。"}
          {comparison.additional_fields_changed
            ? "这些字段的内容发生了变化。"
            : ""}
          {`当前状态：${stateLabel(comparison.state)} · 基线版本 ${comparison.persona_version}。`}
        </p>
        <div className="persona-changes">
          {names.map((name) => (
            <Changed key={name} name={name} field={comparison.fields[name]} />
          ))}
        </div>
      </section>
    );
  }
  if (!revision) {
    return (
      <StatusRail tone="gray" label="尚未选择版本">
        <p>
          从左侧历史里选择一版查看内容，或先设一版为对比基线，再与另一版比较。
        </p>
      </StatusRail>
    );
  }
  const content = revision.revision.content ?? {};
  return (
    <section className="persona-revision" aria-label="选中的版本">
      <h3>
        {shortDigest(revision.revision.revision_id)}
        {revision.is_published ? " · 当前发布" : ""}
        {revision.is_draft ? " · 当前草稿" : ""}
      </h3>
      <p className="muted" role="status">
        {`来源 ${revision.revision.source ?? "未记录"} · 时间 ${shortTime(revision.revision.created_at)} · 状态 ${stateLabel(revision.state)}`}
        {revision.revision.note ? ` · 备注 ${revision.revision.note}` : ""}
      </p>
      <dl className="persona-facts persona-content">
        {["persona", "tone", "style", "address"].map((name) => (
          <Value
            key={name}
            label={fieldLabels[name]}
            value={content[name] ?? null}
          />
        ))}
      </dl>
      <p className="muted">
        {revision.revision.additional_fields.length
          ? `另有本页不解释的字段：${revision.revision.additional_fields.join("、")}。`
          : "这一版没有本页不解释的字段。"}
      </p>
    </section>
  );
}
