"""Platform's native PostgreSQL schema, migration and SQL vocabulary.

The shared package owns transport and backend binding. This module owns Platform's
JSON representation, authority triggers, catalogue and SQLite migration semantics.
Each logical database retains its own schema and transaction serialization, just
as BEGIN IMMEDIATE serialized its SQLite writers before this backend was added.
"""

import re
import json
import sqlite3

from tianshu_storage import MemoryCursor, StorageError


IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
NUMERIC_PATHS = frozenset(
    {
        "missing",
        "auto_download",
        "member_count",
        "quality_review",
        "quality_catchup",
        "next_scan_at",
        "next_check_at",
        "lease",
        "created_at",
        "updated_at",
        "priority",
        "created",
        "updated",
        "revision",
        "generation",
        "completed_parts",
        "enabled",
        "sequence",
        "order",
        "downloaded",
        "size_bytes",
        "characters",
        "file_order",
        "year",
        "number",
        "chapter_count",
        "track_count",
        "count",
        "expires_at",
        "checked_at",
    }
)
ROW_ORDER_TABLES = frozenset(
    {"documents", "replies", "submissions", "provider_sources", "test_attempts"}
)


def quote(name):
    if not IDENTIFIER.fullmatch(name):
        raise ValueError("invalid platform schema identifier")
    return '"' + name + '"'


def _columns(connection, table):
    return connection.native(
        "SELECT a.attname, format_type(a.atttypid,a.atttypmod), "
        "CASE WHEN a.attnotnull THEN 'NO' ELSE 'YES' END, "
        "pg_get_expr(d.adbin,d.adrelid), "
        "EXISTS(SELECT 1 FROM pg_index i WHERE i.indrelid=a.attrelid "
        "AND i.indisprimary AND a.attnum=ANY(i.indkey)) "
        "FROM pg_attribute a LEFT JOIN pg_attrdef d "
        "ON d.adrelid=a.attrelid AND d.adnum=a.attnum "
        "WHERE a.attrelid=to_regclass(?) AND a.attnum>0 AND NOT a.attisdropped "
        "AND a.attname<>'_ts_rowid' ORDER BY a.attnum",
        (table,),
    ).fetchall()


def check_schema(connection):
    """Check the manifest row and actual tables in one read-only schema snapshot."""
    row = connection.native(
        "SELECT m.table_names,(SELECT json_agg(c.relname)::text FROM pg_class c "
        "WHERE c.relnamespace=current_schema()::regnamespace AND c.relkind IN ('r','p')) "
        ",m.version FROM _ts_schema_metadata m WHERE m.id=1"
    ).fetchone()
    try:
        required = json.loads(row[0]) if row else None
        present = set(json.loads(row[1])) if row and row[1] else set()
    except (TypeError, ValueError):
        raise StorageError("platform_schema_incomplete") from None
    if (
        not isinstance(required, list)
        or any(not isinstance(name, str) or not IDENTIFIER.fullmatch(name) for name in required)
        or "_ts_schema_metadata" not in present
        or not set(required) <= present
    ):
        raise StorageError("platform_schema_incomplete")
    return set(required)


def _remember_table(connection, table, required):
    if table not in required:
        connection.native(
            "UPDATE _ts_schema_metadata SET table_names=? WHERE id=1",
            (json.dumps(sorted(required | {table})),),
        )


