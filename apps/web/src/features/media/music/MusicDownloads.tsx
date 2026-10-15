import { useEffect, useState } from "react";
import type { MediaController } from "../useMediaController";
import {
  providerNames,
  type AudioQuality,
  type AudioUnlock,
  type AudioAttempt,
  type MusicPlaylist,
  type MusicQuality,
} from "./types";
import { word } from "../wording";

export const musicLevels: Record<string, string> = {
  best: "最佳可用",
  hires: "Hi-Res",
  lossless: "无损",
  exhigh: "极高",
  higher: "较高",
  standard: "标准",
};
export function audioQualityText(quality?: AudioQuality | null) {
  if (!quality) return "尚未核验";
  return `${musicLevels[quality.level] ?? quality.level} · ${quality.codec.toUpperCase()} · ${quality.sample_rate / 1000} kHz${quality.bits_per_sample ? ` / ${quality.bits_per_sample} bit` : ""}${quality.fallback ? "（已降级）" : ""}`;
}
export function unlockText(unlock?: AudioUnlock | null) {
  if (!unlock) return "";
  const labels = {
    not_needed: "普通音频，无需解锁",
    decoding: "正在自动解锁",
    decoded: "已自动解锁",
    key_required: "解锁需要客户端密钥",
    unsupported: "暂不支持此加密版本",
    interrupted: "解锁已中断",
    failed: "解锁失败",
  };
  return `${labels[unlock.state]}${unlock.format ? ` · ${unlock.format.toUpperCase()}` : ""}`;
}
export function MusicDownloads({
  media,
  playlist,
  configured,
  refresh,
}: {
  media: MediaController;
  playlist: MusicPlaylist;
  configured: boolean;
  refresh: () => Promise<void>;
}) {
  const [quality, setQuality] = useState<MusicQuality>(
    playlist.quality ?? { level: "lossless", allow_fallback: true },
  );
  const [automatic, setAutomatic] = useState(playlist.auto_download ?? false);
  const [exporting, setExporting] = useState(false);
  useEffect(() => {
    setQuality(playlist.quality ?? { level: "lossless", allow_fallback: true });
    setAutomatic(playlist.auto_download ?? false);
  }, [playlist.revision]);
  const download = async () => {
    const result = await media.command<{ queued: number; unavailable: number }>(
      "music/playlists/download",
      {
        playlist_id: playlist.playlist_id,
        expected_revision: playlist.revision,
      },
    );
    if (result) {
      media.setNotice(
        `已加入下载队列 ${result.queued} 首，来源暂不可用 ${result.unavailable} 首。已有成品将先核验复用。`,
      );
      await refresh();
    }
  };
  return (
    <div className="media-stack music-downloads">
      <p className="muted">
        {playlist.auto_download ? "同步后自动下载新增歌曲" : "手动下载"} ·{" "}
        {musicLevels[playlist.quality?.level ?? "lossless"]}
      </p>
      <div className="music-actions">
        <button
          className="button"
          disabled={!configured || !!media.busy}
          onClick={() => void download()}
        >
          下载歌单
        </button>
        <a className="button" href="#/subscriptions/2">
          查看下载任务
        </a>
        <button
          className="button"
          disabled={!configured || exporting}
          onClick={async () => {
            setExporting(true);
            const result = await media.request<{
              directory: string;
              exported: number;
              total: number;
            }>("music-export", "music/playlists/export", {
              playlist_id: playlist.playlist_id,
            });
            if (result)
              media.setNotice(
                `已导出 ${result.exported}/${result.total} 首到 ${result.directory}，缺失歌曲保留在 JSON 清单。`,
              );
            setExporting(false);
          }}
        >
          {exporting ? "正在导出…" : "导出 M3U8 / JSON"}
        </button>
      </div>
      {!configured && <p className="muted">保存音乐目录后即可下载。</p>}
      <details>
        <summary>下载设置</summary>
        <form
          className="media-stack"
          onSubmit={async (event) => {
            event.preventDefault();
            if (
              await media.command("music/playlists/downloads", {
                playlist_id: playlist.playlist_id,
                expected_revision: playlist.revision,
                quality,
                auto_download: automatic,
              })
            ) {
              media.setNotice(
                automatic
                  ? "下载设置已保存，歌单已加入队列。"
                  : "下载设置已保存。",
              );
              await refresh();
            }
          }}
        >
          <label>
            期望音质
            <select
              value={quality.level}
              onChange={(event) =>
                setQuality({
                  ...quality,
                  level: event.target.value as MusicQuality["level"],
                })
              }
            >
              {Object.entries(musicLevels).map(([key, name]) => (
                <option value={key} key={key}>
                  {name}
                </option>
              ))}
            </select>
          </label>
          <label className="media-check">
            <input
              type="checkbox"
              checked={quality.allow_fallback}
              onChange={(event) =>
                setQuality({ ...quality, allow_fallback: event.target.checked })
              }
            />
            允许使用当前账号可下载的较低音质
          </label>
          <label className="media-check">
            <input
              type="checkbox"
              checked={automatic}
              onChange={(event) => setAutomatic(event.target.checked)}
            />
            同步后自动下载新增歌曲
          </label>
          <p className="muted">
            自动检查已连接账号的下载权限，并对加密音源解锁、写标签和查重归档。解锁失败时会继续尝试其他可用来源，处理结果保存在任务中。暂停歌单同步后不再自动加入新歌曲，已排队任务可在下载任务中取消。
          </p>
          <button
            className="button"
            disabled={!!media.busy || (automatic && !configured)}
          >
            保存下载设置
          </button>
        </form>
      </details>
    </div>
  );
}

export function MusicAudioReceipt({
  receipt,
  unlock,
  attempts,
}: {
  unlock?: AudioUnlock | null;
  attempts?: AudioAttempt[];
  receipt:
    | {
        files: { path: string; kind: string }[];
        warnings: string[];
        unlock?: AudioUnlock | null;
      }
    | null
    | undefined;
}) {
  return (
    <section className="media-stack">
      <h4>音乐归档</h4>
      {(unlock || receipt?.unlock) && (
        <p>{unlockText(unlock || receipt?.unlock)}</p>
      )}
      {!!attempts?.length && (
        <details>
          <summary>其他来源的处理结果</summary>
          {attempts.map((attempt, index) => (
            <p key={index}>
              {providerNames[attempt.provider]}：{word(attempt.code)}
            </p>
          ))}
        </details>
      )}
      {receipt ? (
        <>
          {receipt.files.map((file) => (
            <p className="music-file" key={file.path}>
              {file.path}
            </p>
          ))}
          {receipt.warnings.map((code) => (
            <p className="media-warning" key={code}>
              {word(code)}
            </p>
          ))}
        </>
      ) : (
        <p className="muted">尚未完成文件归档。</p>
      )}
    </section>
  );
}
