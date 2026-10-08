"""Composition root for the two published endpoints and trusted local adapter ports."""

import hmac
import time

from . import diagnostics, diagnostics_config, runtime_health
from .auth import Auth, secret
from .assets import Assets, validate_connections, validate_page
from .contracts import Contracts, Fault, require
from .models import Models, validate_max_lifetime
from .model_origin_renewal import enabled as model_origin_renewal_enabled
from .origins import Origins
from .persona_client import PersonaClient
from .persona_page_config import connections as persona_connections
from .persona_page_config import page_configuration
from .projections import Projections
from .storage import Store
from .web_access import WebAccess
from .external_catalog import validate_configuration as validate_external_configuration
from .web_account import WebAccount
from .web_readers import validate_readers
from .web_memory import validate_memory_binding
from .sources import Sources
from .bots import Bots, validate_bot_settings
from .bot_adapter_catalog import validate_configuration as validate_bot_adapter_configuration


def log_state():
    """The durable sink's live state, or the honest development answer when there is none.

    Read-only: it reports what the adapter already knows and never attempts a write, so asking
    about the log can never itself change the log.
    """
    sink = diagnostics.active()
    if sink is None:
        return diagnostics.NON_DURABLE, None
    return sink.state, sink.error


def registered_credentials(settings, *, include_setup=True):
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
    take(settings.get("web_knowledge"))
    take(settings.get("web_life"))
    take(settings.get("web_memory"))
    take((settings.get("web_relationships") or {}).get("memory"))
    take(settings.get("web_qq_profiles"))
    take(settings.get("qq_alias_memory"))
    media = settings.get("media")
    if isinstance(media, dict):
        for target in media.get("targets", []):
            if isinstance(target, dict):
                take(target.get("publisher"))
                for server in target.get("servers", []):
                    take(server)
    if include_setup:
        account = settings.get("web_account")
        if isinstance(account, dict) and account.get("mode") == "create":
            take(account, "setup_token_env")
    return tuple(sorted(names))


def credential_present(settings, name):
    """The one-use installation token may be removed after the account is committed."""
    try:
        account = WebAccount(settings)
        if account.config is not None and account.config.get("setup_token_env") == name:
            if account.read() is not None:
                return True
    except Fault:
        return False
    return secret(name) is not None


# Every top-level setting this build understands. An unknown key is refused rather than ignored:
# a deployment that means something this build does not implement must not look configured.
SETTINGS_KEYS = frozenset(
    {
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
        "web_access",
        "web_account",
        "web_models",
        "provider_self_service",
        "web_assets",
        "native_config_http",
        "model_origin_renewal_http",
        "home",
        "persona_connections",
        "web_personas",
        "web_knowledge",
        "web_life",
        "web_memory",
        "web_relationships",
        "web_qq_profiles",
        "qq_alias_memory",
        "web_external",
        "diagnostics",
        "bot_connections",
        "bot_adapter_self_service",
        "role_runtime",
        "media",
    }
)


