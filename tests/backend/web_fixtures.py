"""Only synthetic web identities; never a product default account."""

from pathlib import Path

from fixtures import DOCUMENTS, NATIVE_PROVIDER
from services.platform.web_console import password_hash
from source_fixtures import source_settings

PASSWORD = "synthetic-local-password-014"
CHAT_PROVIDER = DOCUMENTS["config"]["providers"][0]["provider_id"]


def model_templates():
    """Reviewed server-side templates; the browser only ever sends these ids."""
    return {
        "enabled": True,
        "unlock_ttl_seconds": 900,
        "templates": [
            {
                "template_id": "chat-local-text",
                "label": "本地合成 · Chat 文本",
                "target": "chat",
                "provider_id": CHAT_PROVIDER,
                "verified_capabilities": ["text"],
                "lifetime_seconds": 300,
            },
            {
                "template_id": "native-local-text",
                "label": "本地合成 · 原生 Responses",
                "target": "native",
                "provider_id": NATIVE_PROVIDER,
                "verified_capabilities": ["text", "stream"],
                "lifetime_seconds": 300,
            },
        ],
    }


def web_settings(directory, origin="http://127.0.0.1:4814", static=None):
    c = source_settings(directory, "self_private")
    account = c["input_entries"]["input-entry"]["account"]
    c["principals"]["admin"]["account"] = account
    c["entries"]["config-entry"]["account"] = account
    c["principals"]["admin"]["actions"] += ["source.register", "source.dispatch", "mapping.prepare"]
    c["principals"]["companion"]["actions"] += ["dialogue.send"]
    c["input_entries"]["input-entry"]["owner"] = "admin"
    for name in ("actor-a", "actor-b"):
        c["entries"][name].update(owner="admin", kind="local_operator")
        c["entries"][name]["routes"].append(
            {"caller": "companion", "receiver": "platform", "purpose": "dialogue"}
        )
    if static is None:
        static = Path(directory) / "static"
        static.mkdir()
        (static / "index.html").write_text(
            "<!doctype html><title>Synthetic local web fixture</title>"
        )
    c["web"] = {
        "origin": origin,
        "username": "synthetic-admin",
        "password_hash": password_hash(PASSWORD),
        "principal": "admin",
        "input_entries": ["input-entry"],
        "static_directory": str(Path(static).resolve()),
    }
    c["web_models"] = model_templates()
    return c


def home_section(base_url, **override):
    """One reviewed HA registration: address, credential variable, entities, action templates."""
    section = {
        "enabled": True,
        "base_url": base_url,
        "reviewed_addresses": ["127.0.0.1"],
        "allow_private_http": True,
        "token_env": "TS017_HA_TOKEN",
        "unlock_ttl_seconds": 900,
        "timeout_seconds": 2,
        "status_max_age_seconds": 120,
        "entities": [
            {"entity_id": "light.study", "label": "书房灯", "kind": "light"},
            {"entity_id": "switch.kettle", "label": "热水壶", "kind": "switch"},
            {
                "entity_id": "sensor.living_temperature",
                "label": "客厅温度",
                "kind": "sensor",
                "unit": "°C",
            },
        ],
        "templates": [
            {
                "template_id": "study-light-on",
                "label": "打开书房灯",
                "entity_id": "light.study",
                "service": "turn_on",
            },
            {
                "template_id": "study-light-off",
                "label": "关闭书房灯",
                "entity_id": "light.study",
                "service": "turn_off",
            },
            {
                "template_id": "kettle-on",
                "label": "打开热水壶",
                "entity_id": "switch.kettle",
                "service": "turn_on",
            },
        ],
    }
    section.update(override)
    return section


def home_settings(directory, base_url, origin="http://127.0.0.1:4814", static=None, **override):
    """Console settings plus the synthetic HA connector; the operator may control devices."""
    c = web_settings(directory, origin, static)
    c["principals"]["admin"]["actions"] += ["device.control"]
    c["home"] = home_section(base_url, **override)
    return c


def asset_settings(
    directory, endpoint, ca, origin="http://127.0.0.1:4814", static=None, **override
):
    """Console settings plus a synthetic read-only asset page; the browser never sees a token."""
    c = web_settings(directory, origin, static)
    # A separate, non-operator reading identity: the console's own source identity is not a
    # reader, and the browser login is never one either.
    c["principals"]["assetreader"] = {
        "kind": "service",
        "service": "platform",
        "token_env": "TS019_ASSET_READ",
        "actions": ["asset.read"],
        "asset_connections": ["library-a", "library-b"],
    }
    c["asset_connections"] = {
        "library-a": {"endpoint": endpoint, "ca_file": ca, "token_env": "TS019_ASSET_A"},
        "library-b": {"endpoint": endpoint, "ca_file": ca, "token_env": "TS019_ASSET_B"},
    }
    page = {
        "enabled": True,
        "principal": "assetreader",
        # Narrowing only: `library-b` stays outside this page even though the identity may read it.
        "allowed_connections": ["library-a"],
    }
    page.update(override)
    c["web_assets"] = page
    return c
