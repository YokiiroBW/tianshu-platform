"""The platform is the only writer of immutable, version-pinned model snapshots.

Old Chat (text-dialogue/v1) and native (model-protocol/v1) configurations share the
Models owner but keep separate tables, version sequences and revocation key spaces.
"""

import copy
import ipaddress
from urllib.parse import urlsplit

from .auth import secret
from .contracts import canonical, digest, epoch, loads, require


class Models:
    def __init__(self, store, auth, contracts, origins, settings, clock):
        self.store, self.auth, self.contracts, self.origins = store, auth, contracts, origins
        self.registrations = copy.deepcopy(settings.get("providers", {}))
        self.max_lifetime = settings.get("config_max_lifetime_seconds", 3600)
        require(
            type(self.max_lifetime) is int and 1 <= self.max_lifetime <= 86400, "invalid_input", 400
        )
        self.clock = clock

    def validate_publication(self, document):
        self.contracts.check("model#config_response", document)
        self._validate_window(document)
        providers = self._validate_providers(
            document["providers"], "openai-chat-completions", pinned=False
        )
        self._validate_bindings(document, providers)
        self._validate_no_secrets(document)

    def validate_native_publication(self, document):
        self.contracts.check("model-protocol#config_response", document)
        self._validate_window(document)
        providers = self._validate_providers(document["providers"], "openai-responses", pinned=True)
        self._validate_bindings(document, providers)
        self._validate_no_secrets(document)

    def _validate_window(self, document):
        now = self.clock()
        published, until = epoch(document["published_at"]), epoch(document["usable_until"])
        require(
            published <= now < until and 0 < until - published <= self.max_lifetime,
            "invalid_input",
            400,
        )

    def _validate_providers(self, items, protocol, *, pinned):
        providers = {p["provider_id"]: p for p in items}
        require(len(providers) == len(items), "invalid_input", 400)
        for provider_id, provider in providers.items():
            registration = self.registrations.get(provider_id)
            require(registration is not None, "dependency_unavailable", 503)
            require(provider["protocol"] == protocol, "invalid_input", 400)
            if pinned:
                # Native publication requires the registration to pin this exact protocol.
                require(registration.get("protocol") == protocol, "invalid_input", 400)
            else:
                require(registration.get("protocol") in (None, protocol), "invalid_input", 400)
            require(
                all(
                    provider[k] == registration[k]
                    for k in (
                        "base_url",
                        "credential_ref",
                        "credential_namespace",
                        "capability_verification",
                    )
                )
            )
            require(provider["model_id"] in registration["model_ids"])
            require(
                set(provider["verified_capabilities"]) <= set(registration["verified_capabilities"])
            )
            require(
                len(set(provider["verified_capabilities"]))
                == len(provider["verified_capabilities"]),
                "invalid_input",
                400,
            )
            self._validate_reviewed_address(provider["base_url"], registration)
        return providers

    def _validate_reviewed_address(self, base, registration):
        url = urlsplit(base)
        require(
            url.scheme in {"http", "https"}
            and url.hostname
            and not url.username
            and not url.password
            and not url.query
            and not url.fragment,
            "invalid_input",
            400,
        )
        require(
            not base.endswith("/")
            and not any(ord(c) <= 32 or ord(c) > 126 for c in base)
            and "%" not in base
            and "\\" not in base,
            "invalid_input",
            400,
        )
        require(not any(p in {".", ".."} for p in url.path.split("/")), "invalid_input", 400)
        try:
            addresses = [ipaddress.ip_address(a) for a in registration["reviewed_addresses"]]
            require(bool(addresses), "invalid_input", 400)
            if url.scheme == "http":
                require(
                    registration.get("allow_private_http") is True
                    and all(a.is_private for a in addresses)
                )
                # This local-only platform cannot approve a new cleartext production boundary.
                require(all(a.is_loopback for a in addresses))
            try:
                literal = ipaddress.ip_address(url.hostname)
            except ValueError:
                literal = None
            require(literal is None or addresses == [literal], "invalid_input", 400)
            _ = url.port
        except ValueError:
            require(False, "invalid_input", 400)

    def _validate_bindings(self, document, providers):
        workloads = set()
        for binding in document["bindings"]:
            require(
                binding["workload"] not in workloads and binding["provider_id"] in providers,
                "invalid_input",
                400,
            )
            workloads.add(binding["workload"])
            require(
                binding["model_id"] == providers[binding["provider_id"]]["model_id"],
                "invalid_input",
                400,
            )

    def _validate_no_secrets(self, document):
        # Defense in depth against putting known local credentials into arbitrary config strings.
        encoded = canonical(document)
        require(
            not any(
                value in encoded
                for p in self.auth.principals.values()
                if (value := secret(p["token_env"]))
            ),
            "invalid_input",
            400,
        )

    def publish(self, header, document):
        with self.store.connect(write=True) as db:
            identity, _ = self.auth.authenticate(header, db, "config.publish", operator=True)
            self.validate_publication(document)
            return self._store_publication(
                db,
                identity,
                "configs",
                document,
                "config_version",
                "config.publish",
                revoked_status=410,
            )

    def native_publish(self, header, document):
        with self.store.connect(write=True) as db:
            identity, _ = self.auth.authenticate(header, db, "config.publish", operator=True)
            self.validate_native_publication(document)
            return self._store_publication(
                db,
                identity,
                "native_configs",
                document,
                "native_config_version",
                "native_config.publish",
                # model-protocol/v1 keeps one status for every revoked configuration.
                revoked_status=403,
            )

    def _store_publication(
        self, db, identity, table, document, version_key, operation, *, revoked_status
    ):
        # Only fixed internal table names; never anything derived from input.
        stable = {k: v for k, v in document.items() if k != "request_id"}
        version, checksum = document[version_key], digest(stable)
        current = db.execute(f"SELECT * FROM {table} WHERE version=?", (version,)).fetchone()
        if current:
            require(not current["revoked"], "forbidden", revoked_status)
            require(current["digest"] == checksum, "version_conflict", 409)
            return {version_key: version, "published": True, "deduplicated": True}
        highest = db.execute(f"SELECT max(version) FROM {table}").fetchone()[0]
        require(highest is None or version > highest, "version_conflict", 409)
        db.execute(
            f"INSERT INTO {table}(version,document,digest) VALUES(?,?,?)",
            (version, canonical(stable), checksum),
        )
        db.execute(
            "INSERT INTO audit(principal,operation,object_id,observed_at) VALUES(?,?,?,?)",
            (identity, operation, str(version), self.clock()),
        )
        return {version_key: version, "published": True, "deduplicated": False}

    def revoke(self, header, version):
        require(type(version) is int and version > 0, "invalid_input", 400)
        with self.store.connect(write=True) as db:
            identity, _ = self.auth.authenticate(header, db, "config.revoke", operator=True)
            require(
                db.execute("SELECT 1 FROM configs WHERE version=?", (version,)).fetchone(),
                "not_found",
                404,
            )
            db.execute("UPDATE configs SET revoked=1 WHERE version=?", (version,))
            db.execute(
                "INSERT INTO audit(principal,operation,object_id,observed_at) VALUES(?,?,?,?)",
                (identity, "config.revoke", str(version), self.clock()),
            )

    def native_revoke(self, header, version):
        require(type(version) is int and version > 0, "invalid_input", 400)
        with self.store.connect(write=True) as db:
            identity, _ = self.auth.authenticate(header, db, "config.revoke", operator=True)
            require(
                db.execute("SELECT 1 FROM native_configs WHERE version=?", (version,)).fetchone(),
                "not_found",
                404,
            )
            db.execute("UPDATE native_configs SET revoked=1 WHERE version=?", (version,))
            db.execute(
                "INSERT INTO audit(principal,operation,object_id,observed_at) VALUES(?,?,?,?)",
                (identity, "native_config.revoke", str(version), self.clock()),
            )

    def snapshot(self, header, request):
        self.contracts.check("model#config_request", request)
        with self.store.connect() as db:
            _, caller = self.auth.authenticate(header, db, "config.snapshot")
            require(caller["kind"] == "service")
            self.origins.context(
                db,
                request["query"]["origin"]["assertion_ref"],
                caller["service"],
                "platform",
                "config.snapshot",
            )
            version = request["config_version"]
            if version is None:
                version = db.execute("SELECT max(version) FROM configs").fetchone()[0]
                require(version is not None, "dependency_unavailable", 503)
            require(version in caller.get("config_versions", []))
            row = db.execute("SELECT * FROM configs WHERE version=?", (version,)).fetchone()
            require(row is not None, "not_found", 404)
            require(not row["revoked"], "forbidden", 410)
            document = loads(row["document"])
            require(
                document["config_version"] == version and digest(document) == row["digest"],
                "dependency_unavailable",
                503,
            )
            require(
                epoch(document["published_at"]) <= self.clock() < epoch(document["usable_until"]),
                "forbidden",
                410,
            )
            document["request_id"] = request["query"]["request_id"]
            self.contracts.check("model#config_response", document)
            return document

    def native_snapshot(self, header, request):
        self.contracts.check("model-protocol#config_request", request)
        with self.store.connect() as db:
            _, caller = self.auth.authenticate(header, db, "config.snapshot")
            require(caller["kind"] == "service")
            self.origins.context(
                db,
                request["query"]["origin"]["assertion_ref"],
                caller["service"],
                "platform",
                "config.snapshot",
            )
            document = self._native_selection(db, caller, request["native_config_version"])
            document["request_id"] = request["query"]["request_id"]
            self.contracts.check("model-protocol#config_response", document)
            return document

    def _native_selection(self, db, caller, version):
        # The native allowlist is explicit; missing entries mean nothing is authorized and
        # legacy config_versions are never inherited.
        authorized = caller.get("native_config_versions", [])
        if version is not None:
            require(version in authorized, "forbidden", 403)
            row = db.execute("SELECT * FROM native_configs WHERE version=?", (version,)).fetchone()
            require(row is not None, "not_found", 404)
            require(not row["revoked"], "forbidden", 403)
            document = self._native_intact(row)
            # An expired configuration is an unavailable dependency, not a permission failure.
            require(self._native_live(document), "dependency_unavailable", 503)
            return document
        require(authorized, "forbidden", 403)
        # Filter by the caller's own versions in SQL: never read the global highest first.
        marks = ",".join("?" * len(authorized))
        live = []
        for row in db.execute(
            f"SELECT * FROM native_configs WHERE revoked=0 AND version IN ({marks})",
            tuple(authorized),
        ).fetchall():
            document = self._native_intact(row)
            if self._native_live(document):
                live.append(document)
        require(live, "dependency_unavailable", 503)
        return max(live, key=lambda document: document["native_config_version"])

    def _native_intact(self, row):
        document = loads(row["document"])
        require(
            document["native_config_version"] == row["version"]
            and digest(document) == row["digest"],
            "dependency_unavailable",
            503,
        )
        return document

    def _native_live(self, document):
        return epoch(document["published_at"]) <= self.clock() < epoch(document["usable_until"])

    def view(self, header):
        """Local UI adapter port; deliberately excludes URLs, namespaces, refs and secrets."""
        with self.store.connect() as db:
            self.auth.authenticate(header, db, "config.view", operator=True)
            row = db.execute("SELECT * FROM configs ORDER BY version DESC LIMIT 1").fetchone()
            if row is None:
                return {"availability": "unconfigured", "config_version": None, "providers": []}
            doc = loads(row["document"])
            availability = (
                "revoked"
                if row["revoked"]
                else "expired"
                if epoch(doc["usable_until"]) <= self.clock()
                else "available"
            )
            return {
                "availability": availability,
                "config_version": row["version"],
                "usable_until": doc["usable_until"],
                "providers": [
                    {
                        k: p[k]
                        for k in (
                            "provider_id",
                            "model_id",
                            "capability_verification",
                            "verified_capabilities",
                        )
                    }
                    for p in doc["providers"]
                ],
            }

    def view_native(self, header):
        """Local operator view for native configs; excludes URLs, namespaces and secret refs."""
        with self.store.connect() as db:
            self.auth.authenticate(header, db, "config.view", operator=True)
            row = db.execute(
                "SELECT * FROM native_configs ORDER BY version DESC LIMIT 1"
            ).fetchone()
            if row is None:
                return {
                    "availability": "unconfigured",
                    "native_config_version": None,
                    "providers": [],
                }
            doc = loads(row["document"])
            availability = (
                "revoked"
                if row["revoked"]
                else "expired"
                if epoch(doc["usable_until"]) <= self.clock()
                else "available"
            )
            return {
                "availability": availability,
                "native_config_version": row["version"],
                "usable_until": doc["usable_until"],
                "providers": [
                    {
                        k: p[k]
                        for k in (
                            "provider_id",
                            "protocol",
                            "model_id",
                            "capability_verification",
                            "verified_capabilities",
                        )
                    }
                    for p in doc["providers"]
                ],
            }
