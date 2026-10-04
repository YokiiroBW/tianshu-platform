import { useState } from "react";
import { LifecyclePanel } from "./LifecyclePanel";
import { WardrobePanel } from "./WardrobePanel";
import { ReadingPanel } from "./ReadingPanel";
import { WritingPanel } from "./WritingPanel";
import type { LifeAccess } from "./runtimeApi";
const tabs = [
  { id: "now", label: "活动与关注" },
  { id: "wardrobe", label: "衣柜与相册" },
  { id: "reading", label: "原文与共读" },
  { id: "writing", label: "作品与原稿" },
] as const;
export function LifeWorkspace({ access }: { access: LifeAccess }) {
  const [tab, setTab] = useState<(typeof tabs)[number]["id"]>("now");
  return (
    <section className="life-workspace">
      <div
        className="life-workspace-tabs"
        role="tablist"
        aria-label="角色生活工作区"
      >
        {tabs.map((item) => (
          <button
            className="button"
            role="tab"
            id={`life-tab-${item.id}`}
            aria-controls={`life-panel-${item.id}`}
            aria-selected={tab === item.id}
            key={item.id}
            onClick={() => setTab(item.id)}
          >
            {item.label}
          </button>
        ))}
      </div>
      <div
        role="tabpanel"
        id={`life-panel-${tab}`}
        aria-labelledby={`life-tab-${tab}`}
      >
        {tab === "now" ? (
          <LifecyclePanel access={access} />
        ) : tab === "wardrobe" ? (
          <WardrobePanel access={access} />
        ) : tab === "reading" ? (
          <ReadingPanel access={access} />
        ) : (
          <WritingPanel access={access} />
        )}
      </div>
    </section>
  );
}
