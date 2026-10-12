"""Validated global BV/CID/actual-quality sources with fenced, process-safe ownership."""

import asyncio
from pathlib import Path

from ..contracts import Fault, digest, require
from .engine import file_record
from .organizer import library_lock


def validate_download(download, cid, quality):
    require(download.get("actual_quality") == quality, "quality_mismatch", 503)
    path = Path(download["file"])
    require(
        not any(p.is_symlink() or p.is_junction() for p in (path, *path.parents)),
        "download_cache_changed",
        409,
    )
    require(path.suffix == "." + download["extension"], "download_cache_changed", 409)
    record = file_record(path, "cache", "video", cid)
    require(
        record["size_bytes"] > 0
        and record["sha256"] == download.get("sha256")
        and record["size_bytes"] == download.get("size_bytes"),
        "download_cache_changed",
        409,
    )
    return dict(download)


class DownloadCache:
    def __init__(self, media):
        self.media = media

    async def selected_quality(self, job, part, cookie):
        requested = job["quality"]
        if requested["mode"] == "exact" and not requested["allow_fallback"]:
            return requested["quality_id"]
        directory = (
            Path(self.media.config["staging_directory"])
            / ".probes"
            / digest({"job": job["job_id"], "cid": part["cid"], "generation": job["generation"]})
        )
        formats = await self.media.engine.formats(job["bvid"], part, cookie, directory)
        available = {item["quality_id"] for item in formats}
        require(available, "quality_unavailable", 503)
        if requested["mode"] == "exact" and requested["quality_id"] in available:
            return requested["quality_id"]
        return max(available, key=int)

    async def obtain(self, job, part, cookie, owner, quality):
        media, repo = self.media, self.media.repo
        bvid, cid = job["bvid"], part["cid"]
        key = (bvid, cid, quality)
        directory = (
            Path(media.config["staging_directory"])
            / ".downloads"
            / "global"
            / digest({"bvid": bvid, "cid": cid, "quality": quality})
        )
        while True:
            await media.local(repo.heartbeat, job["job_id"], owner, job["generation"])
            lock = library_lock(directory)
            acquisition = asyncio.create_task(media.local(lock.__enter__))
            try:
                await asyncio.shield(acquisition)
            except asyncio.CancelledError:
                # LocalWork may already be opening the descriptor off-loop. Close it
                # after that operation finishes even when this caller no longer waits.
                def release_acquired(task):
                    try:
                        task.result()
                    except BaseException:
                        return
                    lock.__exit__(None, None, None)

                acquisition.add_done_callback(release_acquired)
                raise
            except Fault as error:
                if error.code != "busy":
                    raise
                await asyncio.sleep(0.25)
                continue
            pulse = None
            finished = True
            generation = None
            try:
                # Lock first: an expired database lease cannot steal from a process
                # that is still downloading. Crashed processes release the OS lock.
                claim = await media.local(repo.download_claim, *key, owner)
                if claim["state"] == "waiting":
                    await asyncio.sleep(0.25)
                    continue
                generation = claim["generation"]
                if claim["state"] == "ready":
                    try:
                        return await media.local(
                            validate_download, claim["download"], cid, quality
                        ), True
                    except (Fault, OSError, KeyError, ValueError):
                        await media.local(repo.download_invalidate, *key, generation)
                        continue
                finished = False
                current_task = asyncio.current_task()

                async def heartbeat():
                    while True:
                        await asyncio.sleep(5)
                        try:
                            await media.local(
                                repo.heartbeat, job["job_id"], owner, job["generation"]
                            )
                            await media.local(repo.download_heartbeat, *key, owner, generation)
                        except Fault:
                            current_task.cancel()
                            return

                pulse = asyncio.create_task(heartbeat())
                # Older completed, interrupted and publication-failed jobs can all supply
                # a source, but only a matching, fully hashed download is accepted.
                for previous in await media.local(repo.legacy_downloads, bvid, cid):
                    try:
                        download = await media.local(validate_download, previous, cid, quality)
                    except (Fault, OSError, KeyError, ValueError):
                        continue
                    await media.local(repo.download_finish, *key, owner, generation, download)
                    finished = True
                    return download, True
                await media.engine.check()
                require(media.engine.status["state"] == "ready", media.engine.status["code"], 503)
                before = await media.connector.resolve(bvid, cookie, with_formats=False)
                mapping = {value["cid"]: value for value in before["snapshot"]["parts"]}
                require(cid in mapping, "source_cid_removed", 409)
                exact_job = {
                    **job,
                    "quality": {"mode": "exact", "quality_id": quality, "allow_fallback": False},
                }
                download = await media.engine.download(
                    exact_job, mapping[cid], cookie, directory / str(generation)
                )
                require(download["actual_quality"] == quality, "quality_mismatch", 503)
                after = await media.connector.resolve(bvid, cookie, with_formats=False)
                require(
                    [(p["cid"], p["index"]) for p in before["snapshot"]["parts"]]
                    == [(p["cid"], p["index"]) for p in after["snapshot"]["parts"]],
                    "source_identity_changed",
                    409,
                )
                record = await media.local(
                    file_record, Path(download["file"]), "cache", "video", cid
                )
                download.update(
                    sha256=record["sha256"],
                    size_bytes=record["size_bytes"],
                    source_index=mapping[cid]["index"],
                    source_captured_at=before["snapshot"]["captured_at"],
                )
                download = await media.local(validate_download, download, cid, quality)
                await media.local(repo.heartbeat, job["job_id"], owner, job["generation"])
                await media.local(repo.download_finish, *key, owner, generation, download)
                finished = True
                return download, False
            finally:
                try:
                    if pulse:
                        pulse.cancel()
                        await asyncio.gather(pulse, return_exceptions=True)
                    if not finished:
                        try:
                            await media.local(repo.download_finish, *key, owner, generation)
                        except Fault:
                            pass
                finally:
                    # Unlock is a bounded descriptor operation. A second cancellation
                    # cannot leave the live process holding this download indefinitely.
                    lock.__exit__(None, None, None)
