"""Read-only connection summary from actual registrations and observed page reads."""

from datetime import datetime, timezone

from .contracts import require


def view(console, body):
    require(body == {}, "invalid_input", 400)
    p = console.platform
    rows = []

    def add(identifier, state, code, checked_at=None, detail=None):
        rows.append(
            {
                "id": identifier,
                "state": state,
                "code": code,
                "detail": detail,
                "checked_at": checked_at,
            }
        )

    add(
        "dialogue",
        "unverified" if console.dialogue.available() else "not_configured",
        "model_or_core_unverified" if console.dialogue.available() else "core_web_not_connected",
    )
    if p.provider_catalog is None:
        add("providers", "not_configured", "provider_not_configured")
    else:
        catalog = p.provider_catalog.view()
        selected = next(
            (
                item
                for item in catalog["providers"]
                if item["provider_id"] == catalog["default"]["provider_id"]
            ),
            None,
        )
        tested = selected.get("test") if selected else None
        configured = catalog["default"]["configured"]
        add(
            "providers",
            "connected" if configured else "unverified",
            "short_reply_test_succeeded" if configured else "default_not_tested",
            datetime.fromtimestamp(tested["tested_at"], timezone.utc).isoformat()
            if configured and tested and type(tested.get("tested_at")) in (int, float)
            else None,
        )
    persona_code = console.personas.code()
    add(
        "personas",
        "not_configured"
        if persona_code in {"personas_not_configured", "personas_disabled"}
        else "unauthorized"
        if persona_code != "ready"
        else "connected"
        if console.personas.last_success
        else "unverified",
        persona_code
        if persona_code != "ready"
        else "read_observed"
        if console.personas.last_success
        else "read_not_observed",
        console.personas.last_success,
    )

    def add_reader(identifier, reader):
        code = reader.code()
        last = reader.last
        if code.endswith("_not_configured"):
            state = "not_configured"
        elif code.endswith("_required"):
            state = "unauthorized"
        elif code != "ready":
            state = "unavailable"
        elif last is None:
            state = "unverified"
        else:
            state = "connected" if last["code"] == "ok" else "unavailable"
        add(
            identifier,
            state,
            last["code"] if code == "ready" and last else code,
            last["at"] if last else None,
        )

    add_reader("knowledge", console.knowledge)
    add_reader("life", console.life)
    assets_code = console.assets.code()
    assets_ok = next((item for item in console.assets.history() if item["code"] == "ok"), None)
    add(
        "assets",
        "not_configured"
        if assets_code in {"assets_disabled", "no_asset_connections"}
        else "unauthorized"
        if assets_code == "operator_not_authorized"
        else "connected"
        if assets_code == "ready" and assets_ok
        else "unverified",
        assets_code
        if assets_code != "ready"
        else "read_observed"
        if assets_ok
        else "read_not_observed",
        None,
    )
    if console.home.config is None or not console.home.enabled:
        add("home", "not_configured", "home_disabled")
    elif console.home.token is None:
        add("home", "unavailable", "home_credential_missing")
    else:
        home = console.home.view(None)
        observed = next(
            (
                item
                for item in home["entities"]
                if item["observed_at"] is not None and item["availability"] == "available"
            ),
            None,
        )
        add(
            "home",
            "connected" if observed else "unverified",
            "state_observed" if observed else "read_not_observed",
            observed["observed_at"] if observed else None,
        )
    add("tasks", "unverified", "local_ledgers_available")
    add_reader("memory_profiles", console.memory)
    return {"connections": rows}
