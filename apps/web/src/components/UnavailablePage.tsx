import type { Module } from "../app/modules";
import { StatePanel } from "./StatePanel";

export function UnavailablePage({
  module,
  section,
}: {
  module: Module;
  section: number;
}) {
  return (
    <StatePanel
      kind="unconfigured"
      title={`${module.sections[section] ?? module.label}尚未接入`}
      action={
        <a className="button" href="#/settings/1">
          查看接入准备
        </a>
      }
    >
      <p>当前没有可读取的服务数据。接入后，这里将显示实际内容与来源。</p>
      {module.id === "home" && section === 1 && (
        <p>容器运行、健康检查和观测时间将分别展示；当前状态未知。</p>
      )}
    </StatePanel>
  );
}
