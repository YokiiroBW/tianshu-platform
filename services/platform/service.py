"""Composition root for the two published endpoints and trusted local adapter ports."""

import time

from . import diagnostics, diagnostics_config, runtime_health
from .auth import Auth, secret
from .assets import Assets
from .contracts import Contracts, Fault, require
from .models import Models
from .origins import Origins
from .persona_client import PersonaClient
from .persona_page_config import connections as persona_connections
from .persona_page_config import page_configuration
from .projections import Projections
from .storage import Store
from .sources import Sources


def log_state():
    """The durable sink's live state, or the honest development answer when there is none.

    Read-only: it reports what the adapter already knows and never attempts a write, so asking
    about the log can never itself change the log.
    """
    sink = diagnostics.active()
    if sink is None:
        return diagnostics.NON_DURABLE, None
    return sink.state, sink.error


def registered_credentials(settings):
    """Every environment variable another registered identity or peer already reads.

    The persona page's credential is its own: reusing the browser operator's, a service principal's,
    the dialogue Core's, an asset library's or the device connection's variable would let one
    identity read as another. This is the *name* half of that rule; the value half is checked where
    the value is resolved, because two names can point at one secret.
    """
    names = set()

    def take(item, key="token_env"):
        if isinstance(item, dict) and isinstance(item.get(key), str):
            names.add(item[key])

    for principal in (settings.get("principals") or {}).values():
        take(principal)
    take(settings.get("core"))
    take(settings.get("home"))
    for section in ("asset_connections", "providers"):
        items = settings.get(section)
        if isinstance(items, dict):
            for item in items.values():
                take(item)
    return tuple(sorted(names))


class Platform:
    def __init__(self, settings, *, clock=time.time):
        require(
            set(settings)
            <= {
                "mode",
                "storage",
                "database_path",
                "contract_directory",
                "principals",
                "entries",
                "providers",
                "config_max_lifetime_seconds",
                "source_contract_directory",
                "input_entries",
                "core",
                "tls",
                "asset_connections",
                "web",
                "web_models",
                "web_assets",
                "native_config_http",
                "home",
                "persona_connections",
                "web_personas",
                "diagnostics",
            },
            "invalid_input",
            400,
        )
        require(settings["storage"] == "sqlite_local", "dependency_unavailable", 503)
        # The native snapshot HTTP port stays closed unless the deployment explicitly opens it.
        self.native_config_http = settings.get("native_config_http", False)
        require(type(self.native_config_http) is bool, "invalid_input", 400)
        self.contracts = Contracts(
            settings["contract_directory"], settings.get("source_contract_directory")
        )
        self.auth = Auth(settings, self.contracts)
        self.store = Store(settings["database_path"])
        self.origins = Origins(self.store, self.auth, self.contracts, clock)
        self.sources = Sources(self.store, self.auth, self.contracts, self.origins, settings, clock)
        self.origins.input_entries = self.sources.entries
        self.settings = settings
        self.models = Models(self.store, self.auth, self.contracts, self.origins, settings, clock)
        self.projections = Projections(self.store, self.auth, self.contracts, clock)
        self.assets = Assets(self.store, self.auth, settings)
        # A read-only page that could widen this identity's own bindings is refused at startup.
        self.assets.configure_page(settings.get("web_assets"), self.contracts)
        # The persona page reads one registered character service through its own deployment
        # credential. The credential variable is its own: it is never a value another service
        # identity, or the browser's own operator, already holds.
        self.other_credentials = registered_credentials(settings)
        # The readiness credential is its own identity. Reading it from a variable a business
        # principal, the browser operator or a peer already holds would let one identity answer
        # readiness as another, so the name half of that rule is refused here at assembly.
        require(
            diagnostics_config.resolve_ready_token_env(settings) not in self.other_credentials,
            "invalid_input",
            400,
        )
        self.persona_connections = persona_connections(
            settings.get("persona_connections"),
            self.contracts.check,
            self.other_credentials,
        )
        # A misconfigured or unverifiable persona page is fatal here rather than a surprise at
        # read time; a disabled one is simply a deployment without this page.
        self.personas = (
            page_configuration(
                settings["web_personas"],
                self.persona_connections,
                self.auth.mode,
                self.contracts.check,
            )
            if settings.get("web_personas") is not None
            else None
        )
        self.auth.activate(self.store, clock)
        # Everything the read-only probes are allowed to know about this deployment, assembled
        # once as a frozen description. The health module never receives this object, a store or a
        # console, and nothing here can write.
        try:
            self.diagnostics_settings = diagnostics_config.parse_diagnostics_settings(settings)
        except diagnostics_config.ContractProblem:
            raise Fault("invalid_input", 400) from None
        self._runtime_state = "running"
        # The frozen diagnostics package is an input the deployment names explicitly. A package
        # that is absent is simply an unconfigured deployment; one that fails verification is
        # recorded and turns readiness red, because running on unverified contract bytes must
        # never be reported as a working production entry point.
        self.diagnostics_contract, self.contract_problem = diagnostics_config.verify_or_none(
            settings
        )
        self.health = runtime_health.health_inputs(
            settings,
            credential_names=self.other_credentials,
            credential_present=lambda name: secret(name) is not None,
            log_state=log_state,
            runtime_state=lambda: self._runtime_state,
            config_problem=self.contract_problem,
        )

    def close(self):
        """Mark this runtime as no longer serving, so readiness stops presenting it as current."""
        self._runtime_state = "closed"

    def persona_reader(self):
        """The one upstream client this deployment's page may read through, or `None`.

        The page's dependency is this client and nothing else: it is built here, where the verified
        connection entry and the verified candidate both live, and it is handed to the page as a
        finished reader. Nothing about the rest of the application travels with it.
        """
        if self.personas is None or not self.personas["enabled"]:
            return None
        connection_id = self.personas["connection_id"]
        return PersonaClient(
            connection_id,
            self.persona_connections[connection_id],
            self.personas["candidate"],
            reserved=self.other_credentials,
        )
