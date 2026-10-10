import {
  ArrowRight,
  CheckCheck,
  Download,
  HardDrive,
  Link,
  ListVideo,
  LoaderCircle,
  Rss,
  TriangleAlert,
  UserRound,
} from "lucide-react";
import { StatusRail } from "../../components/StatusRail";
import { ReasonNote } from "./ReasonNote";
import type { MediaView } from "./types";
import { sourceWords, timestamp, tone, word } from "./wording";
import "./overview.css";

const metrics = [
  {
    key: "published",
    label: "已完成",
    description: "发布或核验完成",
    icon: CheckCheck,
    tone: "blue",
  },
  {
    key: "processing",
    label: "处理中",
    description: "下载、校验与发布中",
    icon: LoaderCircle,
    tone: "blue",
  },
  {
    key: "queued",
    label: "等待队列",
    description: "等待下载或重试",
    icon: ListVideo,
    tone: "yellow",
  },
  {
    key: "attention",
    label: "需要处理",
    description: "失败、未知或等待补全",
    icon: TriangleAlert,
    tone: "red",
  },
] as const;
const shortcuts = [
  {
    label: "视频订阅",
    description: "管理关注来源",
    href: "#/subscriptions/0",
    icon: Rss,
  },
  {
    label: "链接下载",
    description: "解析链接与分 P",
    href: "#/subscriptions/1",
    icon: Link,
  },
  {
    label: "下载中心",
    description: "进度、历史与恢复",
    href: "#/subscriptions/2",
    icon: Download,
  },
  {
    label: "账号与媒体库",
    description: "登录与发布目标",
    href: "#/subscriptions/3",
    icon: UserRound,
  },
];
const number = (value: number) => value.toLocaleString("zh-CN");
function bytes(value: number) {
  const units = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"];
  const index =
    value > 0
      ? Math.min(Math.floor(Math.log(value) / Math.log(1024)), units.length - 1)
      : 0;
  return `${(value / 1024 ** index).toLocaleString("zh-CN", { maximumFractionDigits: 1 })} ${units[index]}`;
}