def _schema(connection):
    connection.native(
        "CREATE TABLE IF NOT EXISTS _ts_schema_metadata (id BIGINT PRIMARY KEY, "
        "version BIGINT NOT NULL, table_names TEXT NOT NULL)"
    )
    connection.native("INSERT INTO _ts_schema_metadata VALUES(1,0,'[]') ON CONFLICT DO NOTHING")
    connection.native("""
        CREATE OR REPLACE FUNCTION ts_json_extract(document TEXT, path TEXT)
        RETURNS TEXT LANGUAGE SQL IMMUTABLE PARALLEL SAFE AS $$
          SELECT CASE jsonb_typeof(value)
            WHEN 'null' THEN NULL
            WHEN 'boolean' THEN CASE value::text WHEN 'true' THEN '1' ELSE '0' END
            ELSE value #>> '{}' END
          FROM (SELECT document::jsonb #> string_to_array(substr(path,3),'.') AS value) item
        $$
    """)
    connection.native("""
        CREATE OR REPLACE FUNCTION ts_json_set(document TEXT, path TEXT, value TEXT)
        RETURNS TEXT LANGUAGE SQL IMMUTABLE PARALLEL SAFE AS $$
          SELECT jsonb_set(document::jsonb,string_to_array(substr(path,3),'.'),to_jsonb(value),true)::text
        $$
    """)
    connection.native("""
        CREATE OR REPLACE FUNCTION ts_json_set(document TEXT, path TEXT, value JSONB)
        RETURNS TEXT LANGUAGE SQL IMMUTABLE PARALLEL SAFE AS $$
          SELECT jsonb_set(document::jsonb,string_to_array(substr(path,3),'.'),value,true)::text
        $$
    """)
    connection.native("""
        CREATE OR REPLACE FUNCTION ts_json_array_length(document TEXT, path TEXT)
        RETURNS BIGINT LANGUAGE SQL IMMUTABLE PARALLEL SAFE AS $$
          SELECT CASE WHEN value IS NULL THEN NULL
            WHEN jsonb_typeof(value)='array' THEN jsonb_array_length(value) ELSE 0 END
          FROM (SELECT document::jsonb #> string_to_array(substr(path,3),'.') AS value) item
        $$
    """)


def _json(sql):
    def extract(match):
        column, path = match.group(1), match.group(2)
        expression = f"ts_json_extract({column},'$.{path}')"
        return f"({expression})::double precision" if path in NUMERIC_PATHS else expression

    sql = re.sub(r"json_extract\(\s*([\w.]+)\s*,\s*'\$\.([^']+)'\s*\)", extract, sql, flags=re.I)
    sql = re.sub(r"\bjson_set\(", "ts_json_set(", sql, flags=re.I)
    sql = re.sub(r"\bjson_array_length\(", "ts_json_array_length(", sql, flags=re.I)
    sql = re.sub(r"\bjson\((\?)\)", r"CAST(\1 AS jsonb)", sql, flags=re.I)
    return sql


def _ddl(sql):
    sql = re.sub(
        r"\bINTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT\b",
        "BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY",
        sql,
        flags=re.I,
    )
    sql = re.sub(r"\bINTEGER\b", "BIGINT", sql, flags=re.I)
    sql = re.sub(r"\bREAL\b", "DOUBLE PRECISION", sql, flags=re.I)
    sql = re.sub(r"\bBLOB\b", "BYTEA", sql, flags=re.I)
    # Preserve SQLite row ordering without replacing a business identity.
    sql = sql.rstrip().rstrip(";")
    table = re.match(r"CREATE TABLE(?: IF NOT EXISTS)?\s+[\"]?(\w+)", sql, re.I).group(1)
    if table in ROW_ORDER_TABLES:
        position = sql.rfind(")")
        sql = (
            sql[:position] + ", _ts_rowid BIGINT GENERATED BY DEFAULT AS IDENTITY" + sql[position:]
        )
    return sql


def _trigger(connection, sql):
    match = re.fullmatch(
        r"CREATE TRIGGER (?:IF NOT EXISTS )?(head_\w+) AFTER (INSERT|UPDATE|DELETE) ON (\w+) "
        r"BEGIN UPDATE authority_head SET sequence=sequence\+1 WHERE singleton=1; END;?",
        sql,
        re.I,
    )
    if not match:
        raise ValueError("unsupported platform authority trigger")
    name, event, table = match.groups()
    connection.native("""
        CREATE OR REPLACE FUNCTION ts_authority_changed() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN UPDATE authority_head SET sequence=sequence+1 WHERE singleton=1; RETURN NULL; END $$
    """)
    connection.native(f"DROP TRIGGER IF EXISTS {quote(name)} ON {quote(table)}")
    return connection.native(
        f"CREATE TRIGGER {quote(name)} AFTER {event} ON {quote(table)} "
        "FOR EACH ROW EXECUTE FUNCTION ts_authority_changed()"
    )


