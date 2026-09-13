import { ArrowUpRight, CalendarDays, CircleAlert } from "lucide-react";

export default function WorkbenchPage() {
  return (
    <div className="workbench">
      <section className="panel attention">
        <div className="section-heading">
          <h2>
            <CircleAlert aria-hidden="true" />
            接入前的准备
          </h2>
          <span className="badge">未配置</span>
        </div>
        <p>先连接服务，再让近况、事项与家庭状态汇集到这里。</p>
        <a className="text-link" href="#/settings/1">
          查看连接与准备事项 <ArrowUpRight aria-hidden="true" />
        </a>
      </section>
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
            <p>陪伴服务尚未接入。此刻的活动、心情和安排将在连接后显示。</p>
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
            <p>设备和容器未配置连接，当前无法判断在线或健康状态。</p>
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
          <p className="empty-title">暂无可显示的事项</p>
          <p>任务来源尚未接入；这里不会将未知进展显示为已完成。</p>
        </div>
      </section>
    </div>
  );
}
