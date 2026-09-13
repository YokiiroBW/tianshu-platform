import { StatePanel } from "../../components/StatePanel";

// TS-011 owns the scene inside this boundary. No renderer or fabricated room state yet.
export default function RoomPage() {
  return (
    <StatePanel
      kind="unconfigured"
      title="小屋尚未开放"
      action={
        <a className="button" href="#/companion">
          前往陪伴
        </a>
      }
    >
      <p>这里将承载角色的生活空间，以及衣橱、书架和日记入口。</p>
      <p>场景与生活服务尚未接入，当前没有房间状态。</p>
    </StatePanel>
  );
}
