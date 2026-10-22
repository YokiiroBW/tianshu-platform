"""Add the plugin ledger to an existing active PG installation during maintenance.

This starts no Platform business loops, plugin workers or scheduler. The native
registry commits each source itself; only a full Platform startup can finalize
all previously bound sources, so this narrowly scoped entry point does not.
"""

import argparse
import json
from pathlib import Path

from tianshu_storage import StorageError

from .. import backends
from ..contracts import Fault, require
from .manager import validate_settings
from .packages import Packages
from .repository import Repository
from .vault import Vault


def initialize(settings):
    validate_settings(settings)
    before = backends.inspect(settings)
    require(
        before["backend"] == "postgresql" and before["state"] == "active",
        "plugin_store_unavailable",
        503,
    )
    runtime = backends.start(settings)
    try:
        anchor = Path(settings["database_path"])
        repository = Repository(str(anchor) + ".plugins.sqlite", runtime=runtime)
        Vault(repository, str(anchor) + ".plugins.key")
        Packages(settings.get("plugins", {}).get("package_directory", str(anchor) + ".plugins"))
        return {
            "backend": runtime.backend,
            "logical_source": "plugins",
            "schema": repository.backend.handle.schema,
            "state": "active",
            "databases": sorted(set(before["databases"]) | {"plugins"}),
        }
    finally:
        runtime.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--settings", required=True)
    args = parser.parse_args(argv)
    try:
        settings = json.loads(Path(args.settings).read_text(encoding="utf-8"))
        result = initialize(settings)
    except (Fault, StorageError, OSError, ValueError, KeyError, TypeError):
        print(json.dumps({"code": "plugin_store_unavailable", "status": 503}))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
