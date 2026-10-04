import { useState } from "react";
import { useDraftId, useLifeAction, useLifeResource } from "./useLifeRuntime";
import { ResourceFeedback, RuntimeFeedback } from "./RuntimeFeedback";
import { showState, type LifeAccess } from "./runtimeApi";
import type { activities_record, open_work_record } from "./runtimeTypes";
import { showMood } from "./lifeLabels";

function time(value: number | null) {
  return value ? new Date(value * 1000).toLocaleString("zh-CN") : "未安排";
}

function localInputTime(value: number | null) {
  if (value === null) return "";
  const date = new Date(value * 1000);
  return new Date(date.getTime() - date.getTimezoneOffset() * 60000)
    .toISOString()
    .slice(0, 16);
}
const certaintyNames = {
  observed: "实际记录",
  reported: "本人陈述",
  inferred: "推测",
  uncertain: "尚未确认",
};

export function LifecyclePanel({ access }: { access: LifeAccess }) {
  const state = useLifeResource(access, "state"),
    affect = useLifeResource(access, "affect");
  return (
    <div className="life-runtime-grid">
      <section className="panel">
        <h3>此刻</h3>
        <ResourceFeedback {...state} empty={!state.items.length} />
        {state.items.map((row) => (
          <div key={row.actor_id}>
            <p className="life-status-title">
              {row.activity || "暂无已记录活动"}
            </p>
            <p>
              心情：{showMood(row.mood)} · 时区：{row.timezone}
            </p>
            <small>最后更新：{time(row.changed_at)} · 虚构角色生活</small>
          </div>
        ))}
        <button className="button" onClick={state.refresh}>
          刷新状态
        </button>
      </section>
      <section className="panel">
        <h3>短期感受</h3>
        <ResourceFeedback {...affect} empty={!affect.items.length} />
        {affect.items.map((row) => (
          <div key={row.actor_id}>
            <p>情绪倾向 {row.valence.toFixed(2)}</p>
            <ul>
              {row.feelings.map((item) => (
                <li key={item.id}>
                  {item.reason}{" "}
                  <small>强度 {Math.round(item.intensity * 100)}%</small>
                </li>
              ))}
            </ul>
            <small>独立于关系分值 · 记录于 {time(row.observed_at)}</small>
          </div>
        ))}
      </section>
      <ActivityPanel access={access} />
      <ConcernsPanel access={access} />
    </div>
  );
}

function ActivityPanel({ access }: { access: LifeAccess }) {
  const records = useLifeResource(access, "activities"),
    action = useLifeAction(access, records.refresh);
  const draft = useDraftId("activity");
  const [editing, setEditing] = useState<activities_record | null>(null),
    [title, setTitle] = useState(""),
    [note, setNote] = useState("");
  const [state, setState] = useState<activities_record["state"]>("planned"),
    [due, setDue] = useState(""),
    [resume, setResume] = useState("");
  function edit(row: activities_record) {
    setEditing(row);
    setTitle(row.title);
    setNote(typeof row.checkpoint.note === "string" ? row.checkpoint.note : "");
    setState(row.state);
    setDue(localInputTime(row.next_due_at));
    setResume(row.resume_condition || "");
  }
  function clearDraft() {
    setEditing(null);
    setTitle("");
    setNote("");
    setDue("");
    setResume("");
    setState("planned");
  }
  async function save(event: React.FormEvent) {
    event.preventDefault();
    const result = await action.run(
      "activity.save",
      {
        id: editing?.id || draft.id,
        title,
        state,
        checkpoint: {
          ...(editing?.checkpoint ?? { step: 0, position: 0, unit: "step" }),
          note,
        },
        next_due_at: due ? new Date(due).getTime() / 1000 : null,
        resume_condition: resume || null,
        sources: editing?.sources ?? [],
        result_refs: editing?.result_refs ?? [],
        scope: editing?.scope ?? null,
      },
      editing?.version ?? 0,
    );
    if (result && result.result.state !== "unknown") {
      draft.reset();
      clearDraft();
    }
  }
  return (
    <section className="panel">
      <h3>活动与进度</h3>
      <ResourceFeedback {...records} empty={!records.items.length} />
      <div className="life-records">
        {records.items.map((row) => (
          <article className="life-record" key={row.id}>
            <header>
              <strong>{row.title}</strong>
              <span className="status-pill">{showState(row.state)}</span>
            </header>
            <p>
              进度：
              {typeof row.checkpoint.note === "string"
                ? row.checkpoint.note
                : Object.entries(row.checkpoint)
                    .map(([k, v]) => `${k}：${String(v)}`)
                    .join("；") || "尚无"}
            </p>
            {row.resume_condition && <p>恢复条件：{row.resume_condition}</p>}
            <small>
              下一步：{time(row.next_due_at)} · 版本 {row.version}
            </small>
            <div className="life-action-row">
              <button
                className="button"
                disabled={action.busy}
                onClick={() => edit(row)}
              >
                编辑进度
              </button>
              {row.state === "running" && (
                <button
                  className="button"
                  disabled={action.busy}
                  onClick={() =>
                    void action.run(
                      "activity.pause",
                      { id: row.id, reason: "用户暂停" },
                      row.version,
                    )
                  }
                >
                  暂停
                </button>
              )}
              {row.state === "paused" && (
                <button
                  className="button"
                  disabled={action.busy}
                  onClick={() =>
                    void action.run(
                      "activity.resume",
                      { id: row.id },
                      row.version,
                    )
                  }
                >
                  恢复
                </button>
              )}
              {!["completed", "cancelled"].includes(row.state) && (
                <button
                  className="button"
                  disabled={action.busy}
                  onClick={() =>
                    void action.run(
                      "activity.cancel",
                      { id: row.id, reason: "用户结束活动" },
                      row.version,
                    )
                  }
                >
                  结束活动
                </button>
              )}
            </div>
          </article>
        ))}
      </div>
      {records.cursor && (
        <button className="button" onClick={() => void records.more()}>
          继续读取活动
        </button>
      )}
      <form className="life-form" onSubmit={save}>
        <h4>{editing ? "编辑活动" : "安排活动"}</h4>
        <label>
          活动名称
          <input
            required
            maxLength={4000}
            value={title}
            onChange={(e) => setTitle(e.target.value)}
          />
        </label>
        <label>
          当前进度
          <textarea
            value={note}
            onChange={(e) => setNote(e.target.value)}
            maxLength={4000}
          />
        </label>
        <div className="life-form-row">
          <label>
            状态
            <select
              value={state}
              onChange={(e) =>
                setState(e.target.value as activities_record["state"])
              }
            >
              {["planned", "running", "paused", "completed"].map((x) => (
                <option key={x} value={x}>
                  {showState(x)}
                </option>
              ))}
            </select>
          </label>
          <label>
            下一步时间
            <input
              type="datetime-local"
              value={due}
              onChange={(e) => setDue(e.target.value)}
            />
          </label>
        </div>
        <label>
          恢复条件
          <input
            maxLength={1000}
            value={resume}
            onChange={(e) => setResume(e.target.value)}
          />
        </label>
        <div className="life-action-row">
          <button className="button primary" disabled={action.busy}>
            保存活动
          </button>
          {editing && (
            <button type="button" className="button" onClick={clearDraft}>
              取消编辑
            </button>
          )}
        </div>
      </form>
      <RuntimeFeedback {...action} />
    </section>
  );
}

