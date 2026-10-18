import { useState } from "react";
import { Drawer } from "../../../components/Drawer";
import { BookOpen, Download, RefreshCw } from "lucide-react";
import type {
  Book,
  BookDetail as Detail,
  ChapterPreview,
  BookChapter,
  BookMetadata,
  MetadataResult,
} from "./types";
import { BookMetadataEditor } from "./BookMetadataEditor";
import { bookFileUrl, bookReadability } from "./presentation";

export function BookDetail({
  book,
  detail,
  preview,
  alternatives,
  busy,
  error,
  onClose,
  onOpen,
  onChapter,
  onMore,
  onDownload,
  onSubscribe,
  onMetadataSearch,
  onMetadataApply,
}: {
  book: Book;
  detail: Detail | null;
  preview: ChapterPreview | null;
  alternatives: Book[];
  busy: boolean;
  error: string;
  onClose: () => void;
  onOpen: (book: Book) => void;
  onChapter: (chapter: BookChapter) => void;
  onMore: () => void;
  onDownload: () => void;
  onSubscribe: () => void;
  onMetadataSearch: (query: string) => Promise<MetadataResult | undefined>;
  onMetadataApply: (
    fields: Partial<BookMetadata>,
    source: string,
  ) => Promise<boolean>;
}) {
  const [tab, setTab] = useState("reading");
  const current = detail?.book ?? book;
  const options = alternatives.some(
    (candidate) => candidate.book_id === current.book_id,
  )
    ? alternatives
    : [current, ...alternatives];
  return (
    <Drawer title={current.metadata.title} open onClose={onClose}>
      <div className="book-detail">
        <p className="muted">
          {current.metadata.author || "作者未提供"} · {current.source_name}
          {current.chapter_count ? ` · ${current.chapter_count} 章` : ""}
        </p>
        <p className="book-fineprint muted">{bookReadability(current)}</p>
        {error && (
          <p className="media-message media-warning" role="alert">
            {error}
          </p>
        )}
        <div className="book-detail-toolbar">
          <label>
            预览来源
            <select
              value={book.book_id}
              onChange={(event) => {
                const selected = options.find(
                  (item) => item.book_id === event.target.value,
                );
                if (selected) onOpen(selected);
              }}
            >
              {options.map((candidate) => (
                <option key={candidate.book_id} value={candidate.book_id}>
                  {candidate.source_name}
                  {candidate.artifact ? " · 已在书库" : ""}
                </option>
              ))}
            </select>
          </label>
          <div className="book-tabs" role="tablist" aria-label="书籍详情">
            <button
              role="tab"
              aria-selected={tab === "reading"}
              onClick={() => setTab("reading")}
            >
              <BookOpen aria-hidden="true" />
              正文预览
            </button>
            <button
              role="tab"
              aria-selected={tab === "metadata"}
              onClick={() => setTab("metadata")}
            >
              书籍资料
            </button>
          </div>
        </div>
        {tab === "reading" ? (
          <div className="book-reader">
            <nav aria-label="章节目录">
              <h3>章节目录</h3>
              {detail ? (
                <>
                  {detail.chapters.map((chapter) => (
                    <button
                      key={chapter.chapter_id}
                      className={
                        preview?.chapter.chapter_id === chapter.chapter_id
                          ? "is-selected"
                          : ""
                      }
                      onClick={() => onChapter(chapter)}
                    >
                      {chapter.title}
                    </button>
                  ))}
                  {detail.chapters.length < detail.total && (
                    <button onClick={onMore}>
                      加载更多目录（{detail.chapters.length}/{detail.total}）
                    </button>
                  )}
                </>
              ) : (
                <p className="muted" role="status">
                  正在读取目录…
                </p>
              )}
            </nav>
            <article className="book-prose">
              {preview ? (
                <>
                  <h3>{preview.chapter.title}</h3>
                  <div>{preview.chapter.content}</div>
                  <p className="book-fineprint muted">
                    {preview.cached
                      ? "使用已经保存的正文"
                      : "已从来源取得并保存正文"}
                  </p>
                </>
              ) : (
                <div className="book-reader-empty" role="status">
                  {error
                    ? "本次试读未取得正文，可以选择其他章节或切换来源。"
                    : detail
                      ? "选择一个章节开始试读"
                      : "正在读取书籍详情…"}
                </div>
              )}
            </article>
          </div>
        ) : (
          <BookMetadataEditor
            key={current.book_id}
            book={current}
            busy={busy}
            onSearch={onMetadataSearch}
            onApply={onMetadataApply}
          />
        )}
        <div className="book-detail-footer">
          <button className="button" onClick={() => onOpen(current)}>
            <RefreshCw aria-hidden="true" />
            重新读取
          </button>
          <div className="book-controls">
            {current.artifact && (
              <a
                className="button"
                href={bookFileUrl(current, "epub")}
                download
              >
                <Download aria-hidden="true" />
                下载 EPUB
              </a>
            )}
            <button className="button" disabled={busy} onClick={onDownload}>
              仅下载一次
            </button>
            <button
              className="button primary"
              disabled={busy}
              onClick={onSubscribe}
            >
              订阅并下载
            </button>
          </div>
        </div>
      </div>
    </Drawer>
  );
}
