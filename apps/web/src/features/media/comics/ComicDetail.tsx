import { useCallback, useEffect, useState } from "react";
import type { Chapter, Comic, Detail, Kind, Media, Mutate } from "./types";
import { kinds } from "./types";
import { word } from "../wording";

export function ComicDetail({
  comic,
  media,
  mutate,
  onBack,
}: {
  comic: Comic;
  media: Media;
  mutate: Mutate;
  onBack: () => void;
}) {
  const [detail, setDetail] = useState<Detail>();
  const [chosen, setChosen] = useState<string[]>([]);
  const [preview, setPreview] = useState<Chapter>();
  const [kind, setKind] = useState<Kind>(comic.library_kind);
  const [mode, setMode] = useState("all");
  const [latest, setLatest] = useState(5);
  const [scanlator, setScanlator] = useState("");
  const [metadata, setMetadata] = useState(comic.metadata);
  const [editing, setEditing] = useState(false);
  const [retrying, setRetrying] = useState<string>();
  const { request } = media;
  const read = useCallback(
    async (offset = 0, refresh = false, replace = false) => {
      const result = await request<Detail>("comic-detail", "comics/detail", {
        comic_id: comic.comic_id,
        offset,
        refresh,
      });
      if (result)
        setDetail((old) => {
          if (replace && old) {
            const updates = new Map(
              result.chapters.map((chapter) => [chapter.chapter_id, chapter]),
            );
            return {
              ...old,
              comic: result.comic,
              chapters: old.chapters.map(
                (chapter) => updates.get(chapter.chapter_id) ?? chapter,
              ),
            };
          }
          return offset && old
            ? { ...result, chapters: [...old.chapters, ...result.chapters] }
            : result;
        });
    },
    [request, comic.comic_id],
  );
  useEffect(() => {
    void read();
  }, [read]);
  const pending = [
    ...new Set(
      detail?.chapters.flatMap((c, index) =>
        c.job &&
        ![
          "completed",
          "published",
          "cancelled",
          "failed",
          "auth_required",
          "unknown",
        ].includes(c.job.state)
          ? [Math.floor(index / 200) * 200]
          : [],
      ) ?? [],
    ),
  ].join(",");
  useEffect(() => {
    if (!pending) return;
    let reading = false;
    const timer = window.setInterval(async () => {
      if (document.hidden || reading) return;
      reading = true;
      try {
        for (const offset of pending.split(","))
          await read(Number(offset), false, true);
      } finally {
        reading = false;
      }
    }, 5000);
    return () => window.clearInterval(timer);
  }, [pending, read]);
  const retry = async (chapter: Chapter) => {
    if (!chapter.job) return;
    setRetrying(chapter.chapter_id);
    try {
      const result = await media.command("jobs/control", {
        job_id: chapter.job.job_id,
        action: "retry",
      });
      if (result) {
        media.setNotice("已继续原任务，已完成页面和归档回执会复用。");
        const index =
          detail?.chapters.findIndex(
            (item) => item.chapter_id === chapter.chapter_id,
          ) ?? 0;
        await read(Math.floor(Math.max(0, index) / 200) * 200, false, true);
      }
    } finally {
      setRetrying(undefined);
    }
  };
  const selected = detail?.comic ?? comic;
  const available =
    detail?.chapters.filter((c) => !scanlator || c.scanlator === scanlator) ??
    [];
  return (
    <div className="comic-stack">
      <div className="comic-heading">
        <button className="button" onClick={onBack}>
          返回列表
        </button>
        <button className="button" onClick={() => void read(0, true)}>
          刷新章节与阅读状态
        </button>
      </div>
      <article className="media-card">
        <div className="comic-heading">
          <div>
            <h2>{selected.metadata.title}</h2>
            <p>
              {selected.metadata.author || "作者未提供"} ·{" "}
              {selected.source_name} · {selected.metadata.language}
            </p>
          </div>
          <button
            className="button"
            onClick={() => {
              setMetadata(selected.metadata);
              setEditing(!editing);
            }}
          >
            编辑资料
          </button>
        </div>
        <p className="comic-description">
          {selected.metadata.description || "来源暂未提供简介。"}
        </p>
        {editing && (
          <form
            className="comic-stack"
            onSubmit={(e) => {
              e.preventDefault();
              void mutate(
                "metadata/save",
                {
                  comic_id: comic.comic_id,
                  metadata: Object.fromEntries(
                    Object.entries(metadata).filter(([key]) => key !== "tags"),
                  ),
                  expected_revision: selected.revision,
                },
                "漫画资料已保存，下次下载采用这份资料。",
              ).then((ok) => {
                if (ok) {
                  setEditing(false);
                  void read();
                }
              });
            }}
          >
            <div className="comic-field-grid">
              {(["title", "author", "artist", "language"] as const).map(
                (key, index) => (
                  <label key={key}>
                    {["作品名", "作者", "画师", "语言"][index]}
                    <input
                      required={key === "title"}
                      value={metadata[key]}
                      maxLength={key === "language" ? 20 : 500}
                      onChange={(e) =>
                        setMetadata({ ...metadata, [key]: e.target.value })
                      }
                    />
                  </label>
                ),
              )}
              <label>
                阅读方向
                <select
                  value={metadata.direction}
                  onChange={(e) =>
                    setMetadata({
                      ...metadata,
                      direction: e.target.value as typeof metadata.direction,
                    })
                  }
                >
                  <option value="rtl">从右向左</option>
                  <option value="ltr">从左向右</option>
                  <option value="vertical">纵向条漫</option>
                </select>
              </label>
            </div>
            <label>
              简介
              <textarea
                rows={4}
                value={metadata.description}
                maxLength={20000}
                onChange={(e) =>
                  setMetadata({ ...metadata, description: e.target.value })
                }
              />
            </label>
            <button className="button" disabled={!!media.busy}>
              保存资料
            </button>
          </form>
        )}
      </article>
      <div className="media-card comic-stack">
        <div className="comic-controls">
          <label>
            归档分类
            <select
              value={kind}
              onChange={(e) => setKind(e.target.value as Kind)}
            >
              {kinds.map(([key, label]) => (
                <option key={key} value={key}>
                  {label}
                </option>
              ))}
            </select>
          </label>
          {!!detail?.scanlators.length && (
            <label>
              来源版本
              <select
                value={scanlator}
                onChange={(e) => {
                  setScanlator(e.target.value);
                  setChosen([]);
                }}
              >
                <option value="">全部版本分别保留</option>
                {detail.scanlators.map((s) => (
                  <option key={s}>{s}</option>
                ))}
              </select>
            </label>
          )}
        </div>
        {comic.source_type === "remote" && (
          <div className="comic-controls">
            <label>
              首次订阅范围
              <select value={mode} onChange={(e) => setMode(e.target.value)}>
                <option value="all">下载已有章节并追更</option>
                <option value="new">只追更今后的章节</option>
                <option value="latest">下载最近章节并追更</option>
              </select>
            </label>
            {mode === "latest" && (
              <label>
                最近章节数
                <input
                  type="number"
                  min={1}
                  max={500}
                  value={latest}
                  onChange={(e) => setLatest(Number(e.target.value))}
                />
              </label>
            )}
            <button
              className="button primary"
              disabled={!detail || !!media.busy}
              onClick={() =>
                void mutate(
                  "subscriptions/save",
                  {
                    comic_id: comic.comic_id,
                    library_kind: kind,
                    mode,
                    latest_count: latest,
                    scanlator,
                  },
                  "已保存追更设置，选定范围已加入下载队列。",
                )
              }
            >
              订阅追更
            </button>
          </div>
        )}
        <p className="muted">
          每个来源版本独立保存；原图无损封装为 CBZ。完成 AssetLibrary 归档与
          Komga 检查后显示阅读入口。
        </p>
      </div>
      <div className="comic-heading">
        <h3>章节 · {detail?.total ?? "读取中"}</h3>
        <div className="comic-actions">
          <button
            className="button"
            onClick={() =>
              setChosen(
                available
                  .filter((c) => !c.source_missing)
                  .map((c) => c.chapter_id),
              )
            }
          >
            选择当前页
          </button>
          <button className="button" onClick={() => setChosen([])}>
            清空选择
          </button>
          <button
            className="button primary"
            disabled={!chosen.length || !!media.busy}
            onClick={() =>
              void mutate(
                "downloads/enqueue",
                {
                  comic_id: comic.comic_id,
                  chapter_ids: chosen,
                  library_kind: kind,
                },
                `已将 ${chosen.length} 章加入队列。`,
              )
            }
          >
            下载所选 {chosen.length || ""} 章
          </button>
        </div>
      </div>
      <div className="comic-chapters">
        {available.map((c) => (
          <article className="media-card comic-chapter" key={c.chapter_id}>
            <label className="comic-check">
              <input
                type="checkbox"
                disabled={c.source_missing}
                checked={chosen.includes(c.chapter_id)}
                onChange={(e) =>
                  setChosen((old) =>
                    e.target.checked
                      ? [...old, c.chapter_id]
                      : old.filter((id) => id !== c.chapter_id),
                  )
                }
              />
              <span>
                {c.title}
                <small>
                  {c.source_missing ? "来源已移除 · " : ""}
                  {c.scanlator}{" "}
                  {c.artifact
                    ? ` · 已归档 ${c.artifact.page_count} 页`
                    : c.page_count > 0
                      ? ` · ${c.page_count} 页`
                      : ""}
                </small>
              </span>
            </label>
            {c.job && !c.artifact && (
              <div className="comic-job-status" role="status">
                <p>
                  {word(c.job.stage)} · {word(c.job.code)}
                </p>
                <p className="muted">
                  已保留 {c.job.comic_pages_done ?? 0} /{" "}
                  {c.job.comic_pages_total || "待查"} 页
                </p>
                {c.job.can_retry && (
                  <button
                    className="button"
                    disabled={!!retrying || !!media.busy}
                    onClick={() => void retry(c)}
                  >
                    {retrying === c.chapter_id
                      ? "正在继续…"
                      : c.job.stage === "comic_archive"
                        ? "继续归档"
                        : c.job.stage === "comic_reader"
                          ? "重试阅读检查"
                          : "继续此章任务"}
                  </button>
                )}
              </div>
            )}
            <div className="comic-actions">
              <button className="button" onClick={() => setPreview(c)}>
                预览前 3 页
              </button>
              {c.artifact && (
                <a
                  className="button primary"
                  href={c.artifact.komga.reader_url}
                  target="_blank"
                  rel="noreferrer"
                >
                  在 Komga 阅读
                </a>
              )}
            </div>
          </article>
        ))}
      </div>
      {detail?.next_offset != null && (
        <button
          className="button"
          onClick={() => void read(detail.next_offset!)}
        >
          加载更多章节
        </button>
      )}
      {preview && (
        <section className="media-card comic-stack" aria-label="漫画预览">
          <div className="comic-heading">
            <h3>{preview.title} · 页预览</h3>
            <button className="button" onClick={() => setPreview(undefined)}>
              关闭预览
            </button>
          </div>
          <p className="muted">这里只预览开头页面；完整阅读使用已归档版本。</p>
          <div className="comic-preview">
            {Array.from(
              { length: Math.min(3, preview.page_count || 3) },
              (_, index) => (
                <figure key={preview.chapter_id + index}>
                  <img
                    src={`/api/web/media/comics/files/${comic.comic_id}/preview/${preview.chapter_id}/${index}`}
                    alt={`第 ${index + 1} 页`}
                    onError={(e) => {
                      e.currentTarget.alt = `第 ${index + 1} 页暂不可用，请检查来源或完成本地导入后阅读`;
                    }}
                  />
                  <figcaption>第 {index + 1} 页</figcaption>
                </figure>
              ),
            )}
          </div>
        </section>
      )}
    </div>
  );
}
