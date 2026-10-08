"""Media composition and lifecycle. The HTTP surface and workers share one ledger."""

import asyncio
import hashlib
import json
import uuid
from pathlib import Path

from jsonschema import Draft202012Validator

from ..contracts import Fault, require
from .accounts import Accounts
from .bilibili import Bilibili
from .config import SOURCE_KINDS, configuration
from .engine import Engine
from .publication import MediaServers, Publisher, Remote, validate_connection
from .repository import Repository
from .rules import RuleEvaluator
from .subscriptions import Subscriptions
from .worker import Worker

JOB_FIELDS = (
    "job_id",
    "subscription_id",
    "media_key",
    "bvid",
    "cid",
    "selected_cids",
    "layout",
    "revision",
    "title",
    "creator",
    "state",
    "stage",
    "code",
    "progress",
    "created_at",
    "updated_at",
    "target_id",
    "quality",
    "actual_quality",
    "cancel_requested",
    "asset_receipt",
    "library_results",
)
RETRYABLE = frozenset({"failed", "auth_required", "unknown", "cancelled", "retry_wait"})


def public_job(job):
    return {
        **{key: job.get(key) for key in JOB_FIELDS},
        "can_retry": job["state"] in RETRYABLE,
        "can_cancel": job["state"]
        not in {"completed", "published", "cancelled", "failed", "unknown"},
    }


class PackageContract:
    def __init__(self, directory):
        root = Path(directory)
        try:
            manifest = json.loads((root / "manifest.json").read_bytes())
            require(manifest["package"] == "media-package/v1", "media_contract_unavailable", 503)
            for filename, digest in manifest["sha256"].items():
                require(
                    "/" not in filename and "\\" not in filename and filename != "..",
                    "media_contract_unavailable",
                    503,
                )
                raw = (root / filename).read_bytes().replace(b"\r\n", b"\n")
                require(
                    hashlib.sha256(raw).hexdigest() == digest, "media_contract_unavailable", 503
                )
            self.validator = Draft202012Validator(json.loads((root / "schema.json").read_bytes()))
        except (OSError, KeyError, ValueError):
            raise Fault("media_contract_unavailable", 503) from None

    def check(self, document):
        require(not list(self.validator.iter_errors(document)), "invalid_media_package", 503)


def validate_media(settings):
    config = configuration(settings)
    if config is None:
        return None
    for target in config["targets"].values():
        require(isinstance(target.get("library_id"), str), "invalid_input", 400)
        if target.get("publisher"):
            validate_connection(target["publisher"])
        for server in target.get("servers", []):
            validate_connection(server)
            require(
                server.get("kind") in {"emby", "jellyfin"}
                and isinstance(server.get("server_id"), str)
                and isinstance(server.get("library_id"), str)
                and isinstance(server.get("media_root"), str),
                "invalid_input",
                400,
            )
    PackageContract(config["package_contract_directory"])
    return config


class Media:
    def __init__(self, settings, *, clock, local):
        self.config = validate_media(settings)
        self.local = local
        self.started = False
        self.tasks = []
        self.previews = {}
        self.qr = {}
        self.wake = asyncio.Event()
        if self.config is None:
            self.repo = None
            return
        self.contract = PackageContract(self.config["package_contract_directory"])
        self.repo = Repository(settings["database_path"] + ".media.sqlite", clock=clock)
        self.accounts = Accounts(self.repo)
        self.connector = Bilibili(clock=clock)
        self.engine = Engine(self.config["engine"])
        self.remote = Remote()
        self.publisher = Publisher(self.remote)
        self.servers = MediaServers(self.remote)
        self.evaluator = RuleEvaluator()
        self.subscriptions = Subscriptions(self)
        self.worker = Worker(self)

    async def start(self):
        if self.config is None or self.started:
            return
        self.started = True
        await self.connector.start()
        await self.engine.check()
        self.tasks = [asyncio.create_task(self.subscriptions.pump())]
        self.tasks += [
            asyncio.create_task(self.worker.pump(index)) for index in range(self.config["workers"])
        ]

    async def close(self):
        if self.config is None:
            return
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks = []
        await self.evaluator.aclose()
        await self.connector.close()
        await self.remote.close()
        self.started = False

    async def view(self):
        if self.config is None:
            return {
                "configured": False,
                "capabilities": {"provider": "bilibili", "source_kinds": list(SOURCE_KINDS)},
                "engine": {
                    "state": "not_configured",
                    "version": None,
                    "code": "media_not_configured",
                },
                "accounts": [],
                "targets": [],
                "subscriptions": [],
                "jobs": [],
            }
        from .accounts import public_account

        accounts = await self.local(self.repo.list, "account")
        subscriptions = await self.local(self.repo.list, "subscription")
        jobs, _ = await self.local(self.repo.jobs)
        return {
            "configured": True,
            "capabilities": {"provider": "bilibili", "source_kinds": list(SOURCE_KINDS)},
            "engine": dict(self.engine.status),
            "accounts": [public_account(account) for account in accounts],
            "targets": [
                {
                    "target_id": identity,
                    "label": target["label"],
                    "state": "ready" if target.get("publisher") else "publisher_not_configured",
                    "servers": [
                        {
                            "server_id": server["server_id"],
                            "label": server.get("label", server["server_id"]),
                            "kind": server["kind"],
                        }
                        for server in target.get("servers", [])
                    ],
                }
                for identity, target in self.config["targets"].items()
            ],
            "subscriptions": subscriptions,
            "jobs": [public_job(job) for job in jobs],
        }

    async def resolve(self, value, cookie):
        resolved = await self.connector.resolve(value, cookie, with_formats=False)
        for part in resolved["video"]["parts"]:
            directory = Path(self.config["staging_directory"]) / ".probes" / str(uuid.uuid4())
            part["formats"] = await self.engine.formats(
                resolved["video"]["bvid"], part, cookie, directory
            )
        current = await self.connector.resolve(
            resolved["video"]["bvid"], cookie, with_formats=False
        )
        require(
            [(part["cid"], part["index"]) for part in current["snapshot"]["parts"]]
            == [(part["cid"], part["index"]) for part in resolved["snapshot"]["parts"]],
            "source_identity_changed",
            409,
        )
        return resolved

    def task_record(self, job):
        return {
            "task_id": "media:" + job["job_id"],
            "state": job["state"],
            "created_at": job["created_at"],
            "updated_at": job["updated_at"],
            "document": public_job(job),
        }

    def history(self, *, after=None, limit=50, states=None):
        if self.repo is None:
            return [], False
        jobs, more = self.repo.jobs(after=after, limit=limit, states=states)
        return [self.task_record(job) for job in jobs], more

    def record(self, identity):
        if self.repo is None:
            return None
        job = self.repo.job(identity)
        return self.task_record(job) if job else None
