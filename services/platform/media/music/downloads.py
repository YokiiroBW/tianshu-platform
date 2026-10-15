"""NetEase audio worker. Queue, leases, cancellation and retry remain owned by Media."""

import asyncio
import json
from pathlib import Path

from ...contracts import Fault, require
from ..destination import safe_path
from ..video_storage import sync_file
from .audio import cover, probe, record, write_tags
from .quality import verify_quality, satisfies
from .resolver import Resolver
from .storage import AudioStorage
from .transfer import Transfer


async def settled(local, operation, *args):
    """Keep filesystem ownership until the executor has actually stopped after cancellation."""
    task = asyncio.create_task(local(operation, *args))
    cancelled = False
    while True:
        try:
            result = await asyncio.shield(task)
            break
        except asyncio.CancelledError:
            cancelled = True
            if task.cancelled():
                raise
        except Exception:
            if cancelled:
                raise asyncio.CancelledError from None
            raise
    if cancelled:
        raise asyncio.CancelledError
    return result


class Downloads:
    def __init__(self, music):
        self.music, self.media = music, music.media
        self.storage, self.transfer = AudioStorage(music), Transfer()
        self.resolver = Resolver(music)

    async def package(self, job, update):
        failure = None
        for candidate in await self.resolver.offers(job):
            try:
                account = candidate["account"]
                await update(
                    acquisition_account_id=account["account_id"],
                    acquisition_account_revision=account["revision"],
                )
                return await self.acquire(job, candidate, update)
            except Fault as error:
                if error.code in {"job_cancelled", "lease_conflict", "scope_changed"}:
                    raise
                failure = error
        raise failure or Fault("music_download_forbidden", 409)

    async def acquire(self, job, candidate, update):
        provider, cookie = candidate["provider"], candidate["cookie"]
        connector, local = self.music.client(provider), self.media.local
        track = {
            k: v
            for k, v in candidate["track"].items()
            if k not in {"revision", "audio_state", "availability"}
        }
        offered = candidate["offered"]
        maximum = min(self.media.config["engine"]["max_bytes"], 1024**3)
        require(
            offered.get("size") is None or offered["size"] <= maximum, "music_file_too_large", 409
        )
        raw = await local(self.storage.stage, job, "source.part")
        raw.unlink(
            missing_ok=True
        )  # Generation-owned scratch only; retries start a fresh transfer.
        await self.transfer.fetch(
            offered["url"],
            raw,
            maximum=maximum,
            expected_size=offered.get("size"),
            expected_md5=offered.get("md5"),
            progress=lambda value: update(progress=value),
            provider=provider,
        )
        await update(state="validating", stage="validating", code="validating")
        observed = await probe(raw, track, self.media.config["engine"])
        quality = verify_quality(job["quality"], offered["level"], observed)
        warnings, artwork, lyrics = [], None, {}
        if track.get("album_id") and hasattr(connector, "album"):
            try:
                track.update(await connector.album(track["album_id"], cookie))
            except Fault:
                warnings.append("music_album_unavailable")
        try:
            lyrics = (
                await connector.lyrics_track(track, cookie)
                if hasattr(connector, "lyrics_track")
                else await connector.lyrics(track["source_id"], cookie)
            )
        except Fault:
            warnings.append("music_lyrics_unavailable")
        if track.get("cover_url"):
            picture = await local(self.storage.stage, job, "cover.input")
            try:
                picture.unlink(missing_ok=True)
                await self.transfer.fetch(
                    track["cover_url"],
                    picture,
                    maximum=16 * 1024**2,
                    provider=provider,
                    artwork=True,
                )
                artwork = await local(lambda: cover(picture.read_bytes()))
            except Fault:
                warnings.append("music_cover_unavailable")
        return await self.prepare_package(
            job, track, raw, observed, quality, artwork, lyrics, warnings
        )

    async def prepare_package(
        self, job, track, raw, observed, quality, artwork=None, lyrics=None, warnings=None
    ):
        local = self.media.local
        provider = track.get("provider", "local")
        lyrics, warnings = lyrics or {}, warnings or []
        audio = await local(self.storage.stage, job, "audio." + quality["extension"])
        raw.replace(audio)
        await settled(local, write_tags, audio, track, artwork, lyrics.get("original", ""))
        # Confirm that tag insertion left a fully decodable audio stream.
        tagged = await probe(audio, track, self.media.config["engine"])
        require(
            all(
                tagged[k] == observed[k]
                for k in ("codec", "sample_rate", "channels", "bits_per_sample", "content_id")
            ),
            "music_tag_failed",
            409,
        )
        files = [(audio, "." + quality["extension"], "audio")]
        extras = {}
        for key, suffix in (
            ("original", ".lrc"),
            ("translated", ".zh.lrc"),
            ("romanized", ".rom.lrc"),
        ):
            if lyrics.get(key):
                extras[suffix] = lyrics[key].encode("utf-8")
        if artwork:
            extras[".jpg"] = artwork
        extras[".source.json"] = json.dumps(
            {
                "provider": provider,
                "source_id": track["source_id"],
                "recording_id": job["recording_id"],
                "requested_from": {"provider": job["provider"], "source_id": job["source_id"]},
                "metadata": track,
                "quality": quality,
                "warnings": warnings,
            },
            ensure_ascii=False,
            indent=2,
        ).encode("utf-8")
        for suffix, content in extras.items():
            path = await local(self.storage.stage, job, "sidecar" + suffix)
            await local(path.write_bytes, content)
            await local(sync_file, path)
            files.append(
                (
                    path,
                    suffix,
                    "metadata"
                    if suffix == ".source.json"
                    else "cover"
                    if suffix == ".jpg"
                    else "lyrics",
                )
            )
        return {
            "track": track,
            "download_provider": provider,
            "download_source_id": track["source_id"],
            "content_id": observed["content_id"],
            "requested_quality": job["quality"],
            "quality": quality,
            "warnings": warnings,
            "files": [
                {
                    "staging": path.relative_to(self.media.config["staging_directory"]).as_posix(),
                    "suffix": suffix,
                    "kind": kind,
                    **await local(record, path),
                }
                for path, suffix, kind in files
            ],
        }

    def package_present(self, package):
        if not package:
            return False
        try:
            return all(
                record(safe_path(Path(self.media.config["staging_directory"]), f["staging"]))
                == {k: f[k] for k in ("sha256", "size_bytes")}
                for f in package["files"]
            )
        except (OSError, Fault):
            return False

    async def run(self, job, owner):
        if job.get("audio_operation") in {"scan", "import"}:
            return await self.music.imports.run(job, owner)
        repo, local = self.media.repo, self.media.local
        identity, generation = job["job_id"], job["generation"]
        running = asyncio.current_task()

        async def update(**changes):
            nonlocal job
            job = await local(
                repo.update_job, identity, changes, owner=owner, generation=generation
            )
            return job

        async def pulse():
            while True:
                await asyncio.sleep(5)
                try:
                    await local(self.storage.guard, job, owner)
                except Fault:
                    running.cancel()
                    return

        heartbeat = asyncio.create_task(pulse())
        try:
            quality, request_version = await local(self.resolver.requests, job)
            await update(
                quality=quality,
                processing_request_version=request_version,
                acquisition_account_id=None,
                acquisition_account_revision=None,
            )
            await self.music.imports.ensure_indexed(job, update)
            await self.storage.refresh_file(job)
            reused = await settled(local, self.storage.reuse, job, owner)
            if reused:
                job = reused
            else:
                intent = await local(repo.get, "music_publication", identity)
                if (
                    intent
                    and intent["state"] != "retired"
                    and satisfies(job["quality"], intent)
                    and await local(self.storage.matches, intent)
                ):
                    job = await settled(local, self.storage.publish, job, owner)
                else:
                    if not await local(
                        self.package_present, job.get("audio_package")
                    ) or not satisfies(job["quality"], job["audio_package"]):
                        # Keep the old intent as history and retain any surviving files.
                        # A rebuilt package gets a fresh collision-safe destination.
                        if intent:
                            await local(self.storage.retire, job)
                        package = await self.package(job, update)
                        await update(
                            audio_package=package,
                            actual_quality=package["quality"]["level"],
                            audio_quality=package["quality"],
                            download_provider=package["download_provider"],
                            download_source_id=package["download_source_id"],
                        )
                    await update(state="publishing", stage="publishing", code="publishing")
                    job = await settled(local, self.storage.publish, job, owner)
            try:
                await settled(local, self.storage.cleanup, job)
            except (Fault, OSError):
                pass  # Existing retention pump retries cleanup after confirmed publication.
            await local(self.storage.export_related, job)
        except asyncio.CancelledError:
            current = await local(repo.job, identity, include_deleted=True)
            if current and current["generation"] == generation and current["state"] != "completed":
                await local(
                    repo.update_job,
                    identity,
                    {
                        "state": "cancelled" if current["cancel_requested"] else "retry_wait",
                        "code": "user_cancelled"
                        if current["cancel_requested"]
                        else "worker_interrupted",
                        "retry_delay": 5,
                    },
                    generation=generation,
                )
            raise
        except (Fault, OSError, TimeoutError) as error:
            code = error.code if isinstance(error, Fault) else "media_io_failure"
            attempts = job.get("attempts", 0) + 1
            transient = code in {
                "music_download_unavailable",
                "music_upstream_unavailable",
                "music_rate_limited",
                "busy",
            }
            state = (
                "cancelled"
                if code == "job_cancelled"
                else "auth_required"
                if code in {"music_auth_required", "music_account_unavailable", "account_changed"}
                else "retry_wait"
                if transient and attempts < 4
                else "failed"
            )
            try:
                await update(
                    state=state,
                    code=code,
                    attempts=attempts,
                    retry_delay=min(300, 15 * 2**attempts),
                )
            except Fault:
                pass  # Lost lease/cancel is settled by its owner; never overwrite a newer worker.
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
