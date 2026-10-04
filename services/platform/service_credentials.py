"""Bound image-backend credentials in the existing encrypted external catalog.

Only Companion can resolve its exact purpose/ref/backend origin. Browser views contain
configuration status and opaque references, never token-bearing catalog snapshots.
"""

from urllib.parse import urlsplit

from .contracts import require
from .external_catalog import ExternalCatalog
from .web_external import _secret_change

PATH = "/internal/v1/service-credentials/resolve"


def backend_origin(value):
    require(isinstance(value, str) and 1 <= len(value) <= 2048, "invalid_input", 400)
    try:
        url = urlsplit(value)
        port = url.port
    except ValueError:
        require(False, "invalid_input", 400)
    require(
        url.scheme in {"https", "http"}
        and url.hostname
        and not url.username
        and not url.password
        and url.path in {"", "/"}
        and not url.query
        and not url.fragment
        and (port is None or 0 < port <= 65535),
        "invalid_input",
        400,
    )
    return value.rstrip("/")


class ServiceCredentials:
    def __init__(self, platform):
        self.p = platform
        config = platform.settings.get("web_external")
        self.catalog = ExternalCatalog(config["directory"]) if config else None

    def resolve(self, header, request):
        self.p.contracts.check_image_credential("request", request)
        with self.p.store.connect() as db:
            _, principal = self.p.auth.authenticate(header, db, "source.input")
            require(principal["kind"] == "service" and principal["service"] == "companion")
        require(self.catalog is not None, "not_found", 404)
        _, rows = self.catalog.snapshot()
        row = next(
            (
                row
                for kind, row in rows.items()
                if kind.startswith("images:")
                and row["value"].get("credential_ref") == request["credential_ref"]
            ),
            None,
        )
        require(
            row is not None
            and row["value"].get("enabled")
            and row["value"].get("consumer") == "companion"
            and row["value"].get("purpose") == request["purpose"]
            and row["value"].get("base_url") == request["audience"]
            and row["credential"],
            "not_found",
            404,
        )
        answer = {
            "schema_version": 1,
            "request_id": request["request_id"],
            "credential_ref": request["credential_ref"],
            "token": row["credential"],
        }
        self.p.contracts.check_image_credential("response", answer)
        return answer

    def view(self, actor):
        if self.catalog is None:
            return {"configured": False, "credential_configured": False, "revision": None}
        revision, rows = self.catalog.snapshot()
        row = rows.get("images:" + actor)
        return {
            "configured": row is not None,
            "credential_configured": bool(row and row["credential"]),
            "revision": revision,
        }

    def save(self, actor, value, credential, expected_revision, client_id):
        require(self.catalog is not None, "external_store_unavailable", 503)
        from .contracts import digest

        base_url = backend_origin(value["base_url"])
        spec = _secret_change(credential)
        _, rows = self.catalog.snapshot()
        old = rows.get("images:" + actor)
        has_credential = spec["action"] == "replace" or (
            spec["action"] == "keep" and old and old["credential"]
        )
        reference = "companion-images:" + digest([actor, base_url])[:40] if has_credential else None
        self.catalog.save(
            "images:" + actor,
            {
                "base_url": base_url,
                "enabled": value["enabled"],
                "consumer": "companion",
                "purpose": "companion.images",
                "credential_ref": reference,
            },
            spec,
            {"action": "clear"},
            expected_revision,
            client_id,
        )
        return reference
