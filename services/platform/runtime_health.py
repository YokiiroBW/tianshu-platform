"""Read-only liveness, readiness and preflight for a platform deployment.

Every answer here is derived by *reading*: a database opened `mode=ro` with `query_only`, a
certificate decoded in place, a directory listing. Nothing is created, migrated, written, ticked,
refreshed or called. That is deliberate - a probe that repairs what it measures cannot be trusted
to report a fault, and a probe that creates a missing database cannot tell a fresh deployment
from a broken one.

The module owns the meaning of readiness and nothing else. It is handed a narrow frozen
description of the deployment by the composition root, so it never reaches into a mutable
Platform, a console or a store, and a business module never depends on it.
"""

import asyncio
import os
import re
import sqlite3
import ssl
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from . import diagnostics_config
from .contracts import Fault
from .diagnostics import SERVICE, safe_code
from .web_account import inspect_account

# A closed set of local checks. The readiness body is exactly this mapping, so a probe can never
# grow a field that leaks a path, an address or a configured value.
CHECK_KEYS = (
    "config",
    "contract",
    "store",
    "sidecars",
    "web_static",
    "tls",
    "credentials",
    "logging",
    "runtime",
)
CHECK_VALUES = ("ok", "failed", "not_configured", "not_verified", "non_durable")
# Only these stop readiness. `not_configured` means the deployment deliberately has no such
# capability; `not_verified` means this product could not establish the fact read-only, which must
# never be reported as reachable.
BLOCKING = ("failed", "not_verified", "non_durable")

BUDGET_SECONDS = 2.0
CACHE_SECONDS = 1.0
SQLITE_TIMEOUT = 0.25
STORE_VERSIONS = (0, 1, 2)
STORE_TABLES = ("origins", "configs", "native_configs", "authority_head", "audit")
# A sidecar is only required when the capability that owns it is enabled, and each one names the
# table its own module creates, so "present" means the module's schema, not merely a file.
SIDECAR_TABLES = {
    "web-inputs": "submissions",
    "web-replies": "replies",
    "web-models": "publication_intents",
    "home-controls": "control_intents",
    "bots": "connections",
}
ASSET_REFERENCE = re.compile(r'(?:src|href)="(/[^"]+\.(?:js|css))"')
CERTIFICATE_TIME = "%b %d %H:%M:%S %Y %Z"


@dataclass(frozen=True)
class HealthInputs:
    """A narrow, immutable description of what this deployment actually turned on.

    Only names, paths and booleans: never a configured value, a credential, an address or a
    callback into the business. Two callables report live process state, and both are read-only.
    """

    mode: str
    config_problem: str | None = None
    contract_directory: str | None = None
    expected_manifest_sha256: str | None = None
    database_path: str | None = None
    web_static_directory: str | None = None
    sidecars: tuple[tuple[str, str], ...] = ()
    tls: dict | None = None
    credential_names: tuple[str, ...] = ()
    credential_present: object = None
    log_directory: str | None = None
    log_directory_bytes: int | None = None
    log_state: object = None
    runtime_state: object = None
    web_account_enabled: bool = False


def _enabled_section(settings, name):
    """Whether one optional reviewed section turned its capability on."""
    section = settings.get(name)
    return isinstance(section, dict) and section.get("enabled") is True


