import {
  ArrowDownUp,
  ChevronLeft,
  ChevronRight,
  RotateCcw,
} from "lucide-react";
import { StatusRail } from "../../components/StatusRail";
import {
  kindLabels,
  rowTitle,
  shortDigest,
  shortTime,
  type HistoryKind,
  type HistoryPage,
  type HistoryRow,
} from "./personaTypes";

type Props = {
  kind: HistoryKind;
  subject: string;
  pages: HistoryPage[];
  current: number;
  busy: boolean;
  baseline: string | null;
  onKind: (kind: HistoryKind) => void;
  onPrevious: () => void;
  onNext: () => void;
  onRestart: () => void;
  onSelect: (revisionId: string) => void;
  onBaseline: (revisionId: string) => void;
  onCompare: (revisionId: string) => void;
};

const order: HistoryKind[] = [
  "revisions",
  "publications",
  "approvals",
  "rollbacks",
];

/** One row's facts, in this page's own words. Text is never invented for an absent field. */
function facts(kind: HistoryKind, row: HistoryRow) {
  const rows: [string, string][] = [];
  if (kind === "revisions") {
    rows.push(["修订", shortDigest(row.revision_id)]);
    rows.push(["来源", row.source ?? "没有记录来源"]);
    rows.push(["操作者", row.operator ?? "没有记录操作者"]);
    rows.push(["时间", shortTime(row.created_at)]);
    rows.push(["父修订", row.parent ? shortDigest(row.parent) : "没有父修订"]);
    rows.push(["批准", row.decision ?? "尚未批准"]);
  } else if (kind === "publications") {
    rows.push(["发布编号", shortDigest(row.publication_id)]);
    rows.push(["修订", shortDigest(row.revision_id)]);
    rows.push(["类型", row.kind ?? "没有记录类型"]);
    rows.push(["状态", row.state ?? "没有记录状态"]);
    rows.push([
      "取代",
      row.supersedes ? shortDigest(row.supersedes) : "没有取代任何版本",
    ]);
    rows.push(["时间", shortTime(row.created_at)]);
  } else if (kind === "approvals") {
    rows.push(["批准编号", shortDigest(row.approval_id)]);
    rows.push(["修订", shortDigest(row.revision_id)]);
    rows.push(["决定", row.decision ?? "没有记录决定"]);
    rows.push(["操作者", row.operator ?? "没有记录操作者"]);
    rows.push(["时间", shortTime(row.created_at)]);
  } else {
    rows.push(["回退编号", shortDigest(row.rollback_id)]);
    rows.push(["恢复为", shortDigest(row.restored_revision)]);
    rows.push(["目标修订", shortDigest(row.target_revision)]);
    rows.push(["被取代", shortDigest(row.superseded_revision)]);
    rows.push(["操作者", row.operator ?? "没有记录操作者"]);
    rows.push(["时间", shortTime(row.created_at)]);
  }
  return rows;
}

/** A row may point at a revision the page can read; the others state their facts and nothing more. */
function revisionOf(row: HistoryRow) {
  return row.revision_id ?? row.target_revision ?? null;
}

export function PersonaHistory({
  kind,
  subject,
  pages,
  current,
  busy,
  baseline,
  onKind,
  onPrevious,
  onNext,
  onRestart,
  onSelect,
  onBaseline,
  onCompare,
}: Props) {
  const page = pages[current];
  const entries = page?.entries ?? [];
  return (
    <section className="persona-history" aria-label={`${subject} 的历史`}>
      <div className="persona-kinds" role="group" aria-label="历史类别">
        {order.map((item) => (
          <button
            key={item}
            type="button"
            className="button persona-kind"
            aria-pressed={item === kind}
            disabled={busy}
            onClick={() => onKind(item)}
          >
            {kindLabels[item]}
          </button>
        ))}
      </div>
      <div className="persona-pager">
        <p className="muted" role="status">
          {busy
            ? "正在读取这一页…"
            : page
              ? `第 ${current + 1} 页 · 本页 ${entries.length} 条 · 基线版本 ${page.persona_version} · 按角色服务返回的顺序`
              : "这一页还没有读取结果。"}
        </p>
        <div className="persona-pager-actions">
          <button
            type="button"
            className="button"
            disabled={busy || current === 0}
            onClick={onPrevious}
          >
            <ChevronLeft aria-hidden="true" />
            上一页
          </button>
          <button
            type="button"
            className="button"
            disabled={busy || !page?.has_more}
            onClick={onNext}
          >
            下一页
            <ChevronRight aria-hidden="true" />
          </button>
          <button
            type="button"
            className="button"
            disabled={busy || (current === 0 && !!page)}
            onClick={onRestart}
          >
            <RotateCcw aria-hidden="true" />
            重新打开第一页
          </button>
        </div>
      </div>
      {!page ? (
        <p className="persona-empty" role="status">
          {busy ? "正在读取这一页…" : "这一页还没有读取结果。"}
        </p>
      ) : !entries.length ? (
        <p className="persona-empty" role="status">
          {busy
            ? "正在读取…"
            : `这一页没有${kindLabels[kind]}记录：角色服务读过之后确实没有返回任何条目。`}
        </p>
      ) : (
        <ol className="persona-rows">
          {entries.map((row, index) => {
            const revision = revisionOf(row);
            const selected = revision !== null && revision === baseline;
            return (
              <li
                key={`${row.revision_id ?? row.publication_id ?? row.approval_id ?? row.rollback_id ?? index}`}
              >
                <StatusRail
                  tone={selected ? "blue" : "gray"}
                  label={rowTitle(kind, row)}
                  selected={selected}
                >
                  <dl className="persona-facts">
                    {facts(kind, row).map(([label, value]) => (
                      <div key={label}>
                        <dt>{label}</dt>
                        <dd>{value}</dd>
                      </div>
                    ))}
                  </dl>
                  <div className="persona-row-actions">
                    <button
                      type="button"
                      className="button"
                      disabled={!revision || busy}
                      onClick={() => revision && onSelect(revision)}
                    >
                      查看这一版
                    </button>
                    <button
                      type="button"
                      className="button"
                      disabled={!revision || busy}
                      onClick={() => revision && onBaseline(revision)}
                    >
                      <ArrowDownUp aria-hidden="true" />
                      设为对比基线
                    </button>
                    <button
                      type="button"
                      className="button"
                      disabled={
                        !baseline || !revision || revision === baseline || busy
                      }
                      onClick={() => revision && onCompare(revision)}
                    >
                      与基线比较
                    </button>
                  </div>
                </StatusRail>
              </li>
            );
          })}
        </ol>
      )}
    </section>
  );
}
