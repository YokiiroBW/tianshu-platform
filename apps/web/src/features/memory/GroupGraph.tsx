import { useMemo, useState } from "react";
import type { Stats, TopicPage } from "./groupTypes";

type Point = {
  id: string;
  label: string;
  x: number;
  y: number;
  size: number;
  topic?: string;
  count?: number;
};
type Link = { from: string; to: string; count: number; locators?: string[] };

/** Coordinates are a deterministic display of explicit membership/interaction, never stored facts. */
export default function GroupGraph({
  stats,
  topics,
  evidence,
  selectTopic,
}: {
  stats?: Stats;
  topics?: TopicPage;
  evidence: (locator: string) => void;
  selectTopic: (key: string) => void;
}) {
  const [mode, setMode] = useState<"interactions" | "topics">("interactions");
  const [zoom, setZoom] = useState(1);
  const [selected, setSelected] = useState<string | null>(null);
  const graph = useMemo(() => {
    const points: Point[] = [],
      links: Link[] = [];
    if (mode === "interactions") {
      const authors = [...(stats?.authors ?? [])]
        .sort(
          (a, b) =>
            b.messages - a.messages || a.sender_id.localeCompare(b.sender_id),
        )
        .slice(0, 80);
      const known = new Set(authors.map((a) => a.sender_id));
      authors.forEach((a, i) => {
        const angle = (2 * Math.PI * i) / Math.max(1, authors.length);
        points.push({
          id: a.sender_id,
          label: `QQ ${a.sender_id}`,
          x: 450 + 320 * Math.cos(angle),
          y: 240 + 175 * Math.sin(angle),
          size: 6 + Math.min(12, Math.sqrt(a.messages)),
          count: a.messages,
        });
      });
      stats?.edges.forEach((e) => {
        if (known.has(e.from_sender_id) && known.has(e.to_sender_id))
          links.push({
            from: e.from_sender_id,
            to: e.to_sender_id,
            count: e.interaction_count,
            locators: e.evidence_locators,
          });
      });
    } else {
      const items = (topics?.items ?? [])
        .flatMap((s) =>
          s.topics.map((t) => ({ ...t, key: `${s.segment_id}/${t.topic_id}` })),
        )
        .slice(0, 80);
      const people = [...new Set(items.flatMap((t) => t.person_ids))].sort();
      const byPerson = new Map<string, Point>();
      people.forEach((id, i) => {
        const angle = (2 * Math.PI * i) / Math.max(1, people.length);
        const p = {
          id,
          label: `人物 ${id}`,
          x: 450 + 330 * Math.cos(angle),
          y: 240 + 175 * Math.sin(angle),
          size: 7,
        };
        points.push(p);
        byPerson.set(id, p);
      });
      items.forEach((t, i) => {
        const members = t.person_ids.map((id) => byPerson.get(id)!);
        const average = (axis: "x" | "y") =>
          members.length
            ? members.reduce((v, p) => v + p[axis], 0) / members.length
            : axis === "x"
              ? 450
              : 240;
        points.push({
          id: t.key,
          label: t.summary,
          topic: t.key,
          x: (average("x") + 450) / 2 + Math.cos(i * 2.4) * 45,
          y: (average("y") + 240) / 2 + Math.sin(i * 2.4) * 45,
          size: 8,
        });
        t.person_ids.forEach((id) =>
          links.push({ from: id, to: t.key, count: 1 }),
        );
      });
    }
    return { points, links, index: new Map(points.map((p) => [p.id, p])) };
  }, [stats, topics, mode]);
  const point = selected ? graph.index.get(selected) : null;
  return (
    <section className="group-graph" aria-label="群互动与话题图">
      <div className="group-toolbar">
        <button
          className="button"
          aria-pressed={mode === "interactions"}
          onClick={() => {
            setMode("interactions");
            setSelected(null);
          }}
        >
          互动关系
        </button>
        <button
          className="button"
          aria-pressed={mode === "topics"}
          onClick={() => {
            setMode("topics");
            setSelected(null);
          }}
        >
          话题点云
        </button>
        <label>
          缩放{" "}
          <input
            aria-label="图缩放"
            type="range"
            min="0.6"
            max="2"
            step="0.1"
            value={zoom}
            onChange={(e) => setZoom(Number(e.target.value))}
          />
        </label>
        <button className="button" onClick={() => setZoom(1)}>
          还原视图
        </button>
      </div>
      <p className="muted">
        {mode === "interactions"
          ? "连线只表示实际回复或 @，不代表亲密程度。绘制最活跃的前 80 位；账号 ID 不与画像人物 ID 自动合并。"
          : "按已整理话题与真实参与者排布，最多显示本页 80 个话题。点的位置是布局，不代表语义距离或共同同意。"}
      </p>
      {!graph.points.length ? (
        <p>
          {mode === "interactions"
            ? "当前窗口没有可绘制互动。"
            : "当前页尚无有效整理话题。"}
        </p>
      ) : (
        <div className="group-graph-scroll">
          <svg
            viewBox="0 0 900 480"
            role="group"
            aria-label={mode === "interactions" ? "定向互动图" : "整理话题点云"}
          >
            <g
              transform={`translate(450 240) scale(${zoom}) translate(-450 -240)`}
            >
              {graph.links.map((link, i) => {
                const a = graph.index.get(link.from)!,
                  b = graph.index.get(link.to)!;
                return (
                  <line
                    key={`${link.from}/${link.to}/${i}`}
                    x1={a.x}
                    y1={a.y}
                    x2={b.x}
                    y2={b.y}
                    strokeWidth={1 + Math.min(4, Math.log2(link.count + 1))}
                    className="group-graph-edge"
                  >
                    <title>{`${a.label} → ${b.label} · ${link.count}`}</title>
                  </line>
                );
              })}
              {graph.points.map((p) => (
                <g
                  key={p.id}
                  role="button"
                  tabIndex={0}
                  aria-label={p.label}
                  className="group-graph-node"
                  data-selected={selected === p.id || undefined}
                  onClick={() => {
                    setSelected(p.id);
                    if (p.topic) selectTopic(p.topic);
                  }}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" || e.key === " ") {
                      e.preventDefault();
                      setSelected(p.id);
                      if (p.topic) selectTopic(p.topic);
                    }
                  }}
                >
                  <circle
                    cx={p.x}
                    cy={p.y}
                    r={p.size}
                    className={p.topic ? "group-topic-point" : undefined}
                  />
                  <title>{p.label}</title>
                  <text x={p.x} y={p.y + p.size + 15} textAnchor="middle">
                    {p.topic
                      ? `话题 ${graph.points.filter((q) => q.topic).findIndex((q) => q.id === p.id) + 1}`
                      : p.label}
                  </text>
                </g>
              ))}
            </g>
          </svg>
        </div>
      )}
      {point && (
        <div className="group-selection">
          <strong>{point.label}</strong>
          {point.count !== undefined && (
            <p>当前统计窗口 {point.count} 条消息</p>
          )}
          {graph.links
            .filter((l) => l.from === point.id && l.locators?.length)
            .map((l) => (
              <p key={l.to}>
                → QQ {l.to} · {l.count} 次定向互动{" "}
                {l.locators!.map((ref, i) => (
                  <button
                    className="button"
                    key={ref}
                    onClick={() => evidence(ref)}
                  >
                    证据 {i + 1}
                  </button>
                ))}
              </p>
            ))}
        </div>
      )}
    </section>
  );
}
