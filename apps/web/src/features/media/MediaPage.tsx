import { useRef, useState, type KeyboardEvent } from "react";
import { Download, ListVideo, RefreshCw, Rss, UserRound } from "lucide-react";
import { StatePanel } from "../../components/StatePanel";
import { StatusRail } from "../../components/StatusRail";
import { AccountsPanel } from "./AccountsPanel";
import { JobsPanel } from "./JobsPanel";
import { LinkPanel } from "./LinkPanel";
import { SubscriptionsPanel } from "./SubscriptionsPanel";
import { useMediaController } from "./useMediaController";
import { tone, word } from "./wording";
import "./media.css";

const tabs = [
  { label: "链接下载", icon: Download },
  { label: "订阅", icon: Rss },
  { label: "下载任务", icon: ListVideo },
  { label: "账号与媒体库", icon: UserRound },
];
export default function MediaPage() {
  const media = useMediaController();
  const [tab, setTab] = useState(0);
  const buttons = useRef<(HTMLButtonElement | null)[]>([]);
  const change = (next: number, focus = false) => {
    media.clearError();
    setTab(next);
    if (focus) buttons.current[next]?.focus();
  };
  const keys = (event: KeyboardEvent, index: number) => {
    const next =
      event.key === "ArrowRight"
        ? (index + 1) % tabs.length
        : event.key === "ArrowLeft"
          ? (index + tabs.length - 1) % tabs.length
          : event.key === "Home"
            ? 0
            : event.key === "End"
              ? tabs.length - 1
              : null;
    if (next !== null) {
      event.preventDefault();
      change(next, true);
    }
  };
  return (
    <div className="media-page">
      <div className="media-header">
        <div>
          <h2>订阅与下载</h2>
          <p className="muted">发现视频，整理成品，查看每一步的实际进度。</p>
        </div>
        <div className="media-actions">
          {media.view && (
            <StatusRail
              tone={tone(media.view.engine.state)}
              label={`下载引擎 · ${word(media.view.engine.state)}`}
            />
          )}
          <button
            className="button"
            disabled={media.loading || !!media.busy}
            onClick={() => {
              media.clearError();
              void media.refresh();
            }}
          >
            <RefreshCw aria-hidden="true" />
            读取状态
          </button>
        </div>
      </div>
      {media.error && (
        <div className="media-message media-warning" role="alert">
          {media.error}
        </div>
      )}
      {media.notice && (
        <div className="media-message" role="status">
          {media.notice}
        </div>
      )}
      {media.stale && media.view && (
        <p className="media-warning" role="status">
          下面仍是上次成功读取的快照。连接恢复后会自动更新。
        </p>
      )}
      {media.loading && !media.view ? (
        <StatePanel kind="loading" title="正在读取视频订阅状态">
          <p>正在确认账号、目标库、订阅和下载任务。</p>
        </StatePanel>
      ) : !media.view ? (
        <StatePanel kind="error" title="暂时无法读取视频订阅">
          <p>重新读取后继续，后台既有任务不会因关闭页面而取消。</p>
        </StatePanel>
      ) : !media.view.configured ? (
        <StatePanel kind="unconfigured" title="视频订阅服务尚未配置">
          <p>此部署还没有登记视频账号、下载引擎和发布目标。</p>
        </StatePanel>
      ) : (
        <>
          <div
            className="media-tabs"
            role="tablist"
            aria-label="订阅与下载功能"
          >
            {tabs.map((item, index) => (
              <button
                key={item.label}
                ref={(element) => {
                  buttons.current[index] = element;
                }}
                id={`media-tab-${index}`}
                role="tab"
                type="button"
                aria-selected={tab === index}
                aria-controls={`media-panel-${index}`}
                tabIndex={tab === index ? 0 : -1}
                onKeyDown={(event) => keys(event, index)}
                onClick={() => change(index)}
              >
                <item.icon aria-hidden="true" />
                {item.label}
                {index === 2 && media.view!.jobs.length > 0 && (
                  <span className="media-count">
                    {media.view!.jobs.length >= 100
                      ? "100+"
                      : media.view!.jobs.length}
                  </span>
                )}
              </button>
            ))}
          </div>
          <div
            id={`media-panel-${tab}`}
            role="tabpanel"
            aria-labelledby={`media-tab-${tab}`}
            className="media-tab-panel"
          >
            {tab === 0 ? (
              <LinkPanel media={media} onJobs={() => change(2, true)} />
            ) : tab === 1 ? (
              <SubscriptionsPanel media={media} />
            ) : tab === 2 ? (
              <JobsPanel media={media} />
            ) : (
              <AccountsPanel media={media} />
            )}
          </div>
        </>
      )}
    </div>
  );
}
