"""One metadata refresh domain operation for the web and trusted maintenance."""

from ..contracts import require
from .metadata import normalize_bilibili

OPERATION = "jobs/refresh_metadata"


class MetadataRefresh:
    def __init__(self, media, project_job):
        self.media, self.project_job = media, project_job

    async def prepare(self, body):
        require(set(body) == {"job_id", "expected_revision", "client_id"}, "invalid_input", 400)
        replay = await self.media.local(
            self.media.repo.replay, body["client_id"], {"operation": OPERATION, "body": body}
        )
        if replay:
            return {"replay": replay}
        previous = await self.media.local(self.media.repo.job, body["job_id"])
        require(previous is not None, "not_found", 404)
        require(previous.get("media_kind") not in {"audio", "book"}, "job_action_conflict", 409)
        require(previous["revision"] == body["expected_revision"], "version_conflict", 409)
        require(
            previous["state"] in {"waiting_metadata", "failed", "published", "completed"},
            "job_action_conflict",
            409,
        )
        cookie = await self.media.local(self.media.accounts.cookie, previous["account_id"])
        resolved = await self.media.connector.resolve(previous["bvid"], cookie, with_formats=False)
        require(
            set(previous["selected_cids"])
            <= {part["cid"] for part in resolved["snapshot"]["parts"]},
            "source_cid_removed",
            409,
        )
        normalize_bilibili(resolved["snapshot"], overrides=previous.get("overrides"))
        return {"resolved": resolved}

    def commit(self, body, prepared, db):
        return {
            "job": self.project_job(
                self.media.repo.refresh_metadata(
                    body["job_id"], body["expected_revision"], prepared["resolved"], db=db
                )
            )
        }

    async def refresh(self, *, job_id, expected_revision, client_id):
        body = {"job_id": job_id, "expected_revision": expected_revision, "client_id": client_id}
        prepared = await self.prepare(body)
        if prepared.get("replay"):
            return prepared["replay"]
        result = await self.media.local(
            self.media.repo.receipt,
            client_id,
            {"operation": OPERATION, "body": body},
            lambda db: self.commit(body, prepared, db),
        )
        self.media.wake.set()
        return result
