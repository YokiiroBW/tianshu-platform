"""Only implemented platform capabilities and owner-supplied, read-only task projections."""

from urllib.parse import urlsplit

from .contracts import canonical, epoch, loads, require, utc

CAPABILITIES = (
    {
        "id": "platform.origins.resolve",
        "owner": "platform",
        "transport": "published_http",
        "path": "/internal/v1/origins/resolve",
    },
    {
        "id": "platform.model-config.snapshot",
        "owner": "platform",
        "transport": "published_http",
        "path": "/internal/v1/model-config/snapshot",
    },
    {"id": "platform.tasks.read", "owner": "platform", "transport": "internal_port", "path": None},
)


class Projections:
    def __init__(self, store, auth, contracts, clock):
        self.store, self.auth, self.contracts, self.clock = store, auth, contracts, clock

    def capabilities(self, header):
        with self.store.connect() as db:
            self.auth.authenticate(header, db, "capability.read")
            return {"mode": "local_rehearsal", "capabilities": list(CAPABILITIES)}

    def project(self, header, document):
        require(
            isinstance(document, dict)
            and set(document)
            == {
                "owner",
                "job_id",
                "version",
                "summary",
                "phase",
                "event_cursor",
                "link",
                "observed_at",
            },
            "invalid_input",
            400,
        )
        for key in ("owner", "job_id", "event_cursor"):
            self.contracts.check("common#id", document[key])
        require(type(document["version"]) is int and document["version"] > 0, "invalid_input", 400)
        for key, maximum in (("summary", 500), ("phase", 64)):
            require(
                isinstance(document[key], str)
                and 0 < len(document[key]) <= maximum
                and all(ord(c) >= 32 for c in document[key]),
                "invalid_input",
                400,
            )
        require(document["link"] is None or isinstance(document["link"], str), "invalid_input", 400)
        if document["link"] is not None:
            url = urlsplit(document["link"])
            require(
                url.scheme == "https"
                and url.hostname
                and not url.username
                and not url.password
                and not url.query
                and not url.fragment,
                "invalid_input",
                400,
            )
        try:
            require(
                document["observed_at"].endswith("Z")
                and epoch(document["observed_at"]) <= self.clock(),
                "invalid_input",
                400,
            )
        except (ValueError, TypeError, AttributeError):
            require(False, "invalid_input", 400)
        with self.store.connect(write=True) as db:
            _, caller = self.auth.authenticate(header, db, "task.project")
            require(caller["kind"] == "service" and caller["service"] == document["owner"])
            key = (document["owner"], document["job_id"])
            current = db.execute(
                "SELECT * FROM projections WHERE owner=? AND job_id=?", key
            ).fetchone()
            if current:
                require(document["version"] >= current["version"], "version_conflict", 409)
                if document["version"] == current["version"]:
                    require(current["document"] == canonical(document), "version_conflict", 409)
                    return
                require(
                    epoch(document["observed_at"])
                    >= epoch(loads(current["document"])["observed_at"]),
                    "version_conflict",
                    409,
                )
            db.execute(
                "INSERT OR REPLACE INTO projections VALUES(?,?,?,?)",
                (*key, document["version"], canonical(document)),
            )

    def tasks(self, header, *, limit=50, after=None):
        require(type(limit) is int and 1 <= limit <= 100, "invalid_input", 400)
        if after is not None:
            require(isinstance(after, (tuple, list)) and len(after) == 2, "invalid_input", 400)
            for value in after:
                self.contracts.check("common#id", value)
        with self.store.connect() as db:
            _, caller = self.auth.authenticate(header, db, "task.read")
            owners = caller.get("task_owners", [])
            if not owners:
                return {"availability": "unconfigured", "tasks": [], "next_after": None}
            placeholders = ",".join("?" for _ in owners)
            rows = db.execute(
                f"SELECT * FROM projections WHERE owner IN ({placeholders}) AND (owner,job_id) > (?,?) ORDER BY owner,job_id LIMIT ?",
                (*owners, *(after or ("", "")), limit + 1),
            ).fetchall()
            items = [
                {
                    **loads(row["document"]),
                    "freshness": "stale"
                    if self.clock() - epoch(loads(row["document"])["observed_at"]) > 60
                    else "current",
                }
                for row in rows[:limit]
            ]
            return {
                "availability": "available",
                "tasks": items,
                "observed_at": utc(self.clock()),
                "next_after": [rows[limit - 1]["owner"], rows[limit - 1]["job_id"]]
                if len(rows) > limit
                else None,
            }
