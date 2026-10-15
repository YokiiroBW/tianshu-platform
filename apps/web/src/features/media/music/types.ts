export type MusicAccount = {
  account_id: string;
  provider: "netease";
  user_id: string;
  label: string;
  state: "ready" | "auth_required" | "revoked";
  revision: number;
  checked_at: number;
};
export type SourcePlaylist = {
  source_id: string;
  name: string;
  description: string;
  cover_url: string | null;
  track_count: number | null;
  kind: "created" | "collected" | "liked";
};
export type MusicPlaylist = SourcePlaylist & {
  auto_download?: boolean;
  quality?: MusicQuality;
  playlist_id: string;
  account_id: string;
  revision: number;
  member_count: number;
  interval_seconds: number;
  state: "active" | "paused";
  synced_at: number;
  code: string;
};
export type MusicQuality = {
  level: "best" | "hires" | "lossless" | "exhigh" | "higher" | "standard";
  allow_fallback: boolean;
};
export type AudioQuality = {
  level: string;
  codec: string;
  sample_rate: number;
  channels: number;
  bits_per_sample: number;
  bit_rate: number;
  fallback: boolean;
};
export type MusicSettings = {
  directory: string;
  layout: "artist_album" | "album_artist_album";
  filename: "artist_title";
  revision: number;
};
export type MusicView = {
  configured: boolean;
  audio_download_available: boolean;
  accounts: MusicAccount[];
  playlists: MusicPlaylist[];
  settings: MusicSettings;
};
export type PlaylistDetail = {
  playlist: MusicPlaylist;
  tracks: {
    source_id: string;
    position: number;
    title: string;
    artists: string[];
    album: string;
    availability: string;
    audio_state: string;
    audio_code?: string;
    audio_quality?: AudioQuality | null;
    file?: string | null;
    job_id?: string | null;
  }[];
  offset: number;
  more: boolean;
};
