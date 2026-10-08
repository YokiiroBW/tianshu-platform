import { useEffect, useRef, useState, type FormEvent } from "react";
import { ArrowLeft, RefreshCw, X } from "lucide-react";
import { StatePanel } from "../../components/StatePanel";
import { StatusRail } from "../../components/StatusRail";
import type { Job, JobDetail } from "./types";
import type { MediaController } from "./useMediaController";
import { useJobs } from "./useJobs";
import { ReasonNote } from "./ReasonNote";
import { qualityWord, timestamp, tone, word } from "./wording";

const serverWords: Record<string, string> = {
  verified: "已核对",
  unavailable: "暂不可用",
  mismatch: "字段不一致",
  pending: "待核对",
};
const issueWords: Record<string, string> = {
  missing_title: "缺少标题",
  missing_description: "缺少简介",
  missing_cover: "缺少封面",
  title: "需要补全标题",
  description: "需要补全简介",
  cover: "尚未取得封面",
};
const receiptWords: Record<string, string> = {
  state: "发布状态",
  operation_id: "发布操作",
  receipt_id: "发布回执",
  package_id: "成品包",
  library_id: "资产库",
  asset_id: "资产",
  revision: "修订",
  generation: "代次",
  published_at: "发布时间",
  code: "原因",
  indexed: "索引已完成",
  manifest_digest: "成品摘要",
};

function MetadataEditor({
  detail,
  media,
  updated,
}: {
  detail: JobDetail;
  media: MediaController;
  updated: () => void;
}) {
  const [title, setTitle] = useState(detail.metadata.title);
  const [description, setDescription] = useState(detail.metadata.description);
  const [revision] = useState(detail.job.revision);
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    const result = await media.command("jobs/metadata", {
      job_id: detail.job.job_id,
      expected_revision: revision,
      title,
      description,
    });
    if (result) {
      media.setNotice(
        "元数据已保存，任务将从对应阶段继续。源视频的原始资料会保留。",
      );
      updated();
    }
  };
  return (
    <form
      className="media-metadata media-stack"
      onSubmit={(event) => void submit(event)}
    >
      <h4>补全与修正元数据</h4>
      <p className="muted">
        只调整成品展示标题和简介。封面从来源批准的原图获取。
      </p>
      <details>
        <summary>查看来源与当前覆盖值</summary>
        <p>来源标题：{detail.metadata.original_title ?? detail.job.title}</p>
        <p className="media-description">
          来源简介：{detail.metadata.original_description || "来源未提供简介"}
        </p>
        <p className="muted">
          当前标题
          {detail.metadata.title_overridden ? "使用人工覆盖" : "沿用来源"}
          ；当前简介
          {detail.metadata.description_overridden ? "使用人工覆盖" : "沿用来源"}
          。
        </p>
      </details>
      <label>
        展示标题
        <input
          value={title}
          required
          maxLength={512}
          onChange={(event) => setTitle(event.target.value)}
        />
      </label>
      <label>
        展示简介
        <textarea
          value={description}
          rows={5}
          maxLength={65536}
          onChange={(event) => setDescription(event.target.value)}
        />
      </label>
      <p className="muted">
        封面：{detail.metadata.cover_available ? "已取得" : "尚未取得来源封面"}
      </p>
      {!!detail.metadata.issues.length && (
        <p className="media-warning">
          需要处理：
          {detail.metadata.issues
            .map((issue) => issueWords[issue] ?? issue)
            .join("、")}
        </p>
      )}
      <button
        className="button primary"
        disabled={!!media.busy || !title.trim()}
      >
        {media.busy === "jobs/metadata" ? "正在保存…" : "保存元数据"}
      </button>
    </form>
  );
}

