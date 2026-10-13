# Historical import NFO maintenance

`media.import_metadata` is an operator Python API. It prepares candidates in writable Platform data; it never writes the configured import root, changes import/source validity, creates jobs, or calls video download. Existing `Imports` observations and raw ffprobe streams are reused. A changed file identity is rejected; absent probe data or a changed ctime requires a new probe. QN/hash are included only from an existing verified import with the complete unchanged observation. Unknown QN remains unknown.

## Prepare and resume

Create the ordinary `Media` instance with current settings and its local thread runner. Do **not** call `Media.start()` for maintenance. Existing connector/remote sessions open lazily and `await media.close()` closes them.

```python
plan = await media.import_metadata.prepare(
    root_id="legacy-bilibili",
    plan_id="legacy-nfo-20261013",
    output_directory="/var/lib/tianshu/platform.media-maintenance/legacy-nfo-20261013/candidates",
    resolved_by_bvid=sources["resolved"],
    source_errors=sources.get("errors", {}),
    bvids=None,                   # or one batch of BV identifiers
    request_interval=1.0,        # seconds between missing-source request starts
)
```

The supplied map contains the existing `Bilibili.resolve(..., with_formats=False)` return for each BV, including `snapshot` and `creator_images`. Each snapshot is validated against **all** registered CIDs of that BV. Supplied results precede persisted per-plan results, which precede new public connector calls. An explicit `source_errors[BV]` prevents a new request only if no usable supplied/persisted result exists. Network requests are deduplicated per BV; avatars reuse the existing persistent MID cache. No cookie is required by this API.

A trusted historical fallback can additionally contain `source_kind="bili_sync_history"`, `source_digest` and `source_warning="current_source_unavailable"` or `"current_source_parts_changed"`. It must still contain the original CIDs and pass normal snapshot validation. These facts appear in the NFO and plan; the fallback is usable without claiming a current Bilibili response.

Use the same plan ID, root and output directory to resume a batch, interrupted process or source failure. Previous entries and videos are retained across batches. Process/OS locking covers a plan ID even if competing callers supply different output directories. Cancellation preserves completed documents, and pending local writes retain the lock until the thread finishes. Candidate bytes are content addressed and atomically saved. Resume reconstructs candidates from documents after an interrupted JSON export. A plan ID is one preparation/application edition; use a new ID for a later metadata edition or intentional change to existing original NFOs.

Discovery uses registered ordinary videos, their same-basename NFO (or `movie.nfo` in a single-video directory), and nearest parent `tvshow.nfo`. It does not scan orphan sidecars, Synology `@eaDir`/`#recycle`, links or unknown media. A missing episode NFO may be prepared only when registered BV/CID/evidence/part index agree with the actual `SxxEyy` filename and `Season N` directory. Its adjacent NFO and absent series parent's `tvshow.nfo` have `action="create"` and `original_sha256=null`. A same-BV registered show template preserves the series title and unrelated fields. The new episode title comes from that exact CID; no cross-directory orphan is treated as the video's original. An unexpected file appearing at a creation path is a conflict, unless it is exactly the already applied candidate.

## Returned plan and application

The schema version 1 plan has:

- `plan_id`, `root_id`, `root_directory`, `output_directory`, `plan_file`, creation/update times and `state`: `completed`, `partial` or `interrupted`.
- `counts`: `files`, `videos`, `ready`, `partial`, `failed`, `pending`, `unchanged`, `already_applied`, `discovery_failures`.
- `files[]`: `entry_id`, `nfo_relative_path`, `type` (`movie`, `episodedetails`, `tvshow`), `action`, `bvid`, `cid` (null for show), `cids`, `video_relative_paths`, `video_observations`, `original_sha256`, `after_sha256`, `content_base64`, `candidate_file`, `state`, `code`, `warnings`, metadata source facts, snapshot and avatar observations. A template-based new show also includes its template path and SHA.
- `videos[]`: `import_id`, `relative_path`, complete `observation` (`dev`, `ino`, `size_bytes`, `mtime_ns`, `ctime_ns`), `import_state`, `import_code`, `actual_quality`, original expected and actual local durations.
- `discovery_failures[]`: original video relative path, BV and exact failure code. Such files are not silently counted as successful.