export function OverviewPanel({ view }: { view: MediaView }) {
  const counts = view.overview?.jobs;
  const countsAvailable =
    counts &&
    [
      counts.total,
      counts.published,
      counts.processing,
      counts.queued,
      counts.attention,
      counts.cancelled,
    ].every((value) => Number.isSafeInteger(value) && value >= 0);
  const storage = view.overview?.storage;
  const storageAvailable =
    storage?.state === "available" &&
    [storage.total_bytes, storage.used_bytes, storage.free_bytes].every(
      (value) =>
        typeof value === "number" && Number.isFinite(value) && value >= 0,
    );
  const subscriptions = view.subscriptions;
  const groups = [
    {
      label: "运行中",
      count: subscriptions.filter((item) => item.state === "active").length,
      tone: "blue",
    },
    {
      label: "已暂停",
      count: subscriptions.filter((item) => item.state === "paused").length,
      tone: "gray",
    },
    {
      label: "需要处理",
      count: subscriptions.filter(
        (item) => item.state === "auth_required" || item.state === "rule_error",
      ).length,
      tone: "red",
    },
  ];
  let offset = 0;
  return (
    <section className="media-overview" aria-label="订阅概览">
      <div className="overview-kpis" aria-label="全部下载任务统计">
        {metrics.map((metric) => (
          <a
            className={`overview-kpi overview-tone-${metric.tone}`}
            href="#/subscriptions/2"
            key={metric.key}
            aria-label={`${metric.label}，${countsAvailable ? number(counts[metric.key]) + " 个任务" : "总量暂不可用"}，打开下载中心`}
          >
            <div className="overview-kpi-label">
              <span className="overview-icon">
                <metric.icon aria-hidden="true" />
              </span>
              <span>{metric.label}</span>
              <ArrowRight className="overview-kpi-arrow" aria-hidden="true" />
            </div>
            <strong className="overview-kpi-value">
              {countsAvailable ? number(counts[metric.key]) : "—"}
            </strong>
            <p>{metric.description}</p>
          </a>
        ))}
      </div>
      {countsAvailable ? (
        <p className="overview-total muted">
          累计 {number(counts.total)} 个任务 · 其中 {number(counts.cancelled)}{" "}
          个已取消
        </p>
      ) : (
        <p className="overview-unavailable muted" role="status">
          任务总量暂不可用。仍可到下载中心查看已读取的任务。
        </p>
      )}
      <div className="overview-columns">
        <div className="overview-main">
          <section
            className="media-card overview-subscriptions"
            aria-label="视频订阅状态"
          >
            <div className="media-section-head">
              <div>
                <h2>视频订阅</h2>
                <p className="muted">
                  已读取 {subscriptions.length} 个订阅来源
                </p>
              </div>
              <a className="overview-text-link" href="#/subscriptions/0">
                管理订阅 <ArrowRight aria-hidden="true" />
              </a>
            </div>
            <div className="overview-subscription-monitor">
              <div className="overview-ring">
                <svg
                  viewBox="0 0 180 180"
                  role="img"
                  aria-label={`订阅状态：${groups.map((group) => `${group.label} ${group.count} 个`).join("，")}`}
                >
                  <circle
                    className="overview-ring-track"
                    cx="90"
                    cy="90"
                    r="70"
                  />
                  {groups.map((group) => {
                    const portion = subscriptions.length
                      ? (group.count / subscriptions.length) * 100
                      : 0;
                    const start = offset;
                    offset += portion;
                    return portion > 0 ? (
                      <circle
                        key={group.label}
                        className={`overview-ring-segment overview-tone-${group.tone}`}
                        cx="90"
                        cy="90"
                        r="70"
                        pathLength="100"
                        strokeDasharray={`${portion} ${100 - portion}`}
                        strokeDashoffset={-start}
                        transform="rotate(-90 90 90)"
                      />
                    ) : null;
                  })}
                </svg>
                <div className="overview-ring-center">
                  <strong>{number(subscriptions.length)}</strong>
                  <span>视频订阅</span>
                </div>
              </div>
              <div className="overview-subscription-legend">
                {groups.map((group) => (
                  <div
                    className={`overview-legend-row overview-tone-${group.tone}`}
                    key={group.label}
                  >
                    <span className="overview-dot" aria-hidden="true" />
                    <span>{group.label}</span>
                    <strong>{number(group.count)}</strong>
                  </div>
                ))}
                <p className="muted">
                  {subscriptions.filter((item) => !item.baseline_ready).length}{" "}
                  个订阅待建立完整成员基线
                </p>
              </div>
            </div>
            {subscriptions.length ? (
              <div className="overview-subscription-cards">
                {subscriptions.slice(0, 3).map((item) => (
                  <a
                    className="overview-subscription-card"
                    href="#/subscriptions/0"
                    key={item.subscription_id}
                  >
                    <div>
                      <h3>{item.label}</h3>
                      <p className="muted">
                        {sourceWords[item.source.kind]} · {item.source.id}
                      </p>
                    </div>
                    <StatusRail
                      tone={tone(item.state)}
                      label={word(item.state)}
                    />
                  </a>
                ))}
              </div>
            ) : (
              <div className="overview-empty">
                <p>还没有视频订阅</p>
                <p className="muted">
                  添加收藏夹、合集、系列或 UP 主来源，持续发现新增视频。
                </p>
                <a className="button" href="#/subscriptions/0">
                  添加视频订阅
                </a>
              </div>
            )}
          </section>
          <section
            className="media-card overview-recent"
            aria-label="近期下载任务"
          >
            <div className="media-section-head">
              <div>
                <h2>近期任务</h2>
                <p className="muted">
                  本次快照中的最近 {Math.min(5, view.jobs.length)} 项
                </p>
              </div>
              <a className="overview-text-link" href="#/subscriptions/2">
                全部下载任务 <ArrowRight aria-hidden="true" />
              </a>
            </div>
            {view.jobs.length ? (
              <div className="overview-recent-list">
                {view.jobs.slice(0, 5).map((job) => (
                  <a
                    className="overview-recent-row"
                    href="#/subscriptions/2"
                    key={job.job_id}
                    aria-label={`在下载中心查看任务：${job.title}`}
                  >
                    <div className="overview-recent-copy">
                      <h3>{job.title}</h3>
                      <p className="muted">
                        {job.creator || "作者资料待补全"} ·{" "}
                        {timestamp(job.updated_at)}
                      </p>
                    </div>
                    <StatusRail
                      tone={tone(job.state)}
                      label={word(job.state)}
                    />
                    <ArrowRight
                      className="overview-row-arrow"
                      aria-hidden="true"
                    />
                  </a>
                ))}
              </div>
            ) : (
              <div className="overview-empty">
                <p>近期列表暂无任务</p>
                <p className="muted">
                  可以解析视频链接创建任务，完整历史请到下载中心查看。
                </p>
                <a className="button" href="#/subscriptions/1">
                  解析视频链接
                </a>
              </div>
            )}
          </section>
        </div>
        <aside className="overview-side" aria-label="下载环境与快捷入口">
          <section
            className="media-card overview-storage"
            aria-label="下载暂存磁盘"
          >
            <div className="media-section-head">
              <h2>
                <HardDrive aria-hidden="true" /> 下载暂存磁盘
              </h2>
              <span className="muted">
                {storageAvailable ? "已读取" : "暂不可用"}
              </span>
            </div>
            {storageAvailable ? (
              <>
                <div className="overview-storage-capacity">
                  <strong>{bytes(storage!.free_bytes!)}</strong>
                  <span className="muted">可用空间</span>
                </div>
                {storage!.total_bytes! > 0 && (
                  <progress
                    aria-label="下载暂存磁盘使用量"
                    value={storage!.used_bytes!}
                    max={storage!.total_bytes!}
                  />
                )}
                <dl className="overview-storage-facts">
                  <div>
                    <dt>已用</dt>
                    <dd>{bytes(storage!.used_bytes!)}</dd>
                  </div>
                  <div>
                    <dt>总容量</dt>
                    <dd>{bytes(storage!.total_bytes!)}</dd>
                  </div>
                </dl>
              </>
            ) : (
              <div className="overview-empty">
                <p>暂未取得磁盘容量</p>
                <p className="muted">恢复读取后显示实际已用和可用空间。</p>
              </div>
            )}
            <p className="overview-note muted">
              平台下载暂存所在磁盘，容量包含同盘其他文件。
            </p>
          </section>
          <section
            className="media-card overview-environment"
            aria-label="引擎、账号与发布目标"
          >
            <h2>下载与发布环境</h2>
            <div className="overview-environment-row">
              <span>下载引擎</span>
              <StatusRail
                tone={tone(view.engine.state)}
                label={word(view.engine.state)}
              />
            </div>
            <ReasonNote code={view.engine.code} />
            <div className="overview-environment-group">
              <h3>B 站账号</h3>
              {view.accounts.length ? (
                view.accounts.map((account) => (
                  <div
                    className="overview-environment-row"
                    key={account.account_id}
                  >
                    <span>{account.label}</span>
                    <StatusRail
                      tone={tone(account.state)}
                      label={word(account.state)}
                    />
                  </div>
                ))
              ) : (
                <p className="muted">尚未登录 B 站，公开视频可直接解析。</p>
              )}
            </div>
            <div className="overview-environment-group">
              <h3>发布目标</h3>
              {view.targets.length ? (
                view.targets.map((target) => (
                  <div
                    className="overview-environment-row"
                    key={target.target_id}
                  >
                    <span>{target.label}</span>
                    <StatusRail
                      tone={tone(target.state)}
                      label={
                        target.state === "ready"
                          ? "配置可用"
                          : word(target.state)
                      }
                    />
                  </div>
                ))
              ) : (
                <p className="media-warning">尚未登记发布目标</p>
              )}
            </div>
            <p className="overview-note muted">
              目标显示平台登记状态；服务器入库结果以任务核对为准。
            </p>
            <a className="overview-text-link" href="#/subscriptions/3">
              管理账号与媒体库 <ArrowRight aria-hidden="true" />
            </a>
          </section>
          <section
            className="media-card overview-shortcuts"
            aria-label="订阅快捷入口"
          >
            <h2>快捷入口</h2>
            <div className="overview-shortcut-grid">
              {shortcuts.map((shortcut) => (
                <a href={shortcut.href} key={shortcut.label}>
                  <shortcut.icon aria-hidden="true" />
                  <strong>{shortcut.label}</strong>
                  <span className="muted">{shortcut.description}</span>
                </a>
              ))}
            </div>
          </section>
        </aside>
      </div>
    </section>
  );
}
