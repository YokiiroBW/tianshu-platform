import {
  createContext,
  useContext,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { useAuth } from "./Auth";
import { request } from "../features/companion/api";
import { readRolePreference, saveRolePreference } from "./rolePreference";

export type WorkspaceRole = { id: string; name: string; state: string };
type Workspace = {
  roles: WorkspaceRole[];
  actor: string;
  select: (id: string) => boolean;
  refresh: () => Promise<void>;
  error: string;
};
const Context = createContext<Workspace | null>(null);
export function RoleWorkspaceProvider({ children }: { children: ReactNode }) {
  const { session } = useAuth();
  const key = `tianshu-workspace-role:${session?.username ?? ""}`;
  const [roles, setRoles] = useState<WorkspaceRole[]>([]);
  const [actor, setActor] = useState("");
  const [error, setError] = useState("");
  const active = useRef<AbortController | null>(null);
  const identity = useRef(session?.csrf);
  identity.current = session?.csrf;
  async function refresh() {
    if (!session?.authenticated) return;
    active.current?.abort();
    const controller = new AbortController();
    active.current = controller;
    const captured = session.csrf;
    try {
      const view = await request<{
        roles: {
          actor_id: string;
          name: string;
          state: string;
          deleted?: boolean;
        }[];
        legacy_roles: { id: string; name: string }[];
      }>("roles/view", controller.signal, {}, session.csrf);
      if (controller.signal.aborted || identity.current !== captured) return;
      const list = [
        ...view.roles
          .filter((r) => !r.deleted)
          .map((r) => ({ id: r.actor_id, name: r.name, state: r.state })),
        ...view.legacy_roles.map((r) => ({ ...r, state: "legacy" })),
      ];
      setRoles(list);
      setError("");
      setActor((current) =>
        list.some((r) => r.id === current)
          ? current
          : (list.find((r) => r.id === readRolePreference(key))?.id ??
            list[0]?.id ??
            ""),
      );
    } catch (cause) {
      if (controller.signal.aborted || identity.current !== captured) return;
      setError(cause instanceof Error ? cause.message : "角色列表读取失败");
      const authorized = [
        ...new Set(session.conversations?.flatMap((item) => item.actors) ?? []),
      ];
      setRoles((before) =>
        authorized.map(
          (id) =>
            before.find((role) => role.id === id) ?? {
              id,
              name: id,
              state: "authorized",
            },
        ),
      );
      setActor((current) =>
        authorized.includes(current)
          ? current
          : (authorized.find((id) => id === readRolePreference(key)) ??
            authorized[0] ??
            ""),
      );
    }
  }
  useEffect(() => {
    setRoles([]);
    setActor("");
    setError("");
    void refresh();
    return () => active.current?.abort();
  }, [session?.csrf]);
  function select(id: string) {
    if (
      id !== actor &&
      !window.dispatchEvent(
        new CustomEvent("tianshu:before-role", {
          cancelable: true,
          detail: { from: actor, to: id },
        }),
      )
    )
      return false;
    setActor(id);
    saveRolePreference(key, id);
    return true;
  }
  return (
    <Context.Provider value={{ roles, actor, select, refresh, error }}>
      {children}
    </Context.Provider>
  );
}
export function useRoleWorkspace() {
  const value = useContext(Context);
  if (!value) throw new Error("role_workspace_missing");
  return value;
}
export function RolePicker() {
  const { roles, actor, select, error } = useRoleWorkspace();
  return (
    <div className="role-picker">
      <label>
        <span>当前角色</span>
        <select
          aria-label="当前角色"
          value={actor}
          onChange={(e) => {
            if (!select(e.target.value)) e.currentTarget.value = actor;
          }}
          disabled={!roles.length}
        >
          {!roles.length && (
            <option value="">{error ? "角色暂不可读" : "还没有角色"}</option>
          )}
          {roles.map((role) => (
            <option key={role.id} value={role.id}>
              {role.name}
            </option>
          ))}
        </select>
      </label>
    </div>
  );
}