def dialect(connection, sql, parameters):
    boolean_parameters = any(type(value) is bool for value in (parameters or ()))
    if boolean_parameters:
        parameters = tuple(int(value) if type(value) is bool else value for value in parameters)
    # The product has a few additive DDL scripts. Split only complete SQLite
    # statements, preserving trigger bodies and quoted semicolons.
    if not parameters and ";" in sql:
        statements, pending = [], ""
        for character in sql:
            pending += character
            if character == ";" and sqlite3.complete_statement(pending):
                statements.append(pending.strip())
                pending = ""
        if pending.strip():
            statements.append(pending.strip())
        if len(statements) > 1:
            result = MemoryCursor()
            for item in statements:
                result = connection.execute(item)
            return result
    statement = sql.strip().rstrip(";")
    upper = statement.upper()
    if upper.startswith("PRAGMA "):
        pragma = statement[7:].strip()
        if pragma.lower() == "user_version":
            return connection.native("SELECT version FROM _ts_schema_metadata WHERE id=1")
        match = re.fullmatch(r"user_version\s*=\s*(\d+)", pragma, re.I)
        if match:
            own_transaction = not connection.in_transaction
            result = connection.native(
                "UPDATE _ts_schema_metadata SET version=? WHERE id=1", (int(match[1]),)
            )
            if own_transaction:
                connection.commit()
            return result
        match = re.fullmatch(r"table_info\((\w+)\)", pragma, re.I)
        if match:
            rows = [
                (i, c[0], c[1], int(c[2] == "NO"), c[3], int(c[4]))
                for i, c in enumerate(_columns(connection, match[1]))
            ]
            return MemoryCursor(rows, ("cid", "name", "type", "notnull", "dflt_value", "pk"))
        if re.fullmatch(
            r"(journal_mode|synchronous|busy_timeout|foreign_keys|query_only)\s*=\s*[\w]+",
            pragma,
            re.I,
        ):
            return MemoryCursor()
        raise ValueError("unsupported platform database pragma")
    if "SQLITE_MASTER" in upper:
        translated = re.sub(
            r"\bsqlite_master\b",
            "(SELECT tablename AS name, 'table' AS type FROM pg_tables WHERE schemaname=current_schema() AND tablename NOT IN ('_ts_schema_metadata','__tianshu_storage_binding')) AS sqlite_master",
            statement,
            flags=re.I,
        )
        return connection.native(translated, parameters)
    if upper.startswith("BEGIN IMMEDIATE"):
        if not connection.in_transaction:
            connection.native("BEGIN")
        # Database/schema-scoped and transaction-bound. Never touches Redis.
        return connection.native(
            "SELECT pg_advisory_xact_lock(hashtextextended(current_database()||':'||current_schema(),0))"
        )
    if upper.startswith("CREATE TRIGGER"):
        return _trigger(connection, statement)
    if upper.startswith("CREATE TABLE"):
        # SQLite DDL outside a transaction commits immediately. Publish the table
        # and its recovery manifest together; never commit a caller-owned migration.
        own_transaction = not connection.in_transaction
        if own_transaction:
            connection.native("BEGIN")
        try:
            connection.native("SELECT id FROM _ts_schema_metadata WHERE id=1 FOR UPDATE")
            required = check_schema(connection)
            result = connection.native(_ddl(statement), parameters)
            table = re.match(
                r"CREATE TABLE(?: IF NOT EXISTS)?\s+[\"]?(\w+)", statement, re.I
            ).group(1)
            _remember_table(connection, table, required)
            if own_transaction:
                connection.commit()
            return result
        except BaseException:
            if own_transaction:
                connection.rollback()
            raise
    if upper.startswith("ALTER TABLE"):
        translated = re.sub(r"\bREAL\b", "DOUBLE PRECISION", statement, flags=re.I)
        translated = re.sub(r"\bINTEGER\b", "BIGINT", translated, flags=re.I)
        return connection.native(translated, parameters)
    translated = _json(statement)
    translated = re.sub(r"\browid\b", "_ts_rowid", translated, flags=re.I)
    # SQLite's integer cast truncates; PostgreSQL's float->integer cast rounds.
    translated = re.sub(
        r"CAST\((created|observed_at) AS INTEGER\)",
        r"CAST(trunc(\1) AS BIGINT)",
        translated,
        flags=re.I,
    )
    translated = re.sub(
        r"\bIS\s+(NOT\s+)?\?",
        lambda m: "IS DISTINCT FROM ?" if m[1] else "IS NOT DISTINCT FROM ?",
        translated,
        flags=re.I,
    )
    translated = re.sub(r"\bMAX\((expires_at),\?\)", r"GREATEST(\1,?)", translated, flags=re.I)
    ignore = re.match(r"INSERT OR IGNORE\s+", translated, re.I)
    replace = re.match(r"INSERT OR REPLACE INTO\s+(\w+)\s+", translated, re.I)
    if ignore:
        translated = (
            re.sub(r"INSERT OR IGNORE", "INSERT", translated, count=1, flags=re.I)
            + " ON CONFLICT DO NOTHING"
        )
    elif replace:
        columns = _columns(connection, replace[1])
        keys = [c[0] for c in columns if c[4]]
        translated = re.sub(r"INSERT OR REPLACE", "INSERT", translated, count=1, flags=re.I)
        translated += " ON CONFLICT (" + ",".join(map(quote, keys)) + ") DO UPDATE SET "
        translated += ",".join(
            f"{quote(c[0])}=excluded.{quote(c[0])}" for c in columns if c[0] not in keys
        )
    if (
        translated != statement
        or boolean_parameters
        or upper.startswith("INSERT INTO PUBLICATION_INTENTS")
        or upper.startswith("INSERT INTO CONTROL_INTENTS")
    ):
        return connection.native(
            translated,
            parameters,
            savepoint=upper.startswith(
                ("INSERT INTO PUBLICATION_INTENTS", "INSERT INTO CONTROL_INTENTS")
            ),
        )
    return None


