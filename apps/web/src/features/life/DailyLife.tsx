import { useEffect, useRef, useState } from "react";
import { RefreshCw } from "lucide-react";
import { integrationPost, readFailure } from "../../app/integrationApi";
import { StatePanel } from "../../components/StatePanel";

type Phase = {
  phase_id: string;
  minute: number;
  activity: string;
  detail: string | null;
  state: "planned" | "current" | "elapsed" | "skipped";
  generation_state: string;
};
type Today = {
  actor_id: string;
  day: string;
  timezone: string;
  enabled: boolean;
  observed_at: number;
  plan: {
    plan_id: string;
    version: number;
    state: string;
    generation_state: string;
    generated_by: "baseline" | "gateway";
    entries: Phase[];
    current_phase_id: string | null;
  };
};
type Experience = {
  event_id: string;
  known_id: string;
  position: number;
  kind: string;
  summary: string;
  occurred_at: number;
  learned_at: number;
  via: string;
  phase_id: string | null;
  plan_id: string | null;
  generated_by: "baseline" | "gateway" | "simulation";
};
type Timeline = {
  actor_id: string;
  day: string;
  items: Experience[];
  next_after: { position: number; known_id: string } | null;
};

const phaseText = {
  planned: "尚未发生",
  current: "当前阶段",
  elapsed: "阶段已过",
  skipped: "恢复时跳过",
};
const generationText: Record<string, string> = {
  queued: "等待内容生成",
  generating: "正在生成内容",
  interrupted: "生成中断，等待重试",
  superseded: "安排已更新",
  skipped: "恢复时跳过此阶段",
  completed: "内容已生成",
  unavailable: "模型尚未可用，按基础作息推进",
  failed: "内容生成失败，基础作息仍在推进",
};
function generationLabel(state: string) {
  return generationText[state] ?? `生成状态：${state}`;
}
function clockText(minute: number) {
  return `${String(Math.floor(minute / 60)).padStart(2, "0")}:${String(minute % 60).padStart(2, "0")}`;
}
function timeText(at: number, timezone: string) {
  return Number.isFinite(at)
    ? new Date(at * 1000).toLocaleString("zh-CN", { timeZone: timezone })
    : "时间未确认";
}