`ready` means a usable candidate, **not** media validity or Emby verification. Unknown QN and the pre-existing duration mismatch remain explicit warnings without blocking known author/description additions. `partial` candidates preserve existing valid plot/actors when source metadata is unavailable; they can still carry actual local streams, but do not claim complete source enrichment. `failed`/`pending` entries have no applicable candidate. `already_applied` retains the original before SHA and candidate when the file already equals the planned after SHA.

The coordinator applies ready/unchanged candidates with the independent NAS executor. Its input can be extracted as:

```python
payload = {
    "root_id": plan["root_id"],
    "files": [
        {"relative_path": entry["nfo_relative_path"],
         "before_sha256": entry["original_sha256"],
         "after_sha256": entry["after_sha256"],
         "content_base64": entry["content_base64"]}
        for entry in plan["files"] if entry["state"] in {"ready", "unchanged"}
    ],
    "videos": [{"relative_path": row["relative_path"], "observation": row["observation"]}
               for row in plan["videos"]],
}
```

The executor must recheck video observations, verify each before SHA (or require a creation path to be absent), back up old NFOs and atomically replace only the listed NFOs. Product code does not grant source-directory write access. Missing/different original NFOs and changed videos require investigation, not forced application. Candidate documents and files are private maintenance data and contain local paths; keep them outside Git. Content-addressed unused versions are intentionally retained for interrupted/reviewed applications.

## NFO additions

Existing title, layout, season/episode, cover, actors/roles/thumbs, unrelated XML and existing uniqueids remain. Both `plot` and `outline` receive a nonempty trusted description. Uploader Directors use the shared MID-qualified display name; original creator names remain in `tianshu/creators`. No old actor is deleted based on its name or arbitrary role.

Profile `bilibili-import-metadata-v1` adds `tianshu/source` with provider/BV/CID, known QN/hash/size, `quality_state`, original import state/code, original/current source and actual local durations, file observations and metadata source kind/digest/warning/capture time. Shows have a `parts/part` for each associated CID. Video NFOs get actual `fileinfo/streamdetails` through the existing renderer; shows receive no P's streamdetails. Resolution/codec/HDR observations do not imply a Bilibili QN. The one duration-invalid import stays invalid and is never turned into a reusable media source.

## Observe Emby

After the coordinator adds the exact historical path to the existing library and applies NFOs:

```python
report = await media.import_metadata.verify(
    "legacy-nfo-20261013",
    server_id="configured-server-id",
    library_id="25136",
    server_root="/video/存储库/短视频/manual/bilibili",
    refresh=True,
)
```

The server must be an existing authenticated Emby connection. The explicit trusted historical `server_root` is used instead of any new-media target root. This method issues ordinary library refresh, lists the specified library with bounded pagination, and matches **exact path and item type**, not the newer ProviderId format. It checks the applied NFO SHA and video observations, title/overview, typed MID-qualified Directors, and Emby's actual video/audio codec/dimensions/channels. It reuses `verify_creator_images`: only the Director ID attached to the matched video/show can receive an authenticated image upload and readback; unchanged person images reuse the result. File/scope observations are checked again after network work.

`report.state` is `verified`, `partial` or `unavailable`; `files[]` records entry/path, item ID, director-image outcomes and an exact code. A refresh can be asynchronous: rerun verification after Emby finishes. Missing streams, items, avatars, mismatches or unapplied candidates remain pending. NFO streamdetails are exported observations; this check does not claim Emby reads every optional NFO technical field or that playback quality is a QN label.

## Query durable facts

```python
plan = await media.import_metadata.plan("legacy-nfo-20261013")
sources = await media.local(media.repo.list, "import_metadata_source")
entries = await media.local(media.repo.list, "import_metadata_entry")
reports = await media.local(media.repo.list, "import_metadata_verification")
inventory = await media.local(media.repo.list, "media_import")
media_sources = await media.local(media.repo.list, "media_source")
```

Plan/source/entry/verification documents use existing schema 2 storage. Original `media_import`, `media_source`, jobs, memberships, episodes, download_sources and publication receipts are untouched. Image preparation may update `creator_avatar`, as in ordinary managed metadata refresh.