def prepare(connection, source, *, logical="", anchor=""):
    """Import typed rows once; source SQLite remains untouched for rollback."""
    state = connection.native(
        "SELECT state FROM __tianshu_storage_binding WHERE singleton=1"
    ).fetchone()
    if state and state[0] == "ready":
        check_schema(connection)
    _schema(connection)
    if source is None:
        return
    tables = source.execute(
        "SELECT name,sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY rowid"
    ).fetchall()
    for table, ddl in tables:
        connection.execute(ddl)
        columns = [row[1] for row in source.execute(f"PRAGMA table_info({quote(table)})")]
        names = ",".join(map(quote, columns))
        rows = source.execute(f"SELECT {names} FROM {quote(table)} ORDER BY rowid").fetchall()
        if (logical == "media" and table == "documents") or (
            logical == "bots" and table == "replies"
        ):
            from .payload_files import MediaPayloads, DeliveryPayloads

            payloads = MediaPayloads(anchor) if logical == "media" else DeliveryPayloads(anchor)
            transformed = []
            for raw in rows:
                row = dict(zip(columns, raw))
                field = "body" if logical == "media" else "request"
                value = json.loads(row[field])
                saved = (
                    payloads.store(row["kind"], value)
                    if logical == "media"
                    else payloads.store(value)
                )
                recovered = (
                    payloads.read(row["kind"], saved)
                    if logical == "media"
                    else payloads.read(saved)
                )
                # References are additive internal metadata; original fields must remain exact.
                original = (
                    payloads.read(row["kind"], value)
                    if logical == "media"
                    else payloads.read(value)
                )
                if recovered != original:
                    raise ValueError("platform payload migration mismatch")
                row[field] = json.dumps(saved, ensure_ascii=False)
                transformed.append(tuple(row[column] for column in columns))
            rows = transformed
        if rows:
            connection.executemany(
                f"INSERT INTO {quote(table)} ({names}) VALUES({','.join('?' for _ in columns)})",
                rows,
            )
        if connection.native(f"SELECT count(*) FROM {quote(table)}").fetchone()[0] != len(rows):
            raise ValueError("platform migration count mismatch")
        order = " ORDER BY _ts_rowid" if table in ROW_ORDER_TABLES else ""
        imported = connection.native(f"SELECT {names} FROM {quote(table)}{order}").fetchall()
        if sorted(map(repr, map(tuple, imported))) != sorted(map(repr, map(tuple, rows))):
            raise ValueError("platform migration content mismatch")
        for column in connection.native(
            "SELECT attname FROM pg_attribute WHERE attrelid=to_regclass(?) AND attidentity<>''",
            (table,),
        ):
            name = column[0]
            connection.native(
                f"SELECT setval(pg_get_serial_sequence(?,?),GREATEST(COALESCE(MAX({quote(name)}),0),1),COALESCE(MAX({quote(name)}),0)>0) FROM {quote(table)}",
                (table, name),
            )
    for _, ddl in source.execute(
        "SELECT name,sql FROM sqlite_master WHERE type='index' AND sql IS NOT NULL"
    ):
        connection.execute(ddl)
    for _, ddl in source.execute(
        "SELECT name,sql FROM sqlite_master WHERE type='trigger' AND sql IS NOT NULL"
    ):
        connection.execute(ddl)
    connection.execute(f"PRAGMA user_version={source.execute('PRAGMA user_version').fetchone()[0]}")
