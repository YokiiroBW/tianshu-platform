"""Automatic, per-song entitlement routing across connected music accounts."""

from ...contracts import Fault, require
from .catalog import same_recording, search_title
from .quality import LEVELS, aggregate, requested_levels
from .request_control import Deferred


class Resolver:
    def __init__(self, music):
        self.music, self.repo = music, music.media.repo

    def requests(self, job):
        current = self.repo.job(job["job_id"])
        requests = [self.repo.get("music_request", key) for key in current.get("request_ids", [])]
        policies = []
        for request in requests:
            if request:
                playlist = self.repo.get("music_playlist", request["playlist_id"])
                if playlist and request["source_id"] in playlist["entries"]:
                    policies.append(playlist.get("quality", request["quality"]))
        return aggregate(policies or [job["quality"]]), current.get("request_version", 0)

    def bind(self, provider, track, recording_id):
        catalog = self.music.library.catalog
        with self.repo.transaction() as db:
            binding = catalog.register(provider, track, db)
            # Existing explicit source assignments take precedence over search guesses.
            require(
                catalog.resolve(binding["recording_id"], db) == catalog.resolve(recording_id, db),
                "music_match_ambiguous",
                409,
            )
        return binding

    async def offers(self, job, *, report=None):
        local, library = self.music.media.local, self.music.library
        accounts = [
            a for a in await local(self.repo.list, "music_account") if a["state"] == "ready"
        ]
        report = report if report is not None else {}
        report.update(policy="highest_available", providers=[], candidates=[])
        require(accounts, "music_auth_required", 409)
        offers, failures = [], []
        original = {**job["track"], "provider": job.get("provider", "netease")}
        for account in accounts:
            provider = account["provider"]
            client = self.music.client(provider)
            checked = {"provider": provider, "matches": 0, "available_levels": [], "failures": []}
            report["providers"].append(checked)
            try:
                account, cookie = await local(library.account, account["account_id"])
                if provider == original["provider"]:
                    candidates = [original]
                else:
                    candidates = await local(library.catalog.sources, job["recording_id"], provider)
                    if not candidates:
                        query = search_title(original) + " " + " ".join(original.get("artists", []))
                        found = await client.search(query, cookie)
                        candidates = list(
                            {
                                t["source_id"]: t for t in found if same_recording(original, t)
                            }.values()
                        )
                        # Multiple editions with indistinguishable metadata need explicit identity evidence.
                        require(len(candidates) <= 1, "music_match_ambiguous", 409)
                    for track in candidates:
                        await local(self.bind, provider, track, job["recording_id"])
                checked["matches"] = len(candidates)
                for track in candidates:
                    for level in requested_levels(job["quality"]):
                        try:
                            offered = (
                                await client.download_track(track, level, cookie)
                                if hasattr(client, "download_track")
                                else await client.download(track["source_id"], level, cookie)
                            )
                            offers.append(
                                {
                                    "provider": provider,
                                    "track": {**track, "provider": provider},
                                    "account": account,
                                    "cookie": cookie,
                                    "offered": offered,
                                }
                            )
                            checked["available_levels"].append(offered["level"])
                            # One provider's best available offer is enough; actual audio is checked below.
                            break
                        except Fault as error:
                            if isinstance(error, Deferred):
                                raise
                            failures.append(error.code)
                            checked["failures"].append(error.code)
                            if error.code not in {
                                "music_download_forbidden",
                                "music_preview_rejected",
                                "music_quality_unavailable",
                            }:
                                break
            except (Fault, TimeoutError) as error:
                if isinstance(error, Deferred):
                    raise
                code = error.code if isinstance(error, Fault) else "music_upstream_unavailable"
                failures.append(code)
                checked["failures"].append(code)
        offers.sort(key=lambda item: LEVELS.get(item["offered"]["level"], -1), reverse=True)
        if not offers:
            if failures and all(
                code in {"music_auth_required", "music_account_unavailable"} for code in failures
            ):
                raise Fault("music_auth_required", 409)
            if "music_download_forbidden" in failures or "music_preview_rejected" in failures:
                raise Fault("music_download_forbidden", 409)
            if failures:
                raise Fault(failures[0], 502)
            raise Fault("music_match_unavailable", 409)
        return offers