def health_inputs(
    settings,
    *,
    credential_names=(),
    credential_present=None,
    log_state=None,
    runtime_state=None,
    config_problem=None,
):
    """Derive the description from the deployment settings, without constructing any service."""
    settings = settings or {}
    mode = settings.get("mode")
    database_path = settings.get("database_path")
    database_path = database_path if isinstance(database_path, str) else None
    web = settings.get("web")
    web = web if isinstance(web, dict) else None
    static = web.get("static_directory") if web else None
    sidecars = ()
    if database_path:
        # Only the ledgers this deployment actually owns are asked about. The console's own two
        # exist wherever the console does; the other two belong to optional capabilities, so a
        # deployment without a registered household or without model management simply has none -
        # that is a configuration choice, and demanding one would report a fault that is not there.
        owned = []
        if web is not None:
            owned.extend(
                [(".web-inputs.sqlite", "web-inputs"), (".web-replies.sqlite", "web-replies")]
            )
            if _enabled_section(settings, "web_models"):
                owned.append((".web-models.sqlite", "web-models"))
            if _enabled_section(settings, "home"):
                owned.append((".home-controls.sqlite", "home-controls"))
        if settings.get("bot_connections") is not None:
            owned.append((".bots.sqlite", "bots"))
        sidecars = tuple((database_path + suffix, kind) for suffix, kind in owned)
    tls = settings.get("tls")
    tls = tls if isinstance(tls, dict) else None
    return HealthInputs(
        mode=mode,
        config_problem=config_problem,
        contract_directory=diagnostics_config.resolve_contract_directory(settings),
        expected_manifest_sha256=diagnostics_config.resolve_manifest_sha256(settings),
        database_path=database_path,
        web_static_directory=static if isinstance(static, str) else None,
        sidecars=sidecars,
        tls=tls,
        credential_names=tuple(credential_names),
        credential_present=credential_present,
        log_directory=diagnostics_config.resolve_log_directory(settings),
        log_directory_bytes=diagnostics_config.resolve_log_directory_bytes(settings),
        log_state=log_state,
        runtime_state=runtime_state,
        web_account_enabled=settings.get("web_account") is not None,
    )


def _readonly_uri(path):
    return "file:" + quote(Path(path).resolve().as_posix(), safe="/:") + "?mode=ro"


