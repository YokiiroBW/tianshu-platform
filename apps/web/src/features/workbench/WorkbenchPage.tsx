import { GettingStarted } from "../../app/Auth";
import { ArrowUpRight, CalendarDays } from "lucide-react";

export default function WorkbenchPage() {
  return (
    <div className="workbench">
      <GettingStarted />
      <div className="workspace-columns">
        <section className="panel">
          <div className="section-heading">
            <h2>角色近况</h2>
            <a className="text-link" href="#/companion">
              陪伴 <ArrowUpRight aria-hidden="true" />
            </a>
          </div>
          <div className="quiet-content">
            <p className="empty-title">还没有可读取的角色近况</p>
            <p>此处尚未读取角色近况。对话是否可用，请以上方准备状态为准。</p>
            <a className="text-link" href="#/room">
              查看小屋入口 <ArrowUpRight aria-hidden="true" />
            </a>
          </div>
        </section>
        <section className="panel">
          <div className="section-heading">
            <h2>家庭与服务</h2>
            <span className="badge">状态未知</span>
          </div>
          <div className="quiet-content">
            <p className="empty-title">尚未取得设备观测</p>
            <p>此处尚未取得设备和容器读数，请前往家庭与服务查看实际状态。</p>
            <a className="text-link" href="#/home">
              查看家庭与服务 <ArrowUpRight aria-hidden="true" />
            </a>
          </div>
        </section>
      </div>
      <section className="panel">
        <div className="section-heading">
          <h2>
            <CalendarDays aria-hidden="true" />
            近期事项
          </h2>
          <a className="text-link" href="#/settings">
            查看任务 <ArrowUpRight aria-hidden="true" />
          </a>
        </div>
        <div className="agenda-empty">
          <p className="empty-title">工作台尚未读取任务</p>
          <p>任务中心会显示平台已记录的操作、各来源状态和实际失败原因。</p>
        </div>
      </section>
    </div>
  );
}
