import { StatusRail } from "../../components/StatusRail";
import {
  bodyState,
  changeLabels,
  fieldLabels,
  shortDigest,
  shortTime,
  stateLabel,
  type CompareField,
  type CompareView,
  type RevisionBody,
  type RevisionView,
} from "./personaTypes";

type Props = {
  revision: RevisionView | null;
  comparison: CompareView | null;
  busy: boolean;
  /** A revision this page was asked for but the producer does not answer for. */
  unavailable: string;
};

const BODY_CLASS: Record<string, string> = {
  absent: "persona-absent",
  null: "persona-absent",
  empty: "persona-absent",
  text: "",
};

/**
 * One field of a body, in one of its three real states.
 *
 * `present` comes from the server's own per-field projection - the `present` list for one revision
 * and `presence` for each side of a comparison - so "not provided", "stated as null" and "stated as
 * the empty string" stay three different readings. Text keeps its own line breaks and is rendered
 * as text, never as markup.
 */
function Value({
  label,
  value,
  present,
}: {
  label: string;
  value: string | null;
  present: boolean;
}) {
  const state = bodyState(value, present);
  return (
    <div>
      <dt>{label}</dt>
      <dd className={BODY_CLASS[state.kind]} data-body-state={state.kind}>
        {state.text}
      </dd>
    </div>
  );
}

/** The four interpreted fields of one revision, each foldable so long text stays readable. */
function Body({ revision }: { revision: RevisionBody }) {
  return (
    <div className="persona-content">
      {["persona", "tone", "style", "address"].map((name) => {
        const present = revision.present.includes(name);
        const state = bodyState(revision.content[name] ?? null, present);
        return (
          <details key={name} className="persona-field" open>
            <summary>{fieldLabels[name]}</summary>
            <p
              className={`persona-field-body ${BODY_CLASS[state.kind]}`}
              data-body-state={state.kind}
            >
              {state.text}
            </p>
          </details>
        );
      })}
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
        <dl className="persona-facts persona-sides">
          <Value
            label="基线"
            value={field.left}
            present={field.presence.left}
          />
          <Value
            label="对照"
            value={field.right}
            present={field.presence.right}
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
      <Body revision={revision.revision} />
      <p className="muted">
        {revision.revision.additional_fields.length
          ? `另有本页不解释的字段：${revision.revision.additional_fields.join("、")}。`
          : "这一版没有本页不解释的字段。"}
      </p>
      <p className="muted">
        {`这一版的指针：当前发布 ${shortDigest(revision.published_revision)} · 当前草稿 ${shortDigest(revision.draft_revision)} · 版本 ${revision.persona_version}`}
      </p>
    </section>
  );
}
