import { useState } from "react";
import type { MediaController } from "../useMediaController";
import { timestamp, word } from "../wording";
import { providerNames, type AudioQuality } from "./types";
import { audioQualityText } from "./MusicDownloads";

type Catalog = {
  offset: number;
  more: boolean;
  counts: Record<string, number>;
  items: {
    recording_id: string;
    metadata: { title: string; artists: string[]; album: string };
    file: {
      download_provider?: keyof typeof providerNames;
      quality: AudioQuality;
      files: { kind: string; path: string }[];
    } | null;
  }[];
};

export function MusicLibrary({
  media,
  status,
  refresh,
}: {
  media: MediaController;
  status?: {
    state: string;
    checked_at?: number;
    total?: number;
    failed?: number;
    queued?: number;
  };
  refresh: () => Promise<void>;
}) {
  const [catalog, setCatalog] = useState<Catalog | null>(null);
  const [loading, setLoading] = useState(false);
  async function read(offset = 0) {
    setLoading(true);
    const result = await media.request<Catalog>(
      "music-library",
      "music/library/view",
      { offset },
    );
    if (result) setCatalog(result);
    setLoading(false);
  }
  return (
    <section className="media-card media-stack" aria-label="统一音乐曲库">
      <div>
        <h3>统一音乐曲库</h3>
        <p className="muted">
          已有 FLAC、MP3、M4A 原位登记，保留原文件。把 NCM、QMC、KGM
          原件放入音乐目录的 .incoming
          文件夹后，会自动尝试解码并归档；原件保留，需要客户端密钥的版本会显示失败原因。
        </p>
      </div>
      <p className="muted">
        扫描状态：
        {status?.state === "not_scanned" || !status
          ? "尚未扫描"
          : word(status.state)}{" "}
        · {timestamp(status?.checked_at)}
        {status?.total != null ? ` · ${status.total} 个文件` : ""}
        {status?.failed ? ` · ${status.failed} 项未能登记` : ""}
      </p>
      <div className="music-actions">
        <button
          className="button"
          disabled={!!media.busy}
          onClick={async () => {
            if (
              await media.request(
                "music-library-scan",
                "music/library/scan",
                {},
              )
            ) {
              media.setNotice(
                "曲库扫描已加入下载队列。既有歌曲会直接登记，导入结果可在任务中查看。",
              );
              await refresh();
            }
          }}
        >
          扫描音乐存档
        </button>
        <button
          className="button"
          disabled={loading}
          onClick={() => void read(catalog?.offset ?? 0)}
        >
          {loading ? "正在读取…" : "查看统一曲库"}
        </button>
      </div>
      {catalog && (
        <>
          <p className="muted">
            {catalog.counts.music_recording ?? 0} 首歌曲 ·{" "}
            {catalog.counts.music_source ?? 0} 个来源 ·{" "}
            {catalog.counts.music_library_file ?? 0} 份已登记音频
          </p>
          <div className="music-track-table">
            <table>
              <thead>
                <tr>
                  <th>歌曲</th>
                  <th>艺人 / 专辑</th>
                  <th>共用文件</th>
                </tr>
              </thead>
              <tbody>
                {catalog.items.map((item) => (
                  <tr key={item.recording_id}>
                    <td>{item.metadata.title}</td>
                    <td>
                      {item.metadata.artists.join("、")}
                      <p className="muted">{item.metadata.album}</p>
                    </td>
                    <td>
                      {item.file ? (
                        <>
                          <p>
                            {
                              providerNames[
                                item.file.download_provider ?? "local"
                              ]
                            }{" "}
                            · {audioQualityText(item.file.quality)}
                          </p>
                          <p className="music-file">
                            {
                              item.file.files.find(
                                (file) => file.kind === "audio",
                              )?.path
                            }
                          </p>
                        </>
                      ) : (
                        "待下载或匹配"
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="music-actions">
            <button
              className="button"
              disabled={loading || catalog.offset === 0}
              onClick={() => void read(Math.max(0, catalog.offset - 100))}
            >
              曲库上一页
            </button>
            <button
              className="button"
              disabled={loading || !catalog.more}
              onClick={() => void read(catalog.offset + 100)}
            >
              曲库下一页
            </button>
          </div>
        </>
      )}
    </section>
  );
}
