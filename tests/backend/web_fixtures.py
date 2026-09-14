"""Only synthetic web identities; never a product default account."""

from pathlib import Path

from services.platform.web_console import password_hash
from source_fixtures import source_settings

PASSWORD = "synthetic-local-password-014"


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
    return c
