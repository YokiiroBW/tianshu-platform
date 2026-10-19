import { useEffect, useState } from "react";
import type { Comic, Kind, Media, SearchResult, Source } from "./types";
import { ComicCard } from "./ComicCard";
import { word } from "../wording";

export function ComicSearch({
  media,
  kind,
  onOpen,
}: {
  media: Media;
  kind: Kind;
  onOpen: (comic: Comic) => void;
}) {
  const [query, setQuery] = useState("");
  const [language, setLanguage] = useState("");
  const [source, setSource] = useState("");
  const [sources, setSources] = useState<Source[]>([]);
  const [result, setResult] = useState<SearchResult>();
  const [starting, setStarting] = useState(false);
  const { request } = media;
  useEffect(() => {
    void request<{ sources: Source[] }>(
      "comic-search-sources",
      "comics/sources/list",
      {},
    ).then((r) => {
      if (r) setSources(r.sources);
    });
  }, [request]);
  useEffect(() => {
    if (result?.state !== "running") return;
    const timer = window.setTimeout(() => {
      void request<SearchResult>("comic-search-poll", "comics/search/results", {
        search_id: result.search_id,
      }).then((r) => {
        if (r) setResult(r);
      });
    }, 900);
    return () => window.clearTimeout(timer);
  }, [result, request]);
  const search = async (page = 1) => {
    setStarting(true);
    if (result?.state === "running")
      await request("comic-search-cancel", "comics/search/cancel", {
        search_id: result.search_id,
      });
    const r = await request<SearchResult>(
      "comic-search-start",
      "comics/search/start",
      {
        query,
        language,
        library_kind: kind,
        source_ids: source ? [source] : [],
        page,
      },
    );
    if (r) setResult(r);
    setStarting(false);
  };
  const available = sources.filter(
    (s) =>
      s.enabled &&
      s.available !== false &&
      (s.content_warning === "SAFE") === (kind === "general"),
  );
  return (
    <div className="comic-stack">
      <form
        className="media-card comic-search"
        onSubmit={(e) => {
          e.preventDefault();
          void search();
        }}
      >
        <label>
          作品或作者
          <input
            required
            maxLength={200}
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="输入作品名或作者"
          />
        </label>
        <label>
          语言
          <select
            value={language}
            onChange={(e) => setLanguage(e.target.value)}
          >
            <option value="">全部语言</option>
            {[...new Set(available.map((s) => s.language))].sort().map((v) => (
              <option key={v}>{v}</option>
            ))}
          </select>
        </label>
        <label>
          下载源
          <select value={source} onChange={(e) => setSource(e.target.value)}>
            <option value="">聚合全部启用源</option>
            {available
              .filter(
                (s) =>
                  !language ||
                  s.language === language ||
                  (language === "zh" && s.language.startsWith("zh-")),
              )
              .map((s) => (
                <option key={s.source_id} value={s.source_id}>
                  {s.name}
                </option>
              ))}
          </select>
        </label>
        <button className="button primary" disabled={starting || !query.trim()}>
          搜索
        </button>
      </form>
      {result && (
        <div className="comic-heading">
          <p role="status">
            {result.state === "running"
              ? "正在搜索"
              : result.state === "cancelled"
                ? "搜索已停止"
                : "搜索完成"}{" "}
            · {result.sources.length} / {result.total} 个源 ·{" "}
            {result.comics.length} 个来源版本
          </p>
          {result.state === "running" && (
            <button
              className="button"
              onClick={() => {
                void request<SearchResult>(
                  "comic-search-cancel",
                  "comics/search/cancel",
                  { search_id: result.search_id },
                ).then((r) => {
                  if (r) setResult(r);
                });
              }}
            >
              停止搜索
            </button>
          )}
        </div>
      )}
      {result && (
        <details>
          <summary>各源结果与失败原因</summary>
          <ul>
            {result.sources.map((s) => (
              <li key={s.source_id}>
                {s.name}：{word(s.code)} · {s.count} 个结果
                {s.search_mode === "catalogue" ? " · 按源目录筛选" : ""}
                {s.filtered ? `，排除 ${s.filtered} 个不相关结果` : ""}
              </li>
            ))}
          </ul>
        </details>
      )}
      <div className="comic-grid">
        {result?.comics.map((c) => (
          <ComicCard key={c.comic_id} comic={c} onOpen={() => onOpen(c)} />
        ))}
      </div>
      {!result && (
        <div className="comic-empty">
          <h3>找到想看的漫画</h3>
          <p>同名作品的不同来源会分别保留。先预览，再选择版本下载和追更。</p>
        </div>
      )}
      {result?.state === "completed" && !result.comics.length && (
        <p className="comic-empty">
          没有匹配结果。可调整名称、语言或下载源；具体失败原因见上方源状态。
        </p>
      )}
      {result?.state === "completed" &&
        result.sources.some((s) => s.has_next_page) && (
          <button
            className="button"
            onClick={() => void search(result.page + 1)}
          >
            查看下一页来源结果
          </button>
        )}
    </div>
  );
}