function ConcernsPanel({ access }: { access: LifeAccess }) {
  const records = useLifeResource(access, "open_work"),
    action = useLifeAction(access, records.refresh);
  const draft = useDraftId("concern"),
    fragmentDraft = useDraftId("fragment");
  const [editing, setEditing] = useState<open_work_record | null>(null),
    [title, setTitle] = useState(""),
    [goal, setGoal] = useState(""),
    [fragment, setFragment] = useState("");
  async function save(event: React.FormEvent) {
    event.preventDefault();
    const fragments =
      editing?.fragments.map(({ version, valid, ...item }) => ({
        ...item,
        expected_version: version,
      })) ?? [];
    if (fragment)
      fragments.push({
        id: fragmentDraft.id,
        expected_version: 0,
        text: fragment,
        sources: [],
        expires_at: null,
        certainty: "reported",
      });
    const result = await action.run(
      "concern.save",
      {
        id: editing?.id || draft.id,
        title,
        goal,
        state: editing?.state ?? "open",
        scope: editing?.scope ?? null,
        sources: editing?.sources ?? [],
        fragments,
        next_due_at: editing?.next_due_at ?? null,
        expires_at: editing?.expires_at ?? null,
        result_refs: editing?.result_refs ?? [],
      },
      editing?.version ?? 0,
    );
    if (result && result.result.state !== "unknown") {
      draft.reset();
      fragmentDraft.reset();
      setEditing(null);
      setTitle("");
      setGoal("");
      setFragment("");
    }
  }
  return (
    <section className="panel">
      <h3>近期关注与约定</h3>
      <ResourceFeedback {...records} empty={!records.items.length} />
      {records.items.map((row) => (
        <article key={row.id} className="life-record">
          <header>
            <strong>{row.title}</strong>
            <span className="status-pill">{showState(row.state)}</span>
          </header>
          <p>{row.goal}</p>
          {row.fragments
            .filter((item) => item.valid)
            .map((item) => (
              <p key={item.id}>
                {item.text} <small>· {certaintyNames[item.certainty]}</small>
              </p>
            ))}
          <small>
            {row.intent_valid ? "当前意图有效" : "意图已失效"} · 版本{" "}
            {row.version}
          </small>
          <div className="life-action-row">
            <button
              className="button"
              onClick={() => {
                setEditing(row);
                setTitle(row.title);
                setGoal(row.goal);
                setFragment("");
              }}
            >
              继续记录
            </button>
            {row.state === "open" && (
              <button
                className="button"
                disabled={action.busy}
                onClick={() =>
                  void action.run(
                    "concern.close",
                    { id: row.id, reason: "用户结束关注" },
                    row.version,
                  )
                }
              >
                结束关注
              </button>
            )}
          </div>
        </article>
      ))}
      {records.cursor && (
        <button className="button" onClick={() => void records.more()}>
          继续读取事项
        </button>
      )}
      <form className="life-form" onSubmit={save}>
        <h4>{editing ? "继续记录关注" : "增加关注"}</h4>
        <label>
          事项
          <input
            required
            maxLength={4000}
            value={title}
            onChange={(e) => setTitle(e.target.value)}
          />
        </label>
        <label>
          目标或约定
          <textarea
            required
            maxLength={4000}
            value={goal}
            onChange={(e) => setGoal(e.target.value)}
          />
        </label>
        <label>
          本次进展
          <textarea
            maxLength={4000}
            value={fragment}
            onChange={(e) => setFragment(e.target.value)}
          />
        </label>
        <button className="button primary" disabled={action.busy}>
          保存关注
        </button>
        {editing && (
          <button
            type="button"
            className="button"
            onClick={() => {
              setEditing(null);
              setTitle("");
              setGoal("");
              setFragment("");
            }}
          >
            取消编辑
          </button>
        )}
      </form>
      <RuntimeFeedback {...action} />
    </section>
  );
}
