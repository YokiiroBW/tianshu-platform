"""Platform-owned database registration; file anchors never become database URLs."""

from pathlib import Path
import sqlite3

from tianshu_storage import StorageOptions, StorageRuntime

from .postgres import check_schema, dialect, prepare


class LocalDatabase:
    backend = "sqlite"

    def __init__(self, path):
        self.path = str(path)

    def connect(self, **kwargs):
        mode = kwargs.pop("mode", None)
        if mode is not None:
            kwargs.pop("uri", None)
            return sqlite3.connect(
                Path(self.path).resolve().as_uri() + "?mode=" + mode, uri=True, **kwargs
            )
        return sqlite3.connect(self.path, **kwargs)


class BoundDatabase:
    def __init__(self, handle):
        self.handle = handle
        self.backend, self.path = handle.backend, handle.path

    def connect(self, **kwargs):
        mode = kwargs.pop("mode", None)
        kwargs.pop("uri", None)
        if self.backend == "sqlite" and mode is not None:
            return LocalDatabase(self.path).connect(mode=mode, **kwargs)
        return self.handle.connect(read_only=mode == "ro", **kwargs)


class PlatformRuntime:
    """Explicit assembly registry used by readiness and the activation boundary."""

    def __init__(self, runtime):
        self.runtime = runtime
        self.handles = {}

    @property
    def backend(self):
        return self.runtime.backend

    @property
    def cache(self):
        return self.runtime.cache

    def database(self, name, path, **kwargs):
        handle = self.runtime.database(name, path, **kwargs)
        self.handles[name] = handle
        return handle

    def finalize(self):
        self.runtime.finalize()

    def status(self):
        return self.runtime.status()

    def probe(self):
        """Read current handles; never register, migrate or select a fallback."""
        try:
            for handle in tuple(self.handles.values()):
                connection = handle.connect(read_only=True, timeout=0.25)
                try:
                    check_schema(connection)
                finally:
                    connection.close()
        except (OSError, sqlite3.Error):
            return "failed"
        return "ok"

    def close(self):
        self.runtime.close()


class ReadOnlyDatabase:
    backend = "postgresql"

    def __init__(self, runtime, name, path, dialect):
        self.runtime, self.name, self.path, self.dialect = runtime, name, str(path), dialect

    def connect(self, **kwargs):
        return StorageRuntime.open_existing(
            self.runtime.options,
            service="platform",
            state_directory=self.runtime.directory,
            name=self.name,
            path=self.path,
            dialect=self.dialect,
            timeout=kwargs.get("timeout", 5),
        )


class ReadOnlyRuntime:
    backend = "postgresql"

    def __init__(self, options, directory, status):
        self.options, self.directory, self.inspection = options, directory, status

    def database(self, name, path, **kwargs):
        return ReadOnlyDatabase(self, name, path, kwargs.get("dialect"))

    def probe(self):
        if self.inspection["postgresql"] != "ok":
            return "failed"
        try:
            for name in self.inspection["databases"]:
                connection = self.database(name, self.directory, dialect=dialect).connect(
                    timeout=0.25
                )
                try:
                    check_schema(connection)
                finally:
                    connection.close()
        except (OSError, sqlite3.Error):
            return "failed"
        return "ok"


def inspect(settings):
    options = StorageOptions.from_settings(settings)
    directory = Path(settings["database_path"]).resolve().parent
    return StorageRuntime.inspect(options, service="platform", state_directory=directory)


def readonly(settings):
    """Purely inspect an existing PG binding; initial/unbound SQLite remains local."""
    if not isinstance(settings.get("database_path"), str) or not settings["database_path"]:
        return None
    options = StorageOptions.from_settings(settings)
    directory = Path(settings["database_path"]).resolve().parent
    result = StorageRuntime.inspect(options, service="platform", state_directory=directory)
    return (
        ReadOnlyRuntime(options, directory, result) if result["backend"] == "postgresql" else None
    )


def database(runtime, name, path):
    if runtime is None:
        return LocalDatabase(path)

    def initialize(connection, source):
        prepare(connection, source, logical=name, anchor=str(path))

    return BoundDatabase(runtime.database(name, str(path), prepare=initialize, dialect=dialect))


def start(settings):
    runtime = StorageRuntime(
        StorageOptions.from_settings(settings),
        service="platform",
        state_directory=Path(settings["database_path"]).resolve().parent,
    )
    runtime.start()
    return PlatformRuntime(runtime)


def postgres(connection):
    return hasattr(connection, "raw")