def validate_settings(settings):
    """Everything this deployment refuses before it touches a store, and nothing that writes.

    This is the *same* function the composition root runs first, and the rolling preflight is
    handed it rather than a second list of rules. That is the whole point: "the preflight says
    ready" and "the entry point would assemble" have to be one statement, because a preflight that
    accepts a configuration the real startup refuses is worse than no preflight at all.

    It reads the published contracts and validates identities, credentials and page narrowing.
    Account state is opened read-only; it creates no file, migrates nothing and binds no port.
    The returned contract and identity objects go back to the caller so assembly does not load them twice.
    """
    require(isinstance(settings, dict), "invalid_input", 400)
    require(set(settings) <= SETTINGS_KEYS, "invalid_input", 400)
    from .media.runtime import validate_media

    validate_media(settings)
    from .qq_admin import validate_profiles_configuration

    validate_profiles_configuration(
        settings,
        registered_credentials(
            {
                key: value
                for key, value in settings.items()
                if key not in {"web_qq_profiles", "qq_alias_memory"}
            }
        ),
    )
    require(settings.get("storage") == "sqlite_local", "dependency_unavailable", 503)
    require(type(settings.get("native_config_http", False)) is bool, "invalid_input", 400)
    model_origin_renewal_enabled(settings)
    WebAccess(settings)
    for key in ("database_path", "contract_directory"):
        require(
            isinstance(settings.get(key), str) and bool(settings[key]),
            "invalid_input",
            400,
        )
    contracts = Contracts(settings["contract_directory"], settings.get("source_contract_directory"))
    auth = Auth(settings, contracts)
    if settings.get("bot_connections") is not None:
        candidate_sources = Sources(None, auth, contracts, None, settings, time.time)
        validate_bot_settings(settings, auth, candidate_sources, contracts)
    # The publication lifetime is validated by the model owner's own rule rather than by a second
    # copy of it here: `Models` runs this same function while it is assembled, so a lifetime the
    # preflight accepts is one the real startup publishes with.
    validate_max_lifetime(settings)
    from .provider_management import validate_configuration

    validate_configuration(settings.get("provider_self_service"))
    if settings.get("provider_self_service") is not None:
        require(
            settings["provider_self_service"]["gateway_token_env"]
            not in registered_credentials(settings),
            "invalid_input",
            400,
        )
    credentials = registered_credentials(settings, include_setup=False)
    account = WebAccount(settings)
    # Read-only validation also prevents a damaged/lost account store from becoming a fresh
    # registration page after restart. The deployment password is never a recovery fallback.
    current_account = account.read()
    if account.config is not None and account.config["mode"] == "create":
        name = account.config["setup_token_env"]
        require(name not in credentials, "invalid_input", 400)
        if current_account is None:
            value = account.setup_secret()
            require(all(secret(other) != value for other in credentials), "invalid_input", 400)
        credentials = (*credentials, name)
    # The readiness credential is its own identity. Reading it from a variable a business
    # principal, the browser operator or a peer already holds would let one identity answer
    # readiness as another, so the name half of that rule is refused before anything is built.
    ready_name = diagnostics_config.resolve_ready_token_env(settings)
    require(ready_name not in credentials, "invalid_input", 400)
    # A name is not the value it resolves to, so the effective values are compared as well: two
    # different variables holding one secret are one identity, and a readiness credential that is
    # currently a business credential's value is refused here rather than accepted quietly. Only
    # the verdict travels - neither value is ever logged, returned or kept.
    ready_value = secret(ready_name)
    if ready_value:
        candidate = ready_value.encode("utf-8")
        for name in credentials:
            other = secret(name)
            if other and hmac.compare_digest(candidate, other.encode("utf-8")):
                raise Fault("invalid_input", 400)
    # The new readers use their own identities, never a web operator or another peer token.
    existing = registered_credentials(
        {
            key: value
            for key, value in settings.items()
            if key not in {"web_knowledge", "web_life", "web_memory"}
        }
    )
    persona_table = settings.get("persona_connections")
    if isinstance(persona_table, dict):
        existing = (
            *existing,
            *(
                entry["token_env"]
                for entry in persona_table.values()
                if isinstance(entry, dict) and isinstance(entry.get("token_env"), str)
            ),
        )
    validate_readers(settings, existing)
    validate_memory_binding(settings, auth)
    from .relationships.config import validate as validate_relationships

    validate_relationships(settings)
    validate_external_configuration(settings.get("web_external"), settings)
    validate_bot_adapter_configuration(settings.get("bot_adapter_self_service"), settings)
    role_runtime = settings.get("role_runtime")
    if role_runtime is not None:
        from .transport import core_settings

        require(
            isinstance(role_runtime, dict)
            and set(role_runtime) == {"enabled", "memory"}
            and type(role_runtime["enabled"]) is bool
            and settings.get("core") is not None
            and settings.get("web") is not None
            and settings.get("provider_self_service") is not None,
            "invalid_input",
            400,
        )
        core_settings(role_runtime["memory"])
    connections = persona_connections(
        settings.get("persona_connections"), contracts.check, credentials
    )
    if settings.get("web_personas") is not None:
        page_configuration(settings["web_personas"], connections, auth.mode, contracts.check)
    assets = validate_connections(settings, auth.principals)
    validate_page(settings.get("web_assets"), auth.principals, assets, contracts)
    try:
        diagnostics_config.parse_diagnostics_settings(settings)
    except diagnostics_config.ContractProblem:
        raise Fault("invalid_input", 400) from None
    return contracts, auth


class Platform:
    def __init__(self, settings, *, clock=time.time):
        # The pure half runs first and is the only place these rules live, so the preflight and the
        # real startup cannot drift apart.
        self.contracts, self.auth = validate_settings(settings)
        # The native snapshot HTTP port stays closed unless the deployment explicitly opens it.
        self.native_config_http = settings.get("native_config_http", False)
        self.store = Store(settings["database_path"])
        self.origins = Origins(self.store, self.auth, self.contracts, clock)
        self.sources = Sources(self.store, self.auth, self.contracts, self.origins, settings, clock)
        self.origins.input_entries = self.sources.entries
        self.settings = settings
        self.bots = Bots(self)
        self.models = Models(self.store, self.auth, self.contracts, self.origins, settings, clock)
        from .provider_catalog import ProviderCatalog

        self.provider_catalog = (
            ProviderCatalog(settings["provider_self_service"]["directory"])
            if settings.get("provider_self_service") is not None
            else None
        )
        from .provider_authority import ProviderAuthority

        self.provider_authority = ProviderAuthority(self, clock)
        self.projections = Projections(self.store, self.auth, self.contracts, clock)
        self.assets = Assets(self.store, self.auth, settings)
        # A read-only page that could widen this identity's own bindings is refused at startup.
        self.assets.configure_page(settings.get("web_assets"), self.contracts)
        # The persona page reads one registered character service through its own deployment
        # credential. The credential variable is its own: it is never a value another service
        # identity, or the browser's own operator, already holds.
        self.other_credentials = registered_credentials(settings)
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
        from .role_runtime import RoleRuntime

        self.role_runtime = RoleRuntime(self)
        from .bot_adapters import BotAdapters

        self.bot_adapters = BotAdapters(self)
        from .bot_observation import BotObservation

        self.bot_observation = BotObservation(self)
        from .qq_admin import QQAdmin

        self.qq_admin = QQAdmin(self)
        from .relationships.application import Relationships

        self.relationships = Relationships(self)
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
            credential_present=lambda name: credential_present(settings, name),
            log_state=log_state,
            runtime_state=lambda: self._runtime_state,
            config_problem=self.contract_problem,
        )
        from .local_work import LocalWork

        self.local_work = LocalWork()
        self.store.run_local = self.local_work.run
        from .media.runtime import Media

        self.media = Media(settings, clock=clock, local=self.local_work.run)

    def close(self):
        """Mark this runtime as no longer serving, so readiness stops presenting it as current."""
        self._runtime_state = "closed"
        self.local_work.close()

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
