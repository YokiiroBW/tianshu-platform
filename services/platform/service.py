"""Composition root for the two published endpoints and trusted local adapter ports."""

import time

from .auth import Auth
from .assets import Assets
from .contracts import Contracts, require
from .models import Models
from .origins import Origins
from .projections import Projections
from .storage import Store
from .sources import Sources


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
            },
            "invalid_input",
            400,
        )
        require(settings["storage"] == "sqlite_local", "dependency_unavailable", 503)
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
        self.auth.activate(self.store, clock)
