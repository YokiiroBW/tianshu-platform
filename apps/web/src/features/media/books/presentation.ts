import { bookWords } from "./wording";
import type { Book } from "./types";

export function groupedBooks(books: Book[]) {
  const groups = new Map<string, Book[]>();
  for (const book of books) {
    const group = groups.get(book.group_key) ?? [];
    if (!group.some((item) => item.book_id === book.book_id)) group.push(book);
    groups.set(book.group_key, group);
  }
  return [...groups.values()];
}

export function bookBytes(bytes: number) {
  return bytes < 1024 * 1024
    ? `${Math.ceil(bytes / 1024)} KB`
    : `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

export function bookCode(code: string) {
  return code === "ready" ? "可用" : (bookWords[code] ?? code);
}

export function bookFeedCode(code: string) {
  return code === "ready" ? "规则已同步" : bookCode(code);
}

export function bookFileUrl(book: Book, kind: "epub" | "cover") {
  return `/api/web/media/books/files/${encodeURIComponent(book.book_id)}/${kind}?v=${book.metadata_revision}`;
}