def inspect_sqlite(path, tables, versions):
    """Read one existing database without writing, migrating or locking it for writing.

    `mode=ro` plus `PRAGMA query_only` means even an accidental statement cannot take a write
    lock, and the short busy timeout keeps a probe from queueing behind a busy writer. A WAL
    database whose sidecar files are unreadable is reported as unreadable rather than assumed
    healthy, and `immutable` is never used: it would let a probe read a stale snapshot as current.
    """
    if not path or not os.path.isfile(path):
        return "missing", None
    database = None
    try:
        database = sqlite3.connect(_readonly_uri(path), uri=True, timeout=SQLITE_TIMEOUT)
        database.execute("PRAGMA query_only=ON")
        database.execute(f"PRAGMA busy_timeout={int(SQLITE_TIMEOUT * 1000)}")
        version = database.execute("PRAGMA user_version").fetchone()[0]
        if version not in versions:
            return "unsupported_version", version
        names = {
            row[0] for row in database.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if not names:
            return "not_initialized", version
        if any(table not in names for table in tables):
            return "missing_tables", version
        return "ok", version
    except sqlite3.Error:
        return "unreadable", None
    finally:
        if database is not None:
            database.close()


def inspect_directory(directory):
    """A purely static look at a directory: existence, kind and the permission bits it carries.

    Whether a write would actually succeed cannot be established by looking, so this reports only
    what it can see and callers must not turn it into a stronger claim.
    """
    if not directory:
        return "not_configured", None
    path = Path(directory)
    if not path.exists():
        return "missing", None
    if not path.is_dir():
        return "not_a_directory", None
    writable = os.access(path, os.W_OK)
    return ("ok" if writable else "read_only"), writable


def inspect_static(directory):
    """The built entry point and every asset it references must actually be on disk."""
    if not directory:
        return "not_configured"
    root = Path(directory)
    index = root / "index.html"
    if not index.is_file():
        return "missing_index"
    try:
        html = index.read_text(encoding="utf-8", errors="strict")
    except (OSError, UnicodeError):
        return "unreadable_index"
    references = ASSET_REFERENCE.findall(html)
    if not references:
        return "missing_build_assets"
    resolved = root.resolve()
    for reference in references:
        target = (root / reference.lstrip("/")).resolve()
        if not target.is_relative_to(resolved) or not target.is_file():
            return "missing_build_assets"
    return "ok"


def certificate_window(path):
    """The certificate's own validity window, decoded in place and read-only.

    Python's decoding helper is what makes this possible without adding a cryptography dependency
    to the runtime. Where it is unavailable the answer is "not verified" - never an assumption.
    """
    decode = getattr(getattr(ssl, "_ssl", None), "_test_decode_cert", None)
    if decode is None:
        return None
    try:
        decoded = decode(str(path))
        not_before = datetime.strptime(decoded["notBefore"], CERTIFICATE_TIME).replace(
            tzinfo=timezone.utc
        )
        not_after = datetime.strptime(decoded["notAfter"], CERTIFICATE_TIME).replace(
            tzinfo=timezone.utc
        )
    except (KeyError, ValueError, OSError, ssl.SSLError):
        return None
    return not_before.timestamp(), not_after.timestamp()


def inspect_tls(tls, now=None):
    """Load the serving certificate and prove it covers the current moment.

    Loading proves the file is a certificate, that it parses and that the private key matches it -
    a real cryptographic check on the bytes, not a stat. The window is then read from the
    certificate itself, because a chain that cannot be trusted for *now* is not a working
    production entry point. Peer trust still has to be established by a real connection.
    """
    if not isinstance(tls, dict) or set(tls) != {"certificate_file", "private_key_file"}:
        return "failed"
    certificate, key = tls["certificate_file"], tls["private_key_file"]
    if not all(
        isinstance(value, str) and Path(value).is_absolute() for value in (certificate, key)
    ):
        return "failed"
    if not (os.path.isfile(certificate) and os.path.isfile(key)):
        return "failed"
    try:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(certificate, key)
    except (OSError, ssl.SSLError):
        return "failed"
    window = certificate_window(certificate)
    if window is None:
        return "not_verified"
    not_before, not_after = window
    moment = time.time() if now is None else now
    return "ok" if not_before <= moment <= not_after else "failed"


class Probe:
    """Readiness with a hard budget, one real check at a time, and no expired green.

    Checks run off the event loop under a single owner, so concurrent probes share one pass. The
    owner is held until the underlying check *really* finishes, not merely until the caller's wait
    expires: cancelling a wait does not cancel a thread, and a second pass started on top of a
    still-running first one is exactly how a read-only probe turns into an unbounded pile of
    concurrent work. A caller that arrives while a check is in flight gets the unexpired cache if
    there is one and the closed `not_verified` document otherwise - never a queued wait, and never
    a timeout that quietly replaces a good answer with a worse one.
    """

    def __init__(
        self, inputs, *, clock=time.monotonic, budget=BUDGET_SECONDS, cache_seconds=CACHE_SECONDS
    ):
        self.inputs = inputs
        self.clock = clock
        self.budget = budget
        self.cache_seconds = cache_seconds
        self._inflight = False
        self._cached = None
        self._cached_at = None
        # When the check that is currently in flight started. A result is a fact about the moment it
        # was measured, so this - not the moment it happened to arrive - is the age the cache is
        # judged by. Without it, a check that ran past the budget would come back and be cached as
        # brand new, which is how a stale snapshot turns into a fresh green.
        self._started_at = None

    # -- liveness ---------------------------------------------------------------------------

    def live(self):
        """Alive means this event loop answered. It says nothing about business capability."""
        return {"status": "alive"}

    # -- readiness --------------------------------------------------------------------------

    async def ready(self):
        """The closed readiness document: `status`, `service` and the local checks only."""
        cached = self._fresh()
        if cached is not None:
            return cached
        if self._inflight:
            # One real check is already running and has not finished. This caller neither queues
            # behind it nor starts a second one: "not verified right now" is the honest answer, and
            # it is the only one that stays inside the budget without adding work.
            return self._document(dict.fromkeys(CHECK_KEYS, "not_verified"))
        self._inflight = True
        self._started_at = self.clock()
        loop = asyncio.get_running_loop()
        worker = loop.run_in_executor(None, self.evaluate)
        worker.add_done_callback(self._settle)
        try:
            # Shielded on purpose: the budget bounds *this* answer, and the check itself is left to
            # finish so that ownership is released by the work rather than by the timeout.
            document = await asyncio.wait_for(asyncio.shield(worker), self.budget)
        except (TimeoutError, asyncio.TimeoutError):
            return self._document(dict.fromkeys(CHECK_KEYS, "not_verified"))
        return document

    def _settle(self, worker):
        """Release the single owner, and cache the answer against the moment it was measured.

        A late result is still a result - the check really ran - but it is not a *current* one. It
        is therefore cached with the time its check started, so a snapshot older than the cache TTL
        is stale the instant it arrives and the next caller starts a real check instead of being
        handed an old answer wearing a new timestamp. Releasing the owner is what lets that next
        check run at all, and it happens whatever the outcome was.
        """
        started_at, self._started_at = self._started_at, None
        self._inflight = False
        if worker.cancelled():
            return
        error = worker.exception()
        if error is not None:
            # A check that raised is not a fact about the deployment, and it never replaces the
            # cached answer: the next probe simply asks again.
            return
        self._cached = worker.result()
        self._cached_at = self.clock() if started_at is None else started_at

    def _fresh(self):
        if self._cached is None or self._cached_at is None:
            return None
        if self.clock() - self._cached_at > self.cache_seconds:
            return None
        return self._cached

    def _document(self, checks):
        blocked = any(value in BLOCKING for value in checks.values())
        return {
            "status": "not_ready" if blocked else "ready",
            "service": SERVICE,
            "checks": checks,
        }

    def evaluate(self):
        """Run every local read-only check once and return the readiness document."""
        inputs = self.inputs
        checks = {
            "config": "failed" if inputs.config_problem else "ok",
            "contract": self._contract(),
            "store": self._store(),
            "sidecars": self._sidecars(),
            "web_static": self._static(),
            "tls": self._tls(),
            "credentials": self._credentials(),
            "logging": self._logging(),
            "runtime": self._runtime(),
        }
        return self._document(checks)

    def _contract(self):
        if self.inputs.contract_directory is None:
            # A production entry point must know which contract it was reviewed against.
            return "ok" if self.inputs.mode != "service_https" else "failed"
        try:
            diagnostics_config.load_contract(
                self.inputs.contract_directory, self.inputs.expected_manifest_sha256
            )
        except diagnostics_config.ContractProblem:
            return "failed"
        return "ok"

    def _store(self):
        if not self.inputs.database_path:
            return "failed"
        state, _ = inspect_sqlite(self.inputs.database_path, STORE_TABLES, STORE_VERSIONS)
        # A running service whose authority store is missing or unmigrated is not ready; the
        # first-boot distinction belongs to preflight, which is allowed to say so instead.
        return "ok" if state == "ok" else "failed"

    def _sidecar_states(self):
        """One read-only state per enabled sidecar, so preflight can tell "not yet" from "wrong"."""
        states = []
        for path, kind in self.inputs.sidecars:
            table = SIDECAR_TABLES.get(kind)
            states.append(inspect_sqlite(path, (table,) if table else (), (0, 1))[0])
        if self.inputs.web_account_enabled and self._account_state() != "ok":
            states.append("failed")
        return states

    def _account_state(self):
        # Only local account data is inspected. A genuinely fresh installation is an operable
        # setup page; missing data after the seal exists is a failure, never fresh setup.
        try:
            inspect_account(self.inputs.database_path)
        except (Fault, OSError, ValueError, TypeError):
            return "failed"
        return "ok"

    def _sidecars(self):
        if not self.inputs.sidecars:
            return "not_configured"
        return "ok" if all(state == "ok" for state in self._sidecar_states()) else "failed"

    def _static(self):
        if self.inputs.web_static_directory is None:
            return "not_configured"
        return "ok" if inspect_static(self.inputs.web_static_directory) == "ok" else "failed"

    def _tls(self):
        if self.inputs.mode != "service_https":
            # Loopback rehearsal is not a production entry point and has no certificate to check.
            return "not_configured"
        return inspect_tls(self.inputs.tls)

    def _credentials(self):
        names = self.inputs.credential_names
        if not names:
            return "not_configured"
        present = self.inputs.credential_present
        if present is None:
            return "not_verified"
        return "ok" if all(present(name) for name in names) else "failed"

    def _logging(self):
        state = self.inputs.log_state
        if state is None:
            return "not_verified"
        value, _ = state()
        if value == "durable":
            return "ok"
        if value == "non_durable":
            # Explicit development output is not a durable sink, so it cannot back readiness.
            return "non_durable"
        # Everything else - unavailable, and `recovering`, which is a sink that has not yet proved
        # it works again - is a log that cannot be relied on, and readiness says so.
        return "failed"

    def _runtime(self):
        state = self.inputs.runtime_state
        if state is None:
            return "not_verified"
        return "ok" if state() == "running" else "failed"


def preflight(settings, *, credential_names=(), credential_present=None, validate=None):
    """Answer "would this deployment boot safely?" without starting or creating anything.

    The composition root is deliberately *not* constructed here: that would create missing
    databases and run migrations, which is exactly what a preflight must not do. A first boot on
    an empty data directory is reported as `requires_initialization` so the operator runs the
    normal startup path instead of a probe quietly inventing a database.

    `validate` is the entry point's own pre-store validation, injected rather than re-implemented:
    the rolling check therefore asks the *same* question the real startup asks, and a settings
    file the entry point would refuse can never be reported as ready. The validator reads
    contracts and validates identities and page narrowing, and opens nothing - no store, no
    database, no port - so passing it does not weaken the read-only promise above.
    """
    inputs = health_inputs(
        settings, credential_names=credential_names, credential_present=credential_present
    )
    reasons = []
    checks = {}

    problem = inputs.config_problem
    if problem is None and validate is not None:
        try:
            validate(settings)
        except Fault as exc:
            # Only the fixed code travels: never the offending value, the path or the exception.
            problem = safe_code(exc.code)
        except (ValueError, TypeError, KeyError, OSError, RecursionError):
            problem = "config_load_failed"
    if problem:
        checks["config"] = "failed"
        reasons.append(problem)
    else:
        checks["config"] = "ok"

    probe = Probe(inputs)
    checks["contract"] = probe._contract()
    if checks["contract"] != "ok" and inputs.contract_directory is not None:
        reasons.append("contract_not_verified")

    requires_initialization = False
    if not inputs.database_path:
        checks["store"] = "failed"
        reasons.append("database_path_not_configured")
    else:
        state, _ = inspect_sqlite(inputs.database_path, STORE_TABLES, STORE_VERSIONS)
        if state == "missing":
            checks["store"] = "not_configured"
            requires_initialization = True
            reasons.append("requires_initialization")
        elif state == "not_initialized":
            checks["store"] = "not_configured"
            requires_initialization = True
            reasons.append("requires_initialization")
        elif state == "ok":
            checks["store"] = "ok"
        else:
            checks["store"] = "failed"
            reasons.append("store_" + state)

    # Sidecars are created by the same boot as the authority store, so a deployment that has not
    # booted yet simply has none. A sidecar that exists with a schema this build cannot read is a
    # different fact and is reported as such.
    sidecar_states = probe._sidecar_states()
    if not sidecar_states:
        checks["sidecars"] = "not_configured"
    elif all(state == "missing" for state in sidecar_states):
        checks["sidecars"] = "not_configured"
        requires_initialization = True
        if "requires_initialization" not in reasons:
            reasons.append("requires_initialization")
    elif all(state == "ok" for state in sidecar_states):
        checks["sidecars"] = "ok"
    else:
        checks["sidecars"] = "failed"
        reasons.append("sidecar_schema_unsupported")
    checks["web_static"] = probe._static()
    if checks["web_static"] == "failed":
        reasons.append("web_static_incomplete")
    checks["tls"] = probe._tls()
    if checks["tls"] in ("failed", "not_verified"):
        reasons.append("tls_not_usable")
    checks["credentials"] = probe._credentials()
    if checks["credentials"] == "failed":
        reasons.append("credential_not_registered")

    # A deployment that still needs its first normal boot is not "ready" in any useful sense,
    # even though nothing about it is broken: the operator has an action to take.
    ok = not requires_initialization and not any(value in BLOCKING for value in checks.values())
    return {
        "status": "ready" if ok else "not_ready",
        "service": "platform",
        "checks": checks,
        "reasons": sorted(set(reasons)),
        "requires_initialization": requires_initialization,
    }
