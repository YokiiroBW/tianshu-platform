"""CAS-owned download, immutable package assembly and publication recovery."""

import asyncio
import base64
import hashlib
import json
import os
import shutil
import uuid
from pathlib import Path

from ..contracts import Fault, require
from .engine import file_record, validate_image
from .metadata import normalize_bilibili
from .sidecars import render_sidecars
from .types import ImageBinding, RenderRequest


class Worker:
    def __init__(self, media):
        self.media = media
        self.active = {}

    async def pump(self, index):
        owner = str(uuid.uuid4()) + ":" + str(index)
        while True:
            job = await self.media.local(self.media.repo.claim, owner)
            if job is None:
                self.media.wake.clear()
                try:
                    await asyncio.wait_for(self.media.wake.wait(), 2)
                except TimeoutError:
                    pass
                continue
            task = asyncio.create_task(self.run(job, owner))
            self.active[job["job_id"]] = task
            try:
                await task
            except asyncio.CancelledError:
                if asyncio.current_task().cancelling():
                    raise
            finally:
                self.active.pop(job["job_id"], None)

    async def run(self, job, owner):
        generation = job["generation"]
        repo = self.media.repo
        target = self.media.config["targets"][job["target_id"]]
        identity = job["job_id"]

        async def update(**changes):
            nonlocal job
            job = await self.media.local(
                repo.update_job, identity, changes, owner=owner, generation=generation
            )
            return job

        async def pulse():
            while True:
                await asyncio.sleep(5)
                try:
                    await self.media.local(repo.heartbeat, identity, owner, generation)
                    if job["account_id"]:
                        current = await self.media.local(repo.get, "account", job["account_id"])
                        require(
                            current is not None
                            and current["state"] == "ready"
                            and current["revision"]
                            == job.get("account_revision", current["revision"]),
                            "account_changed",
                            409,
                        )
                except Fault:
                    task = self.active.get(identity)
                    if task:
                        task.cancel()
                    return

        heartbeat = asyncio.create_task(pulse())
        try:
            if job["cancel_requested"]:
                raise asyncio.CancelledError
            require(target.get("publisher"), "publisher_not_configured", 503)
            if job.get("asset_receipt") and not job.get("metadata_changed"):
                await self.verify_libraries(job, update, target)
                return
            if not job.get("package"):
                if set(job["selected_cids"]) - set(job.get("downloads", {})) or not job.get(
                    "base_job_id"
                ):
                    cookie = await self.media.local(self.media.accounts.cookie, job["account_id"])
                    if job["account_id"]:
                        account = await self.media.local(repo.get, "account", job["account_id"])
                        await update(account_revision=account["revision"])
                    await self.media.engine.check()
                    require(
                        self.media.engine.status["state"] == "ready",
                        self.media.engine.status["code"],
                        503,
                    )
                    # An already-owned edition supplies immutable old videos; only missing CIDs download.
                    base = await self.base(job)
                    replace_cids = job.get("replace_video_cids", [])
                    if base and base["quality"] != job["quality"]:
                        replace_cids = list(base["selected_cids"])
                    if base:
                        selected = list(dict.fromkeys(base["selected_cids"] + job["selected_cids"]))
                        snapshot = dict(job["snapshot"])
                        parts = {part["cid"]: part for part in base["snapshot"]["parts"]}
                        parts.update({part["cid"]: part for part in snapshot["parts"]})
                        snapshot["parts"] = list(parts.values())
                        await update(
                            selected_cids=selected,
                            snapshot=snapshot,
                            base_job_id=base["job_id"],
                            replace_video_cids=replace_cids,
                        )
                    downloads = {
                        **(base.get("downloads", {}) if base else {}),
                        **job.get("downloads", {}),
                    }
                    for cid in replace_cids:
                        if cid not in job.get("downloads", {}):
                            downloads.pop(cid, None)
                    for cid, downloaded in list(downloads.items()):
                        try:
                            record = await self.media.local(
                                file_record, Path(downloaded["file"]), "cache", "video", cid
                            )
                            require(
                                record["sha256"] == downloaded.get("sha256")
                                and record["size_bytes"] == downloaded.get("size_bytes"),
                                "download_cache_changed",
                                409,
                            )
                        except (Fault, OSError):
                            require(
                                not base or cid not in base["selected_cids"] or cid in replace_cids,
                                "publication_base_unavailable",
                                409,
                            )
                            downloads.pop(cid)
                    for part in job["snapshot"]["parts"]:
                        if part["cid"] not in job["selected_cids"] or part["cid"] in downloads:
                            continue
                        directory = (
                            Path(self.media.config["staging_directory"])
                            / ".downloads"
                            / identity
                            / str(generation)
                            / part["cid"]
                        )
                        await update(state="downloading", stage="downloading", code="downloading")
                        before = await self.media.connector.resolve(
                            job["bvid"], cookie, with_formats=False
                        )
                        mapping = {value["cid"]: value for value in before["snapshot"]["parts"]}
                        require(part["cid"] in mapping, "source_cid_removed", 409)
                        downloaded = await self.media.engine.download(
                            job, mapping[part["cid"]], cookie, directory
                        )
                        after = await self.media.connector.resolve(
                            job["bvid"], cookie, with_formats=False
                        )
                        require(
                            [
                                (value["cid"], value["index"])
                                for value in before["snapshot"]["parts"]
                            ]
                            == [
                                (value["cid"], value["index"])
                                for value in after["snapshot"]["parts"]
                            ],
                            "source_identity_changed",
                            409,
                        )
                        record = await self.media.local(
                            file_record, Path(downloaded["file"]), "cache", "video", part["cid"]
                        )
                        downloaded.update(
                            sha256=record["sha256"],
                            size_bytes=record["size_bytes"],
                            source_index=mapping[part["cid"]]["index"],
                            source_captured_at=before["snapshot"]["captured_at"],
                        )
                        downloads[part["cid"]] = downloaded
                        await update(
                            downloads=downloads,
                            actual_quality=downloaded["actual_quality"],
                            progress=len(downloads) / len(job["selected_cids"]),
                        )
                await update(state="validating", stage="validating", code="validating")
                metadata = normalize_bilibili(job["snapshot"], overrides=job.get("overrides"))
                missing = [
                    issue.code
                    for issue in metadata.issues
                    if issue.code
                    in {
                        "title_missing",
                        "description_missing",
                        "creator_missing",
                        "author_name_missing",
                    }
                ]
                if missing:
                    await update(
                        state="waiting_metadata",
                        stage="metadata",
                        code="metadata_incomplete",
                        metadata_issues=missing,
                    )
                    return
                package = await self.package(job, target)
                await update(
                    state="metadata_ready",
                    stage="metadata_ready",
                    code="package_ready",
                    package=package,
                    cover_present=package["cover_present"],
                    metadata_changed=False,
                )
            await update(state="publishing", stage="publishing", code="publishing")
            operation = job.get("asset_operation")
            if operation is None:
                operation = await self.media.publisher.prepare(
                    target, job["package"], identity + ":" + str(job.get("edition", 0))
                )
                await update(asset_operation=operation)
            else:
                operation = await self.media.publisher.read(target, operation["operation_id"])
            if operation["status"] == "prepared":
                try:
                    operation = await self.media.publisher.publish(target, operation)
                except Fault as error:
                    if error.code != "publication_unverified":
                        raise
                    operation = await self.media.publisher.read(target, operation["operation_id"])
            for _ in range(15):
                if operation["status"] in {
                    "indexed",
                    "rejected",
                    "cancelled",
                    "failed",
                    "manual_review",
                }:
                    break
                await asyncio.sleep(2)
                operation = await self.media.publisher.read(target, operation["operation_id"])
            await update(asset_operation=operation)
            if operation["status"] == "indexed":
                require(operation.get("receipt") is not None, "publication_unverified", 503)
                manifest = json.loads(base64.b64decode(job["package"]["manifest_base64"]))
                receipt = operation["receipt"]
                require(
                    receipt.get("operation_id") == operation["operation_id"]
                    and receipt.get("package_id") == manifest["package_id"]
                    and receipt.get("library_id") == target["library_id"],
                    "media_peer_invalid_response",
                    503,
                )
                expected = {
                    entry["path"]: (
                        entry["cid"],
                        entry["kind"],
                        entry["sha256"],
                        entry["size_bytes"],
                    )
                    for entry in manifest["files"]
                }
                observed = {
                    entry.get("path"): (
                        entry.get("cid"),
                        entry.get("kind"),
                        entry.get("sha256"),
                        entry.get("size_bytes"),
                    )
                    for entry in receipt["files"]
                    if isinstance(entry, dict)
                }
                require(
                    expected == observed and len(receipt["files"]) == len(expected),
                    "media_peer_invalid_response",
                    503,
                )
                await update(
                    state="asset_indexed",
                    stage="asset_indexed",
                    code="asset_indexed",
                    asset_receipt=operation["receipt"],
                    publication_observed_at=self.media.repo.clock(),
                )
                await self.verify_libraries(job, update, target)
            elif operation["status"] in {"rejected", "failed", "manual_review", "cancelled"}:
                await update(
                    state="unknown" if operation["status"] == "manual_review" else "failed",
                    stage="publishing",
                    code="publication_" + operation["status"],
                )
            else:
                await update(
                    state="retry_wait",
                    stage="publishing",
                    code="publication_pending",
                    retry_delay=30,
                    attempts=job.get("attempts", 0),
                )
        except asyncio.CancelledError:
            current = await self.media.local(repo.job, identity)
            if current and current["generation"] == generation:
                if current["cancel_requested"]:
                    cancelled = True
                    if current.get("asset_operation"):
                        try:
                            operation = await self.media.publisher.read(
                                target, current["asset_operation"]["operation_id"]
                            )
                            operation = await self.media.publisher.cancel(target, operation)
                            cancelled = operation["status"] == "cancelled"
                            if operation.get("receipt"):
                                current = await self.media.local(
                                    repo.update_job,
                                    identity,
                                    {"asset_receipt": operation["receipt"]},
                                    generation=generation,
                                )
                        except Fault:
                            cancelled = False
                    await self.media.local(
                        repo.update_job,
                        identity,
                        {
                            "state": "cancelled" if cancelled else "unknown",
                            "stage": current["stage"],
                            "code": "user_cancelled"
                            if cancelled
                            else "cancel_publication_unverified",
                        },
                        owner=owner,
                        generation=generation,
                    )
                else:
                    await self.media.local(
                        repo.update_job,
                        identity,
                        {
                            "state": "retry_wait",
                            "stage": current["stage"],
                            "code": "runtime_interrupted",
                        },
                        owner=owner,
                        generation=generation,
                    )
            raise
        except Fault as error:
            current = await self.media.local(repo.job, identity)
            if current and current["generation"] == generation and not current["cancel_requested"]:
                auth = error.code in {"auth_required", "account_unavailable", "account_changed"}
                retry = error.code in {
                    "publication_unverified",
                    "media_peer_unavailable",
                    "media_peer_invalid_response",
                    "media_server_unavailable",
                    "source_rate_limited",
                    "source_unavailable",
                    "source_identity_changed",
                    "busy",
                    "download_timeout",
                    "download_failed",
                }
                attempts = job.get("attempts", 0) + 1
                state = (
                    "auth_required"
                    if auth
                    else "retry_wait"
                    if retry and attempts <= 5
                    else "failed"
                )
                await update(
                    state=state,
                    code=error.code,
                    attempts=attempts,
                    retry_delay=min(1800, 30 * 2 ** min(attempts - 1, 6)),
                )
                if auth and current["account_id"]:
                    await self.media.local(
                        self.media.accounts.mark, current["account_id"], "auth_required", error.code
                    )
        except (OSError, ValueError):
            current = await self.media.local(repo.job, identity)
            if current and current["generation"] == generation and not current["cancel_requested"]:
                await update(state="failed", code="media_io_failure")
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)

    async def base(self, job):
        if job.get("base_edition"):
            return job["base_edition"]
        head = await self.media.local(
            self.media.repo.get, "target_head", job["target_id"] + ":" + job["bvid"]
        )
        if head is None:
            return None
        base = await self.media.local(self.media.repo.job, head["job_id"])
        require(
            base is not None and base.get("asset_receipt") and base.get("package"),
            "publication_base_unavailable",
            409,
        )
        return base

    async def cover(self, url):
        if url is None:
            return None
        await self.media.connector.start()
        try:
            async with self.media.connector.session.get(url, allow_redirects=False) as response:
                require(response.status == 200, "cover_unavailable", 503)
                raw = bytearray()
                async for chunk in response.content.iter_chunked(16384):
                    raw.extend(chunk)
                    require(len(raw) <= 32 * 1024**2, "cover_invalid", 503)
                extension = await self.media.local(validate_image, bytes(raw))
                return bytes(raw), extension
        except (TimeoutError, OSError):
            raise Fault("cover_unavailable", 503) from None

    async def package(self, job, target):
        repo = self.media.repo
        layout = job.get("layout", "multipart" if len(job["snapshot"]["parts"]) > 1 else "single")
        base = await self.base(job)
        if base:
            require(base["package"]["layout"] == layout, "layout_change_requires_migration", 409)
        episodes = await self.media.local(
            repo.episode_numbers, job["bvid"], [part["cid"] for part in job["snapshot"]["parts"]]
        )
        metadata = normalize_bilibili(job["snapshot"], overrides=job.get("overrides"))
        extensions = {entry["extension"] for entry in job["downloads"].values()}
        require(len(extensions) == 1, "media_extension_mismatch", 503)
        extension = next(iter(extensions))
        image = await self.cover(job["cover_url"])
        images = [ImageBinding("poster", None, image[1])] if image else []
        if image and layout == "multipart":
            images += [ImageBinding("episode_thumb", cid, image[1]) for cid in job["selected_cids"]]
        request = RenderRequest.build(
            layout=layout,
            selected_cids=job["selected_cids"],
            media_extension=extension,
            episode_numbers={cid: episodes[cid] for cid in job["selected_cids"]}
            if layout == "multipart"
            else {},
            images=images,
        )
        bundle = render_sidecars(metadata, request)
        package_id = str(uuid.uuid4())
        staging_ref = "pkg_" + package_id.replace("-", "")
        root = Path(self.media.config["staging_directory"])
        root.mkdir(parents=True, exist_ok=True)
        temporary = root / (".building_" + package_id)
        sealed = root / staging_ref

        def build():
            temporary.mkdir()
            records = []
            for index, expected in enumerate(bundle.expected_media):
                cid = job["selected_cids"][index]
                destination = temporary / expected.path
                destination.parent.mkdir(parents=True, exist_ok=True)
                original = Path(job["downloads"][cid]["file"])
                require(original.is_file() and not original.is_symlink(), "unsafe_staging", 503)
                try:
                    os.link(original, destination)
                except OSError:
                    shutil.copyfile(original, destination)
                records.append(file_record(destination, expected.path, "video", cid))
            for entry in bundle.files:
                destination = temporary / entry.path
                destination.parent.mkdir(parents=True, exist_ok=True)
                with destination.open("xb") as handle:
                    handle.write(entry.content)
                    handle.flush()
                    os.fsync(handle.fileno())
                cid = next(
                    (cid for cid in job["selected_cids"] if "cid-" + cid + "." in entry.path), None
                )
                records.append(
                    file_record(
                        destination,
                        entry.path,
                        "source" if entry.path == "source.json" else "nfo",
                        cid,
                    )
                )
            if image:
                for referenced in bundle.referenced_images:
                    destination = temporary / referenced.path
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    with destination.open("xb") as handle:
                        handle.write(image[0])
                        handle.flush()
                        os.fsync(handle.fileno())
                    records.append(
                        file_record(destination, referenced.path, referenced.role, referenced.cid)
                    )
            manifest = {
                "schema_version": 1,
                "package_id": package_id,
                "staging_ref": staging_ref,
                "library_id": target["library_id"],
                "provider": "bilibili",
                "bvid": job["bvid"],
                "layout": layout,
                "media_extension": extension,
                "selected_parts": [
                    {"cid": cid, "episode_number": episodes[cid] if layout == "multipart" else None}
                    for cid in job["selected_cids"]
                ],
                "files": records,
            }
            self.media.contract.check(manifest)
            raw = json.dumps(manifest, ensure_ascii=False, separators=(",", ":")).encode()
            os.rename(temporary, sealed)
            return {
                "package_id": package_id,
                "staging_ref": staging_ref,
                "layout": layout,
                "manifest_base64": base64.b64encode(raw).decode(),
                "manifest_digest": hashlib.sha256(raw).hexdigest(),
                "cover_present": bool(image),
            }

        package = await self.media.local(build)
        if base:
            latest = await self.media.publisher.read(
                target, base["asset_operation"]["operation_id"]
            )
            require(latest["status"] == "indexed", "publication_base_unavailable", 409)
            package.update(base_operation_id=latest["operation_id"], base_version=latest["version"])
            if job.get("replace_video_cids"):
                package["replace_video_cids"] = job["replace_video_cids"]
        return package

    async def verify_libraries(self, job, update, target):
        if not target.get("servers"):
            await update(state="published", stage="published", code="published_no_media_servers")
            return
        await update(state="library_verifying", stage="library_verifying", code="library_verifying")
        current = await self.media.local(self.media.repo.job, job["job_id"])
        current["staging_directory"] = self.media.config["staging_directory"]
        results = await self.media.servers.verify(target, current)
        complete = all(result["state"] == "verified" for result in results)
        attempts = current.get("library_attempts", 0) + 1
        await update(
            state="completed" if complete else "retry_wait" if attempts <= 20 else "failed",
            stage="completed" if complete else "library_verifying",
            code="completed"
            if complete
            else "library_verification_pending"
            if attempts <= 20
            else "library_verification_failed",
            library_results=results,
            progress=1 if complete else current.get("progress"),
            library_attempts=attempts,
            retry_delay=min(300, 30 * 2 ** min(attempts - 1, 4)),
        )
