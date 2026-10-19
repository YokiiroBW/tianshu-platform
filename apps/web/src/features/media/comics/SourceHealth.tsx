import { timestamp, word } from "../wording";
import type { Source, SourceObservation, SourceStage } from "./types";

const stages: [SourceStage, string][] = [
  ["connectivity", "入口探测"],
  ["search", "搜索"],
  ["directory", "章节目录"],
  ["preview", "预览"],
  ["download", "完整下载"],
  ["package", "无损封装"],
  ["archive", "AssetLibrary 归档"],
  ["reader", "Komga 阅读"],
];

export function observations(source: Source) {
  const records = { ...source.health };
  if (!records.search && source.search_checked_at) {
    records.search = {
      code: source.code,
      checked_at: source.search_checked_at,
    };
  }
  if (!records.download && source.download_checked_at) {
    records.download = {
      code: source.download_code ?? "comic_source_unchecked",
      checked_at: source.download_checked_at,
    };
  }
  return records;
}

export function needsAttention(source: Source) {
  return (
    source.available === false ||
    Object.values(observations(source)).some(
      (record) => record && record.code !== "ready",
    )
  );
}

function result(stage: SourceStage, observation: SourceObservation) {
  if (observation.code !== "ready") return word(observation.code);
  if (stage === "connectivity") return "入口响应成功，未验证内容功能";
  if (stage === "search" && observation.count !== undefined) {
    const prefix =
      observation.search_mode === "catalogue" ? "目录筛选" : "搜索响应成功";
    return `${prefix} · ${observation.count} 条匹配${observation.returned !== undefined ? `（返回 ${observation.returned} 条）` : ""}`;
  }
  if (stage === "directory" && observation.count !== undefined) {
    return `已读取 ${observation.count} 章`;
  }
  if (stage === "preview") return "样本图片读取成功，不等同完整下载";
  return `${observation.cached ? "已核对已有回执" : "已验证"}${observation.page_count !== undefined ? ` · ${observation.page_count} 页` : ""}`;
}

export function SourceHealth({ source }: { source: Source }) {
  const records = observations(source);
  return (
    <dl className="comic-source-health" aria-label={`${source.name} 验证记录`}>
      {stages
        .filter(([stage]) => stage !== "connectivity" || records.connectivity)
        .map(([stage, title]) => {
          const record = records[stage];
          return (
            <div key={stage} data-stage={stage}>
              <dt>{title}</dt>
              <dd>
                <span>{record ? result(stage, record) : "尚未验证"}</span>
                {record?.library_kind && (
                  <span>
                    {" "}
                    ·{" "}
                    {record.library_kind === "general"
                      ? "漫画目标"
                      : "私藏目标"}
                  </span>
                )}
                {record?.http_status && (
                  <span> · HTTP {record.http_status}</span>
                )}
                {record && <small>{timestamp(record.checked_at)}</small>}
                {record?.code !== "ready" && record?.last_success_at && (
                  <small>上次成功：{timestamp(record.last_success_at)}</small>
                )}
              </dd>
            </div>
          );
        })}
    </dl>
  );
}