export function JobsPanel({ media }: { media: MediaController }) {
  const [selected, setSelected] = useState("");
  const [detail, setDetail] = useState<JobDetail | null>(null);
  const [reading, setReading] = useState(false);
  const [filter, setFilter] = useState("all");
  const [editorKey, setEditorKey] = useState(0);
  const trigger = useRef<HTMLElement | null>(null);
  const panel = useRef<HTMLElement>(null);
  const generation = useRef(0);
  const controller = useRef(media);
  controller.current = media;
  const listing = useJobs(media, filter);
  const jobs = listing.jobs;
  const visible = jobs;
  useEffect(
    () => () => {
      generation.current += 1;
      controller.current.cancel("job-detail");
    },
    [],
  );
  const close = () => {
    generation.current += 1;
    media.cancel("job-detail");
    setSelected("");
    setDetail(null);
    setReading(false);
    requestAnimationFrame(() => trigger.current?.focus());
  };
  const read = async (id: string, focus = true) => {
    generation.current += 1;
    const mine = generation.current;
    setSelected(id);
    setReading(true);
    setDetail(null);
    const result = await media.request<JobDetail>("job-detail", "jobs/detail", {
      job_id: id,
    });
    if (mine !== generation.current) return;
    setReading(false);
    if (result) {
      setDetail(result);
      setEditorKey((key) => key + 1);
    }
    if (focus)
      requestAnimationFrame(() =>
        panel.current?.focus({ preventScroll: true }),
      );
  };
  const control = async (job: Job, action: "cancel" | "retry") => {
    const result = await media.command<{ job: Job }>("jobs/control", {
      job_id: job.job_id,
      action,
    });
    if (result) {
      media.setNotice(
        action === "cancel"
          ? "取消请求已记录。已发布的共享成品会保留，请查看任务最终状态。"
          : "重试请求已受理，后台将从可恢复阶段继续。",
      );
      if (selected === job.job_id) void read(job.job_id, false);
    }
  };
  const selectedSnapshot = jobs.find((job) => job.job_id === selected);
  const serverLabel = (id: string) =>
    media.view?.targets
      .flatMap((target) => target.servers)
      .find((server) => server.server_id === id)?.label ?? id;
  return (
    <section className="media-stack" aria-label="下载任务">
      <div className="media-section-head">
        <div>
          <h2>下载与入库进度</h2>
          <p className="muted">
            下载、资产发布和媒体服务器核对分别记账。刷新失败不会触发重新下载。
          </p>
        </div>
        <div className="media-actions">
          <button
            className="button"
            disabled={listing.loading || listing.loadingMore}
            onClick={() => {
              close();
              listing.reset();
            }}
          >
            重新读取任务列表
          </button>
          <a className="button" href="#/settings/0">
            任务中心
          </a>
        </div>
      </div>
      <label className="media-filter">
        筛选任务
        <select
          value={filter}
          onChange={(event) => {
            close();
            listing.clear();
            setFilter(event.target.value);
          }}
        >
          <option value="all">全部任务</option>
          <option value="running">尚未结束</option>
          <option value="attention">需要处理</option>
          <option value="finished">发布或核对完成</option>
        </select>
      </label>
      {listing.loading ? (
        <StatePanel kind="loading" title="正在读取下载任务">
          <p>正在读取当前筛选的任务记录。</p>
        </StatePanel>
      ) : listing.failed && !visible.length ? (
        <StatePanel
          kind="error"
          title="暂时无法读取下载任务"
          action={
            <button className="button" onClick={() => void listing.refresh()}>
              重新读取任务
            </button>
          }
        >
          <p>读取失败不代表没有任务，请稍后重试。</p>
        </StatePanel>
      ) : !visible.length ? (
        <StatePanel
          kind="empty"
          title={filter !== "all" ? "这个筛选下没有任务" : "还没有下载任务"}
        >
          <p>
            {filter !== "all"
              ? "选择全部任务查看其他状态。"
              : "从链接创建下载任务，或扫描一个订阅来源。"}
          </p>
        </StatePanel>
      ) : (
        <div className={`media-job-layout${selected ? " has-detail" : ""}`}>
          <div className="media-job-list">
            {visible.map((job) => (
              <article
                className={`media-card media-job${selected === job.job_id ? " selected" : ""}`}
                key={job.job_id}
              >
                <div className="media-section-head">
                  <div>
                    <h3>{job.title || job.bvid}</h3>
                    <p className="muted">
                      {job.creator || "UP 主未提供"} · {job.bvid} ·{" "}
                      {job.selected_cids.length} 个分 P
                    </p>
                  </div>
                  <StatusRail tone={tone(job.state)} label={word(job.state)} />
                </div>
                <dl className="media-facts">
                  <div>
                    <dt>阶段</dt>
                    <dd>{word(job.stage)}</dd>
                  </div>
                  <div>
                    <dt>实际画质</dt>
                    <dd>{qualityWord(job.actual_quality)}</dd>
                  </div>
                  <div>
                    <dt>更新</dt>
                    <dd>{timestamp(job.updated_at)}</dd>
                  </div>
                  <div>
                    <dt>目标库</dt>
                    <dd>
                      {media.view?.targets.find(
                        (target) => target.target_id === job.target_id,
                      )?.label ?? job.target_id}
                    </dd>
                  </div>
                </dl>
                {job.progress != null && Number.isFinite(job.progress) ? (
                  <div className="media-progress">
                    <progress
                      value={Math.max(0, Math.min(100, job.progress * 100))}
                      max={100}
                      aria-label={`${job.title} 下载进度`}
                    />
                    <span>{Math.round(job.progress * 100)}%</span>
                  </div>
                ) : job.state === "downloading" ? (
                  <p className="muted">正在下载，上游尚未提供可靠总量。</p>
                ) : null}
                <ReasonNote code={job.code} />
                {job.cancel_requested && (
                  <p role="status" className="muted">
                    取消请求已记录，等待停止确认。
                  </p>
                )}
                <div className="media-actions">
                  <button
                    className="button"
                    aria-expanded={selected === job.job_id}
                    onClick={(event) => {
                      trigger.current = event.currentTarget;
                      void read(job.job_id);
                    }}
                  >
                    查看任务详情
                  </button>
                  {job.can_cancel && (
                    <button
                      className="button"
                      disabled={!!media.busy || job.cancel_requested}
                      onClick={() => void control(job, "cancel")}
                    >
                      取消任务
                    </button>
                  )}
                  {job.can_retry && (
                    <button
                      className="button"
                      disabled={!!media.busy}
                      onClick={() => void control(job, "retry")}
                    >
                      重试任务
                    </button>
                  )}
                </div>
              </article>
            ))}
            {listing.failed && (
              <p className="media-warning" role="status">
                任务列表读取失败，已显示的记录仍是上次快照。
              </p>
            )}
            {listing.hasMore && (
              <button
                className="button"
                disabled={listing.loadingMore}
                onClick={() => void listing.more()}
              >
                {listing.loadingMore
                  ? "正在加载…"
                  : `加载更多任务（已显示 ${visible.length} 条）`}
              </button>
            )}
          </div>
          {selected && (
            <aside
              className="media-card media-job-detail"
              ref={panel}
              tabIndex={-1}
              aria-label="任务详情"
              onKeyDown={(event) => {
                if (event.key === "Escape") {
                  event.preventDefault();
                  close();
                }
              }}
            >
              <div className="media-section-head">
                <h3>任务详情</h3>
                <button className="button" onClick={close}>
                  <ArrowLeft
                    className="media-mobile-return"
                    aria-hidden="true"
                  />
                  <X className="media-desktop-close" aria-hidden="true" />
                  返回任务列表
                </button>
              </div>
              {reading ? (
                <p role="status">正在重新读取这条任务…</p>
              ) : !detail ? (
                <div className="media-stack">
                  <p>未能读取详情，列表中的状态仍是上次快照。</p>
                  <button
                    className="button"
                    onClick={() => void read(selected)}
                  >
                    重新读取详情
                  </button>
                </div>
              ) : (
                <div className="media-stack">
                  <h3>{detail.job.title}</h3>
                  <StatusRail
                    tone={tone(detail.job.state)}
                    label={word(detail.job.state)}
                  />
                  {selectedSnapshot &&
                    selectedSnapshot.updated_at !== detail.job.updated_at && (
                      <p className="media-warning">
                        列表已收到更新。重新读取详情可查看最新事实；未保存的元数据输入会被当前值替换。
                      </p>
                    )}
                  <button
                    className="button"
                    onClick={() => void read(selected, false)}
                  >
                    <RefreshCw aria-hidden="true" />
                    重新读取详情
                  </button>
                  <dl className="media-facts">
                    <div>
                      <dt>任务标识</dt>
                      <dd>{detail.job.job_id}</dd>
                    </div>
                    <div>
                      <dt>下载画质</dt>
                      <dd>{qualityWord(detail.job.actual_quality)}</dd>
                    </div>
                    <div>
                      <dt>当前阶段</dt>
                      <dd>{word(detail.job.stage)}</dd>
                    </div>
                  </dl>
                  <section>
                    <h4>资产发布</h4>
                    {detail.job.asset_receipt ? (
                      <dl className="media-facts">
                        {Object.entries(detail.job.asset_receipt)
                          .filter(([, value]) => typeof value !== "object")
                          .map(([key, value]) => (
                            <div key={key}>
                              <dt>{receiptWords[key] ?? key}</dt>
                              <dd>
                                {typeof value === "boolean"
                                  ? value
                                    ? "是"
                                    : "否"
                                  : String(value ?? "—")}
                              </dd>
                            </div>
                          ))}
                      </dl>
                    ) : (
                      <p className="muted">尚无已确认的资产发布回执。</p>
                    )}
                  </section>
                  <section>
                    <h4>媒体服务器核对</h4>
                    {detail.job.library_results.length ? (
                      detail.job.library_results.map((result) => (
                        <div
                          className="media-server-result"
                          key={result.server_id}
                        >
                          <strong>
                            {serverLabel(result.server_id)} · {result.kind}
                          </strong>
                          <StatusRail
                            tone={
                              result.state === "verified"
                                ? "blue"
                                : result.state === "pending"
                                  ? "yellow"
                                  : "red"
                            }
                            label={serverWords[result.state] ?? result.state}
                          />
                          <ReasonNote code={result.code} />
                          {result.item_id && (
                            <p className="muted">条目 {result.item_id}</p>
                          )}
                        </div>
                      ))
                    ) : (
                      <p className="muted">
                        {media.view?.targets.find(
                          (target) => target.target_id === detail.job.target_id,
                        )?.servers.length
                          ? "配置的服务器尚未返回核对证据。"
                          : "目标库未配置媒体服务器。发布完成不代表服务器核对完成。"}
                      </p>
                    )}
                  </section>
                  {!["completed", "published", "cancelled"].includes(
                    detail.job.state,
                  ) &&
                    detail.metadata && (
                      <MetadataEditor
                        key={editorKey}
                        detail={detail}
                        media={media}
                        updated={() => void read(selected, false)}
                      />
                    )}
                  {[
                    "waiting_metadata",
                    "failed",
                    "published",
                    "completed",
                  ].includes(detail.job.state) && (
                    <button
                      className="button"
                      disabled={!!media.busy}
                      onClick={async () => {
                        const result = await media.command(
                          "jobs/refresh_metadata",
                          {
                            job_id: detail.job.job_id,
                            expected_revision: detail.job.revision,
                          },
                        );
                        if (result) {
                          media.setNotice(
                            "来源已重新解析，请查看最新元数据和封面状态。",
                          );
                          void read(selected, false);
                        }
                      }}
                    >
                      重新解析来源
                    </button>
                  )}
                  <section>
                    <h4>阶段记录</h4>
                    <ol className="media-timeline">
                      {detail.events.map((event, index) => (
                        <li key={`${event.at}-${index}`}>
                          <strong>{word(event.state)}</strong>
                          <time>{timestamp(event.at)}</time>
                          <ReasonNote code={event.code} />
                        </li>
                      ))}
                    </ol>
                  </section>
                </div>
              )}
            </aside>
          )}
        </div>
      )}
    </section>
  );
}