/** Persisted Companion projections. A browser read never advances the life clock. */
export function DailyLife({
  actor,
  csrf,
  canRetry,
}: {
  actor: string;
  csrf: string;
  canRetry: boolean;
}) {
  const [today, setToday] = useState<Today | null>(null);
  const [day, setDay] = useState("");
  const [timeline, setTimeline] = useState<Timeline | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const active = useRef<AbortController | null>(null);

  function start() {
    active.current?.abort();
    const controller = new AbortController();
    active.current = controller;
    setBusy(true);
    setError("");
    return controller;
  }
  async function readToday() {
    const controller = start();
    setToday(null);
    setTimeline(null);
    try {
      const next = await integrationPost<Today>(
        "life/today",
        { actor_id: actor },
        csrf,
        controller.signal,
      );
      if (controller.signal.aborted) return;
      setToday(next);
      const selectedDay = day || next.day;
      setDay(selectedDay);
      const events = await integrationPost<Timeline>(
        "life/timeline",
        { actor_id: actor, day: selectedDay, limit: 20, after: null },
        csrf,
        controller.signal,
      );
      if (!controller.signal.aborted) setTimeline(events);
    } catch (cause) {
      if (!controller.signal.aborted) setError(readFailure(cause));
    } finally {
      if (!controller.signal.aborted) setBusy(false);
    }
  }
  async function readTimeline(selectedDay: string, more = false) {
    const after = more ? timeline?.next_after : null;
    if (!selectedDay || (more && !after)) return;
    const controller = start();
    setDay(selectedDay);
    if (!more) setTimeline(null);
    try {
      const next = await integrationPost<Timeline>(
        "life/timeline",
        { actor_id: actor, day: selectedDay, limit: 20, after },
        csrf,
        controller.signal,
      );
      if (!controller.signal.aborted)
        setTimeline((before) => {
          const entries =
            more && before?.day === selectedDay ? before.items : [];
          const found = new Set(entries.map((item) => item.known_id));
          return {
            ...next,
            items: [
              ...entries,
              ...next.items.filter((item) => !found.has(item.known_id)),
            ],
          };
        });
    } catch (cause) {
      if (!controller.signal.aborted) setError(readFailure(cause));
    } finally {
      if (!controller.signal.aborted) setBusy(false);
    }
  }
  async function retry(phaseId: string | null) {
    if (!today) return;
    const controller = start();
    setNotice("");
    try {
      await integrationPost(
        "life/retry",
        {
          actor_id: actor,
          plan_id: today.plan.plan_id,
          phase_id: phaseId,
          expected_version: today.plan.version,
        },
        csrf,
        controller.signal,
      );
      if (controller.signal.aborted) return;
      setNotice("内容生成重试已受理。");
      await readToday();
    } catch (cause) {
      if (!controller.signal.aborted) setError(readFailure(cause));
    } finally {
      if (!controller.signal.aborted) setBusy(false);
    }
  }
  useEffect(() => {
    void readToday();
    return () => active.current?.abort();
  }, [actor, csrf]);

  const current = today?.plan.entries.find(
    (phase) => phase.phase_id === today.plan.current_phase_id,
  );
  useEffect(() => {
    if (
      !today?.enabled ||
      busy ||
      ![today.plan.generation_state, current?.generation_state].some(
        (state) => state === "queued" || state === "generating",
      )
    )
      return;
    const timer = window.setInterval(() => {
      if (!document.hidden) void readToday();
    }, 2000);
    return () => window.clearInterval(timer);
  }, [today, busy]);
  const retryable = (state: string) =>
    canRetry &&
    today?.enabled === true &&
    today.plan.state === "active" &&
    ["failed", "unavailable", "interrupted"].includes(state);
  return (
    <section
      className="panel life-daily"
      aria-label="角色日常"
      aria-busy={busy}
    >
      <div className="section-heading">
        <h2>今日安排与生活经历</h2>
        <button
          className="button"
          disabled={busy}
          onClick={() => void readToday()}
        >
          <RefreshCw aria-hidden="true" /> 刷新日常
        </button>
      </div>
      <p className="muted">
        生活由角色后台独立推进。安排中的未来阶段尚未发生；下方经历只读取已经保存的内容。
      </p>
      {notice && (
        <p className="life-notice" role="status">
          {notice}
        </p>
      )}
      {error && (
        <StatePanel
          kind="error"
          title="日常读取失败"
          action={
            <button
              className="button"
              disabled={busy}
              onClick={() => void readToday()}
            >
              重新读取日常
            </button>
          }
        >
          <p>{error}</p>
        </StatePanel>
      )}
      {!today && busy && (
        <StatePanel kind="loading" title="正在读取日常">
          <p>请稍候。</p>
        </StatePanel>
      )}
      {today && (
        <>
          <p className="muted">
            {today.day} · {today.timezone} ·{" "}
            {today.enabled ? "生活正在运行" : "生活已暂停"} · 安排版本{" "}
            {today.plan.version}
          </p>
          <h3>当前阶段</h3>
          {current ? (
            <div className="life-current">
              <strong>
                {clockText(current.minute)} · {current.activity}
              </strong>
              {current.detail && <p>{current.detail}</p>}
              <p className="muted">
                {generationLabel(current.generation_state)}
              </p>
              {retryable(current.generation_state) && (
                <button
                  className="button"
                  disabled={busy}
                  onClick={() => void retry(current.phase_id)}
                >
                  重试当前阶段内容
                </button>
              )}
            </div>
          ) : (
            <p className="muted">当前还没有已登记阶段。</p>
          )}
          <h3>今日安排</h3>
          <p className="muted">
            {today.plan.generated_by === "gateway"
              ? "当天计划已由模型生成"
              : "当前采用基础作息"}{" "}
            · {generationLabel(today.plan.generation_state)}
          </p>
          {retryable(today.plan.generation_state) && (
            <button
              className="button"
              disabled={busy}
              onClick={() => void retry(null)}
            >
              重试今日安排内容
            </button>
          )}
          <ol className="life-plan">
            {today.plan.entries.map((phase) => (
              <li
                key={phase.phase_id}
                data-current={phase.state === "current" || undefined}
              >
                <strong>
                  {clockText(phase.minute)} · {phase.activity}
                </strong>
                <small>
                  {phaseText[phase.state]} ·{" "}
                  {generationLabel(phase.generation_state)}
                </small>
                {phase.detail && <p>{phase.detail}</p>}
              </li>
            ))}
          </ol>
          <div className="section-heading">
            <h3>已发生经历</h3>
            <label>
              经历日期{" "}
              <input
                type="date"
                value={day}
                max={today.day}
                disabled={busy}
                onChange={(event) => void readTimeline(event.target.value)}
              />
            </label>
          </div>
          {timeline && timeline.items.length === 0 && !error && (
            <StatePanel kind="empty" title="这一天还没有已保存经历">
              <p>安排仍可存在，尚未发生的活动不会写成经历。</p>
            </StatePanel>
          )}
          <ol className="life-timeline">
            {timeline?.items.map((event) => (
              <li key={event.known_id}>
                <p>{event.summary}</p>
                <small>
                  {timeText(event.occurred_at, today.timezone)} ·{" "}
                  {event.generated_by === "gateway"
                    ? "生成的生活内容"
                    : event.generated_by === "simulation"
                      ? "虚构生活事件"
                      : "基础生活记录"}
                </small>
              </li>
            ))}
          </ol>
          {timeline?.next_after && (
            <button
              className="button"
              disabled={busy}
              onClick={() => void readTimeline(day, true)}
            >
              继续读取经历
            </button>
          )}
        </>
      )}
    </section>
  );
}
