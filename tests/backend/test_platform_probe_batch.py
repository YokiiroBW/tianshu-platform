"""Product schema checks retain every registered owner while reducing read round trips."""

import json
import unittest
from types import SimpleNamespace

from tianshu_storage import StorageError
from services.platform.backends import PlatformRuntime
from services.platform.postgres import check_schema


class SchemaConnection:
    def __init__(self, required='["jobs"]', present=None, version=2):
        self.row = (required, json.dumps(present or ["jobs", "_ts_schema_metadata"]), version)
        self.statements = []
        self.closed = False

    def native(self, statement):
        self.statements.append(statement)
        return SimpleNamespace(fetchone=lambda: self.row)

    def close(self):
        self.closed = True


class PlatformProbeBatchTests(unittest.TestCase):
    def test_all_17_handles_keep_readonly_binding_and_one_schema_select_each(self):
        runtime = PlatformRuntime(SimpleNamespace())
        connections, options = [], []

        def connect(**kwargs):
            options.append(kwargs)
            connection = SchemaConnection()
            connections.append(connection)
            return connection

        runtime.handles = {str(i): SimpleNamespace(connect=connect) for i in range(17)}
        self.assertEqual(runtime.probe(), "ok")
        self.assertEqual(len(connections), 17)
        self.assertTrue(all(option == {"read_only": True, "timeout": 0.25} for option in options))
        self.assertTrue(all(c.closed and len(c.statements) == 1 for c in connections))
        self.assertTrue(all("m.version" in c.statements[0] for c in connections))

    def test_manifest_loss_invalid_identifiers_and_missing_business_tables_fail(self):
        cases = [
            SchemaConnection('"invalid"'),
            SchemaConnection('["bad.name"]'),
            SchemaConnection('["lost_jobs"]'),
            SchemaConnection(present=["jobs"]),
        ]
        absent = SchemaConnection()
        absent.row = None
        cases.append(absent)
        for connection in cases:
            with self.subTest(row=connection.row), self.assertRaises(StorageError):
                check_schema(connection)
