"""Compare every entitled source and the existing file before choosing an archive."""

from math import inf

from ...contracts import Fault
from .audio import inspect_audio
from .decoding import encrypted_format
from .quality import LEVELS, LOSSLESS_CODECS, minimum_level, quality_rank, verify_quality
from .request_control import Deferred


class Selection:
    def __init__(self, downloads):
        self.downloads = downloads
        self.media = downloads.media

    async def choose(self, job, update, existing=None, prepared=None):
        downloads, local = self.downloads, self.media.local
        report = {"policy": "highest_available", "providers": [], "candidates": []}
        failure, attempts = None, []
        best = (
            existing
            if existing and quality_rank(existing["quality"])[0] >= minimum_level(job["quality"])
            else None
        )
        package = None
        selected_account = None
        if (
            prepared
            and quality_rank(prepared["quality"])[0] >= minimum_level(job["quality"])
            and (best is None or quality_rank(prepared["quality"]) > quality_rank(best["quality"]))
        ):
            best, package = prepared, prepared
        try:
            offers = await downloads.resolver.offers(job, report=report)
        except Fault as error:
            if isinstance(error, Deferred):
                raise
            offers, failure = [], error
        # Source enrichment may have just linked this request to an existing file.
        # Refresh after matching, before any full transfer, so it joins the comparison.
        current = existing or await local(downloads.storage.available, job)
        if (
            current
            and quality_rank(current["quality"])[0] >= minimum_level(job["quality"])
            and (best is None or quality_rank(current["quality"]) >= quality_rank(best["quality"]))
        ):
            best, package = current, None
        # No full audio transfer begins until every account's search/entitlement check finishes.
        await update(
            state="validating",
            stage="music_comparing",
            code="music_comparing",
            audio_routing=report,
        )
        ranked = []
        for index, candidate in enumerate(offers):
            evidence = {
                "provider": candidate["provider"],
                "source_id": candidate["track"]["source_id"],
                "offered_level": candidate["offered"]["level"],
                "probe": "unknown",
            }
            candidate_job = {**job, "candidate_index": index}
            path = await local(downloads.storage.stage, candidate_job, "quality-prefix.part")
            path.unlink(missing_ok=True)
            observed = None
            try:
                await downloads.transfer.sample(
                    candidate["offered"]["url"],
                    path,
                    provider=candidate["provider"],
                    account_id=candidate["account"]["account_id"],
                )
                encrypted = await local(
                    encrypted_format,
                    path,
                    (candidate["offered"]["url"], candidate["offered"].get("format")),
                    candidate["provider"],
                )
                if encrypted:
                    evidence["probe"] = "encrypted"
                else:
                    described = await inspect_audio(path, self.media.config["engine"])
                    if described["codec"] in LOSSLESS_CODECS or described["bit_rate"] > 0:
                        observed = verify_quality(
                            {"level": "best", "allow_fallback": True}, "observed", described
                        )
                        evidence.update(probe="header", quality=observed)
            except Fault as error:
                if isinstance(error, Deferred):
                    raise
                evidence["probe_code"] = error.code
            # Unknown formats stay eligible: full validation may be needed to compare them.
            priority = (
                quality_rank(observed)
                if observed
                else (
                    LEVELS.get(candidate["offered"]["level"], 5),
                    inf,
                    inf,
                    inf,
                )
            )
            ranked.append((priority, index, candidate, evidence))
            report["candidates"].append(evidence)
        ranked.sort(key=lambda row: row[0], reverse=True)
        await update(audio_routing=report)
        for priority, index, candidate, evidence in ranked:
            if best and priority <= quality_rank(best["quality"]):
                evidence["decision"] = "already_equal_or_better"
                continue
            try:
                account = candidate["account"]
                await update(
                    acquisition_account_id=account["account_id"],
                    acquisition_account_revision=account["revision"],
                    audio_unlock=None,
                    state="downloading",
                    stage="downloading",
                    code="downloading",
                )
                acquired = await downloads.acquire(
                    {**job, "candidate_index": index}, candidate, update
                )
                evidence["validated_quality"] = acquired["quality"]
                if best is None or quality_rank(acquired["quality"]) > quality_rank(
                    best["quality"]
                ):
                    best, package = acquired, acquired
                    selected_account = account
                    evidence["decision"] = "validated_candidate"
                else:
                    evidence["decision"] = "existing_candidate_better"
            except Fault as error:
                if isinstance(error, Deferred):
                    raise
                if error.code in {"job_cancelled", "lease_conflict", "scope_changed"}:
                    raise
                failure = error
                current = await local(self.media.repo.job, job["job_id"])
                attempts.append(
                    {
                        "provider": candidate["provider"],
                        "source_id": candidate["track"]["source_id"],
                        "code": error.code,
                        "unlock": current.get("audio_unlock"),
                    }
                )
                evidence.update(decision="failed", code=error.code)
                await update(audio_attempts=attempts)
        if best is None:
            await update(audio_routing=report)
            raise failure or Fault("music_download_forbidden", 409)
        report["selected"] = {
            "provider": best.get("download_provider", "local"),
            "source_id": best.get("download_source_id"),
            "quality": best["quality"],
            "reused": package is None,
        }
        await update(
            audio_routing=report,
            audio_unlock=best.get("unlock"),
            acquisition_account_id=selected_account["account_id"] if selected_account else None,
            acquisition_account_revision=selected_account["revision"] if selected_account else None,
        )
        return package, best if package is None else None
