import type { useMediaController } from "../useMediaController";

export type Media = ReturnType<typeof useMediaController>;
export type Kind = "general" | "private";
export type Metadata = {
  title: string;
  author: string;
  artist: string;
  description: string;
  language: string;
  direction: "rtl" | "ltr" | "vertical";
  tags: string[];
};
export type Comic = {
  comic_id: string;
  metadata: Metadata;
  source_id: string;
  source_name: string;
  source_type: "remote" | "local";
  library_kind: Kind;
  chapter_count: number;
  downloaded: number;
  revision: number;
  group_key: string;
  has_cover: boolean;
};
export type Chapter = {
  source_missing?: boolean;
  chapter_id: string;
  title: string;
  number: string;
  scanlator: string;
  order: number;
  page_count: number;
  artifact?: {
    page_count: number;
    relative_path: string;
    sha256: string;
    asset_receipt: { entry_id: string; content_version: string };
    komga: { reader_url: string };
  } | null;
};
export type Detail = {
  comic: Comic;
  chapters: Chapter[];
  total: number;
  next_offset: number | null;
  scanlators: string[];
};
export type Source = {
  source_id: string;
  source_key: string;
  name: string;
  language: string;
  content_warning: string;
  enabled: boolean;
  code: string;
  revision: number;
};
export type Connection = {
  ca_pem?: string;
  url: string;
  username: string;
  has_credentials: boolean;
  password?: string;
  token?: string;
};
export type Target = {
  directory: string;
  asset_target_id: string;
  asset_library_id: string;
  komga_library_id: string;
  komga_root: string;
};
export type Settings = {
  revision: number;
  import_directory: string;
  interval_seconds: number;
  connections: Partial<
    Record<"suwayomi" | "komga" | "assetlibrary", Connection>
  >;
  targets: Partial<Record<Kind, Target>>;
};
export type Subscription = {
  comic_id: string;
  comic: Comic;
  state: string;
  revision: number;
  interval_seconds: number;
  next_scan_at: number;
  last_scan_at?: number;
  last_added?: number;
  code: string;
};
export type View = {
  settings: Settings;
  configured: boolean;
  subscriptions: Subscription[];
  counts: {
    comics: number;
    subscriptions: number;
    downloaded_chapters: number;
  };
};
export type SearchResult = {
  search_id: string;
  state: string;
  query: string;
  page: number;
  total: number;
  comics: Comic[];
  sources: {
    source_id: string;
    name: string;
    code: string;
    count: number;
    filtered?: number;
    search_mode?: string;
    has_next_page?: boolean;
  }[];
};
export type Mutate = (
  operation: string,
  body: object,
  notice?: string,
) => Promise<boolean>;
export const kinds: [Kind, string][] = [
  ["general", "漫画"],
  ["private", "私有收藏"],
];
