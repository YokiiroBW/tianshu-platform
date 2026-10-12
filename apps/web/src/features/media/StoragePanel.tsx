import { HardDrive } from "lucide-react";
import type { MediaStorage, StorageVolume } from "./types";

function bytes(value: number) {
  const units = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"];
  const index =
    value > 0
      ? Math.min(Math.floor(Math.log(value) / Math.log(1024)), units.length - 1)
      : 0;
  return `${(value / 1024 ** index).toLocaleString("zh-CN", { maximumFractionDigits: 1 })} ${units[index]}`;
}

function Volume({ volume }: { volume: StorageVolume }) {
  const available =
    volume.state === "available" &&
    [volume.total_bytes, volume.used_bytes, volume.free_bytes].every(
      (value) =>
        typeof value === "number" && Number.isFinite(value) && value >= 0,
    );
  return (
    <section className="overview-storage-volume" aria-label={volume.label}>
      <div className="media-section-head">
        <h3>{volume.label}</h3>
        <span className="muted">{available ? "已读取" : "暂不可用"}</span>
      </div>
      <ul className="overview-storage-directories" aria-label="媒体目录">
        {volume.directories.map((directory) => (
          <li key={directory}>{directory}</li>
        ))}
      </ul>
      {available ? (
        <>
          <div className="overview-storage-capacity">
            <strong>{bytes(volume.free_bytes!)}</strong>
            <span className="muted">可用空间</span>
          </div>
          {volume.total_bytes! > 0 && (
            <progress
              aria-label={`${volume.label} 使用量`}
              value={volume.used_bytes!}
              max={volume.total_bytes!}
            />
          )}
          <dl className="overview-storage-facts">
            <div>
              <dt>已用</dt>
              <dd>{bytes(volume.used_bytes!)}</dd>
            </div>
            <div>
              <dt>总容量</dt>
              <dd>{bytes(volume.total_bytes!)}</dd>
            </div>
          </dl>
        </>
      ) : (
        <p className="muted">暂未取得磁盘容量</p>
      )}
    </section>
  );
}

export function StoragePanel({ storage }: { storage?: MediaStorage }) {
  const volumes = Array.isArray(storage?.volumes) ? storage.volumes : [];
  return (
    <section className="media-card overview-storage" aria-label="存储磁盘">
      <h2>
        <HardDrive aria-hidden="true" /> 存储磁盘
      </h2>
      {volumes.length ? (
        volumes.map((volume) => (
          <Volume key={volume.storage_id} volume={volume} />
        ))
      ) : (
        <div className="overview-empty">
          <p>
            {storage?.state === "not_configured"
              ? "尚未配置媒体存储目录"
              : "暂未取得磁盘容量"}
          </p>
          <p className="muted">
            {storage?.state === "not_configured"
              ? "添加媒体服务器并设置共享目录后显示对应磁盘。"
              : "恢复读取后显示媒体目录所在磁盘的实际容量。"}
          </p>
        </div>
      )}
      <p className="overview-note muted">
        媒体目录所在的存储磁盘，容量包含同盘其他文件。同盘目录合并显示。
      </p>
    </section>
  );
}
