"""Bound image-backend credentials in the existing encrypted external catalog.

Only Companion can resolve its exact purpose/ref/backend origin. Browser views contain
configuration status and opaque references, never token-bearing catalog snapshots.
"""

from urllib.parse import urlsplit

from .contracts import Fault, require
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


def skill_origin(value):
    """Skills may fetch a manifest path; credentials remain bound to its origin."""
    require(isinstance(value, str) and 1 <= len(value) <= 2048, "invalid_input", 400)
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise Fault("invalid_input", 400) from None
    require(
        parsed.scheme in {"http", "https"}
        and parsed.hostname
        and not parsed.username
        and not parsed.password
        and not parsed.query
        and not parsed.fragment
        and "\\" not in value
        and all(ord(char) > 32 for char in value)
        and (port is None or 0 < port <= 65535),
        "invalid_input",
        400,
    )
    host = parsed.hostname.lower()
    host = "[" + host + "]" if ":" in host else host
    return parsed.scheme + "://" + host + (":" + str(port) if port is not None else "")


class ServiceCredentials:
    def __init__(self, platform):
        self.p = platform
        config = platform.settings.get("web_external")
        self.catalog = ExternalCatalog(config["directory"]) if config else None

    def resolve(self, header, request):
        require(isinstance(request, dict), "invalid_input", 400)
        skills = request.get("purpose") == "companion.skills"
        check = (
            self.p.contracts.check_skill_credential
            if skills
            else self.p.contracts.check_image_credential
        )
        check("request", request)
        with self.p.store.connect() as db:
            _, principal = self.p.auth.authenticate(header, db, "source.input")
            require(principal["kind"] == "service" and principal["service"] == "companion")
        require(self.catalog is not None, "not_found", 404)
        _, rows = self.catalog.snapshot()
        row = next(
            (
                row
                for kind, row in rows.items()
                if kind.startswith("skills:" if skills else "images:")
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
        check("response", answer)
        return answer

    @staticmethod
    def _skill_key(actor, target, origin):
        from .contracts import digest

        return "skills:" + digest([actor, target, origin])

    def view_skill(self, actor, target, current):
        if self.catalog is None:
            return {"credential_configured": False, "revision": None}
        revision, rows = self.catalog.snapshot()
        url = current.get("base_url") or current.get("manifest_url")
        row = rows.get(self._skill_key(actor, target, skill_origin(url))) if url else None
        configured = bool(
            current.get("credential_configured")
            and url
            and row
            and row["credential"]
            and row["value"].get("base_url") == skill_origin(url)
        )
        return {"credential_configured": configured, "revision": revision}

    def skill_reference(self, actor, target, url, credential, current):
        """Reconstruct an owner replay without editing the credential catalog."""
        from .contracts import digest

        origin = skill_origin(url)
        spec = _secret_change(credential)
        if spec["action"] == "replace":
            return "companion-skills:" + digest([actor, target, origin])[:40]
        if spec["action"] == "clear" or self.catalog is None:
            return None
        _, rows = self.catalog.snapshot()
        row = rows.get(self._skill_key(actor, target, origin))
        old_url = current.get("base_url") or current.get("manifest_url")
        if (
            current.get("credential_configured")
            and old_url
            and skill_origin(old_url) == origin
            and row
            and row["credential"]
            and row["value"].get("base_url") == origin
        ):
            return row["value"].get("credential_ref")
        return None

    def save_skill(self, actor, target, url, credential, revision, client_id, current):
        """Use the existing catalog CAS; another actor or origin never supplies a token."""
        require(self.catalog is not None, "external_store_unavailable", 503)
        origin = skill_origin(url)
        spec = _secret_change(credential)
        reference = self.skill_reference(actor, target, url, credential, current)
        current_revision, rows = self.catalog.snapshot()
        row = rows.get(self._skill_key(actor, target, origin))
        if spec["action"] == "keep":
            if reference and row and row["credential"]:
                spec = {"action": "replace", "value": row["credential"]}
            elif row and row["credential"]:
                # Keep encrypted bytes for explicit recovery; this new connection
                # receives no reference and cannot consume the previous origin's key.
                require(current_revision == revision, "revision_conflict", 409)
                return None
        self.catalog.save(
            self._skill_key(actor, target, origin),
            {
                "base_url": origin,
                # Registry owns role/source enablement. Existing operations must
                # retain their credential after the skill stops accepting new calls.
                "enabled": True,
                "consumer": "companion",
                "purpose": "companion.skills",
                "credential_ref": reference,
            },
            spec,
            {"action": "clear"},
            revision,
            client_id,
        )
        return reference

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

    @staticmethod
    def _connection_row(rows, connection):
        reference = connection.get("credential_ref")
        if not reference or not connection.get("base_url"):
            return None
        return next(
            (
                row
                for kind, row in rows.items()
                if kind.startswith("images:")
                and row["value"].get("credential_ref") == reference
                and row["value"].get("base_url") == connection["base_url"]
                and row["value"].get("consumer") == "companion"
                and row["value"].get("purpose") == "companion.images"
            ),
            None,
        )

    def view_connection(self, connection):
        if self.catalog is None:
            return {"configured": False, "credential_configured": False, "revision": None}
        revision, rows = self.catalog.snapshot()
        row = self._connection_row(rows, connection)
        return {
            "configured": bool(connection.get("base_url")),
            "credential_configured": bool(row and row["credential"]),
            "revision": revision,
        }

    def save_connection(self, value, credential, expected_revision, client_id, connection):
        """A shared connection keeps only the owner's exact current origin-bound secret."""
        require(self.catalog is not None, "external_store_unavailable", 503)
        from .contracts import digest

        base_url = backend_origin(value["base_url"])
        spec = _secret_change(credential)
        revision, rows = self.catalog.snapshot()
        current = self._connection_row(rows, connection)
        shared = rows.get("images:connection")
        if spec["action"] == "keep":
            if current and current["credential"] and connection["base_url"] == base_url:
                # The same conversion on replay keeps the catalog's durable receipt
                # stable, including migration from an old actor-owned reference.
                spec = {"action": "replace", "value": current["credential"]}
            elif shared and shared["credential"]:
                # A new origin must never receive the previous origin's token.
                # Keep those encrypted bytes until an explicit replace/clear; the
                # authoritative new connection uses no credential reference.
                require(revision == expected_revision, "revision_conflict", 409)
                return None
        reference = (
            "companion-images:" + digest(["connection", base_url])[:40]
            if spec["action"] == "replace"
            else None
        )
        self.catalog.save(
            "images:connection",
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

    def connection_reference(self, value, credential):
        """Reconstruct only an existing request's reference, without mutating any secret."""
        require(self.catalog is not None, "external_store_unavailable", 503)
        from .contracts import digest

        base_url = backend_origin(value["base_url"])
        spec = _secret_change(credential)
        if spec["action"] == "replace":
            return "companion-images:" + digest(["connection", base_url])[:40]
        if spec["action"] == "clear":
            return None
        _, rows = self.catalog.snapshot()
        row = rows.get("images:connection")
        if row and row["value"].get("base_url") == base_url and row["credential"]:
            return row["value"].get("credential_ref")
        return None

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
