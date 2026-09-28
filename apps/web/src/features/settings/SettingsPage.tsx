import { ProviderModelsPanel } from "./ProviderModelsPanel";
import { TasksPanel } from "./TasksPanel";
import { AccessPanel } from "./AccessPanel";
import { ConnectionsPanel } from "./ConnectionsPanel";
import { ExternalConnectionsPanel } from "./ExternalConnectionsPanel";
import { BotConnectionsPanel } from "./BotConnectionsPanel";
import "./settings.css";

const setupLinks = [
  {
    href: "#/settings/2",
    title: "模型供应商",
    detail: "配置模型与用量，检查回复能力。",
  },
  {
    href: "#/settings/3",
    title: "机器人接入",
    detail: "查看接入步骤，管理机器人连接。",
  },
  {
    href: "#/settings/4",
    title: "资产库接入",
    detail: "管理 AssetLink 地址与凭据，执行只读检测。",
  },
  {
    href: "#/settings/5",
    title: "家庭设备接入",
    detail: "管理 Home Assistant 地址、实体与凭据。",
  },
  {
    href: "#/settings/6",
    title: "访问与域名",
    detail: "设置网页访问地址与 HTTPS。",
  },
];

function ConnectionsOverview() {
  return (
    <div className="settings-overview">
      <section
        className="panel settings-directory"
        aria-labelledby="settings-directory-title"
      >
        <div className="section-heading">
          <h2 id="settings-directory-title">连接设置入口</h2>
        </div>
        <p>
          按要配置的服务进入独立页面。下方功能连接状态来自部署配置与真实读取记录。
        </p>
        <div className="settings-directory-grid">
          {setupLinks.map((link) => (
            <a
              className="settings-directory-link"
              href={link.href}
              key={link.href}
            >
              <strong>{link.title}</strong>
              <span>{link.detail}</span>
            </a>
          ))}
        </div>
      </section>
      <ConnectionsPanel />
    </div>
  );
}

export default function SettingsPage({ section }: { section: number }) {
  if (section === 0) return <TasksPanel />;
  if (section === 1) return <ConnectionsOverview />;
  if (section === 2) return <ProviderModelsPanel />;
  if (section === 3) return <BotConnectionsPanel />;
  if (section === 4)
    return <ExternalConnectionsPanel key="assets" kind="assets" />;
  if (section === 5) return <ExternalConnectionsPanel key="home" kind="home" />;
  return <AccessPanel />;
}
