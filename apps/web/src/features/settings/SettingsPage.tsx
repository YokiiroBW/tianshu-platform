import { StatePanel } from "../../components/StatePanel";
import { ProviderModelsPanel } from "./ProviderModelsPanel";
import { TasksPanel } from "./TasksPanel";
import { AccessPanel } from "./AccessPanel";
import { ConnectionsPanel } from "./ConnectionsPanel";
import { ExternalConnectionsPanel } from "./ExternalConnectionsPanel";

export default function SettingsPage({ section }: { section: number }) {
  if (section === 0) return <TasksPanel />;
  if (section === 2) return <ProviderModelsPanel />;
  if (section !== 1)
    return (
      <StatePanel
        kind="unconfigured"
        title="任务来源尚未接入"
        action={
          <a className="button" href="#/settings/1">
            查看接入准备
          </a>
        }
      >
        <p>连接后将按任务责任方显示实际进展、阶段与失败原因。</p>
      </StatePanel>
    );
  return (
    <>
      <AccessPanel />
      <ConnectionsPanel />
      <ExternalConnectionsPanel />
    </>
  );
}
