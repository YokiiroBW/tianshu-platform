import { lazy } from "react";
import {
  BookOpen,
  Boxes,
  Folder,
  House,
  LayoutDashboard,
  MessageCircle,
  Network,
  Settings,
} from "lucide-react";

// Local navigation metadata, never a cross-product API or a connection-health registry.
export const modules = [
  {
    id: "workbench",
    label: "工作台",
    icon: LayoutDashboard,
    group: "个人空间",
    description: "关注事项与近期安排",
    sections: [],
  },
  {
    id: "companion",
    label: "陪伴",
    icon: MessageCircle,
    group: "个人空间",
    description: "文字对话与角色近况",
    sections: ["文字对话", "生活与日记", "人格与世界", "角色管理"],
  },
  {
    id: "room",
    label: "小屋",
    icon: House,
    group: "个人空间",
    description: "小屋环境预览",
    sections: [],
  },
  {
    id: "memory",
    label: "记忆",
    icon: Network,
    group: "个人空间",
    description: "人物与共同经历",
    sections: ["记忆概览", "人物与群画像", "账号关联", "本人记忆"],
  },
  {
    id: "resources",
    label: "资料与资源",
    icon: BookOpen,
    group: "工作空间",
    description: "研究资料、资产与归档",
    sections: ["研究资料", "资产库", "订阅与下载"],
  },
  {
    id: "home",
    label: "家庭与服务",
    icon: Boxes,
    group: "工作空间",
    description: "设备与容器的实际状态",
    sections: ["家庭设备", "容器", "节点与游戏服", "身体与活动"],
  },
  {
    id: "projects",
    label: "项目",
    icon: Folder,
    group: "工作空间",
    description: "任务、文档与交接",
    sections: ["项目工作区", "经验与交接"],
  },
  {
    id: "settings",
    label: "任务与设置",
    icon: Settings,
    group: "管理",
    description: "任务记录与各项连接设置",
    sections: [
      "任务记录",
      "连接总览",
      "模型供应商",
      "机器人接入",
      "资产库接入",
      "家庭设备接入",
      "访问与域名",
    ],
  },
] as const;

export type Module = (typeof modules)[number];
export type ModuleId = Module["id"];

// Room remains a separate chunk. Future modules register one lazy page here.
export const pages = {
  companion: lazy(() => import("../features/companion/CompanionPage")),
  personas: lazy(() => import("../features/companion/PersonaPanel")),
  roles: lazy(() => import("../features/companion/RoleManager")),
  workbench: lazy(() => import("../features/workbench/WorkbenchPage")),
  room: lazy(() => import("../features/room/RoomPage")),
  settings: lazy(() => import("../features/settings/SettingsPage")),
  home: lazy(() => import("../features/home/HomePage")),
  resources: lazy(() => import("../features/resources/ResourcesPage")),
  knowledge: lazy(() => import("../features/knowledge/KnowledgePage")),
  experience: lazy(() => import("../features/projects/ExperiencePage")),
  life: lazy(() => import("../features/life/LifePage")),
  memory: lazy(() => import("../features/memory/MemoryPage")),
};

export function resolveRoute(hash: string) {
  const path = hash.replace(/^#\/?/, "");
  const [id = "", section, ...rest] = path.split("/");
  const module = modules.find((item) => item.id === (id || "workbench"));
  if (!module || rest.length > 0) return null;
  if (
    section !== undefined &&
    (!/^\d+$/.test(section) || Number(section) >= module.sections.length)
  )
    return null;
  return { module, section: section === undefined ? 0 : Number(section) };
}
