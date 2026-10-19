"""Small public projections; node secrets, execution payloads and raw output stay private."""

from ..contracts import canonical


def maintenance(protection=None):
    protection = protection or {}
    return {
        "required": bool(protection.get("protected")),
        "owner": protection.get("owner"),
        "reason_codes": [protection["reason"]] if protection.get("reason") else [],
    }


def actions(names, available, protection, *, fresh=True, prefix="stack"):
    result = {}
    for name in names:
        remote = "deploy" if name == "configure" else name
        allowed = remote in available or prefix + "." + remote in available
        code = (
            "infrastructure_maintenance_required"
            if protection.get("protected")
            else "infrastructure_observation_stale"
            if not fresh
            else "infrastructure_action_unavailable"
        )
        result[name] = {
            "enabled": bool(allowed and fresh and not protection.get("protected")),
            "reason_codes": [] if allowed and fresh and not protection.get("protected") else [code],
        }
    return result


def health(value):
    return {"not_configured": "none"}.get(value, value)


def runtime(value):
    return value


def stack(value, *, fresh=True):
    return {
        "stack_id": value["id"],
        "project_name": value["project_name"],
        "display_name": value["name"],
        "revision": value["revision"],
        "ownership": value["owner"],
        "exists": value["compose_file"] is not None,
        "runtime_state": runtime(value["runtime"]["state"]),
        "health_state": health(value["runtime"]["health"]),
        "containers": [item["id"] for item in value["runtime"]["containers"]],
        "maintenance": maintenance(value["protection"]),
        "allowed_actions": actions(
            ("start", "stop", "restart", "update", "configure", "restore"),
            value["allowed_actions"],
            value["protection"],
            fresh=fresh,
        ),
    }


def container(value, stack_id=None, *, fresh=True):
    return {
        "container_id": value["id"],
        "stack_id": stack_id,
        "maintenance": maintenance(value["protection"]),
        "allowed_actions": actions(
            ("start", "stop", "restart"),
            value["allowed_actions"],
            value["protection"],
            fresh=fresh,
            prefix="container",
        ),
    }


def plan(value, node_id, revision, protection=None):
    def display(rows):
        return [canonical(row)[:2048] if not isinstance(row, str) else row[:2048] for row in rows]

    return {
        "plan_id": value["plan_id"],
        "node_id": node_id,
        "connection_revision": revision,
        "stack_id": value["stack_id"],
        "project_name": value["project_name"],
        "action": "configure" if value["action"] == "deploy" else value["action"],
        "expected_revision": value["base_revision"],
        "expires_at": value["expires_at"],
        "executable": value["state"] == "ready" and not value["blockers"],
        "changes": ["stack." + value["action"]],
        "diff": value["config_diff"],
        "images": display(value["images"]),
        "ports": display(value["ports"]),
        "mounts": display(value["mounts"]),
        "warnings": value["warnings"],
        "blockers": value["blockers"],
        "maintenance": maintenance(protection),
    }


def version(value, revision, *, can_restore):
    return {
        "version_id": value["version_id"],
        "created_at": value["created_at"],
        "label": value["version_id"],
        "current": False,
        "restorable": can_restore,
        "reason_codes": [] if can_restore else ["infrastructure_action_unavailable"],
    }


def receipt(value):
    command, state = value["command"], value["state"]
    active = value["execution_active"]
    result = {
        "runtime_state": runtime(value["runtime"]["state"]),
        "health_state": health(value["runtime"]["health"]),
        "containers": [
            {"container_id": row["id"], "state": row["state"], "health": row["health"]}
            for row in value["runtime"]["containers"]
        ],
        "version_id": value["version_id"],
        "node_operation_id": value["operation_id"],
        "command_state": command["state"],
        "execution_active": active,
    }
    public_state = {
        "accepted": "running",
        "running": "running",
        "succeeded": "observed",
        "failed": "failed",
        "unknown": "unknown",
        "rejected": "failed",
    }[state]
    if state == "succeeded" and command["state"] != "succeeded":
        public_state = "unknown"
    if public_state == "observed" and (
        value["runtime"]["state"] in {"partial", "unknown"}
        or value["runtime"]["health"] in {"unhealthy", "starting", "mixed", "unknown"}
    ):
        public_state = "partial"
    acceptance = (
        "rejected" if state == "rejected" else "unknown" if state == "unknown" else "submitted"
    )
    return {
        "state": public_state,
        "stage": value["phase"],
        "acceptance": acceptance,
        "settled": active is False and state not in {"accepted", "running"},
        "reason_codes": sorted(set(value["reason_codes"] + command["reason_codes"]))[:100],
        "can_refresh": active is not False or state in {"accepted", "running", "unknown"},
        "result": result,
    }
