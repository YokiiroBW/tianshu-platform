import ContainerPage from "./ContainerPage";
import NodePage from "./NodePage";
import "./infrastructure.css";

export default function InfrastructurePage({ section }: { section: number }) {
  return section === 1 ? <ContainerPage /> : <NodePage />;
}
