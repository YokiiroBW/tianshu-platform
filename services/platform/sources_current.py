"""Request-local source-current rows inside the caller's authority transaction.

This only changes how existing rows are fetched. The original Auth, Origins and
Sources predicates still decide every grant, using live credentials and clocks.
"""

from .contracts import canonical

_BATCH_SIZE = 256
_ROWS = {
    "admissions": ("admission_history", "receipt_id", "*"),
    "actor_origins": ("actor_origins", "ref", "ref,ingress,input_entry_id"),
    "origins": ("origins", "ref", "*"),
    "revoked_entries": ("revoked_entries", "id", "id"),
    "revoked_principals": ("revoked_principals", "id", "id"),
    "identities": ("identities", "account", "account,person_id,version"),
    "channels": ("channels", "channel", "channel,conversation_id"),
}


class CurrentReads:
    def __init__(self, db, admissions, viewer, actor_entries, input_entries):
        self.db = db
        self.rows = {name: {} for name in _ROWS}
        # SQLite and small legacy database adapters retain lazy reads. PG uses
        # its existing confirmed SELECT pipeline without owning a transaction.
        if not callable(getattr(db, "read_batch", None)):
            return

        self._prefetch(
            {
                "admissions": {a["source"]["receipt_id"] for a in admissions},
                "actor_origins": {a["accepted_origin"]["assertion_ref"] for a in admissions},
                "origins": {viewer["origin"]["assertion_ref"]} if viewer is not None else set(),
            }
        )
        actor_ids = {row["entry_id"] for row in self.rows["admissions"].values() if row is not None}
        actor_ids.update(
            row["entry_id"] for row in self.rows["origins"].values() if row is not None
        )
        input_ids = {
            row["input_entry_id"] for row in self.rows["actor_origins"].values() if row is not None
        }
        actor_rows = [actor_entries[key] for key in actor_ids if key in actor_entries]
        input_rows = [input_entries[key] for key in input_ids if key in input_entries]
        accounts = {row["account"] for row in self.rows["admissions"].values() if row is not None}
        accounts.update(canonical(entry["account"]) for entry in actor_rows)
        self._prefetch(
            {
                "revoked_entries": actor_ids | input_ids,
                "revoked_principals": {entry["owner"] for entry in actor_rows + input_rows},
                "identities": accounts,
                "channels": {canonical(entry["channel"]) for entry in actor_rows},
            }
        )

    def _prefetch(self, selections):
        requests, destinations = [], []
        for name, keys in selections.items():
            table, column, fields = _ROWS[name]
            keys = sorted(keys)
            for start in range(0, len(keys), _BATCH_SIZE):
                selected = keys[start : start + _BATCH_SIZE]
                placeholders = ",".join("?" for _ in selected)
                requests.append(
                    (f"SELECT {fields} FROM {table} WHERE {column} IN ({placeholders})", selected)
                )
                destinations.append(name)
                self.rows[name].update((key, None) for key in selected)
        if requests:
            # A failed or incomplete pipeline never escapes as an empty healthy
            # lookup. The existing caller rolls back the whole transaction.
            for name, cursor in zip(destinations, self.db.read_batch(requests), strict=True):
                column = _ROWS[name][1]
                for row in cursor:
                    self.rows[name][row[column]] = row

    def _get(self, name, key):
        rows = self.rows[name]
        if key not in rows:
            table, column, fields = _ROWS[name]
            rows[key] = self.db.execute(
                f"SELECT {fields} FROM {table} WHERE {column}=?", (key,)
            ).fetchone()
        return rows[key]

    def admission(self, receipt_id):
        return self._get("admissions", receipt_id)

    def actor_origin(self, ref):
        return self._get("actor_origins", ref)

    def origin(self, ref):
        return self._get("origins", ref)

    def revoked_entry(self, entry_id):
        return self._get("revoked_entries", entry_id)

    def revoked_principal(self, principal_id):
        return self._get("revoked_principals", principal_id)

    def identity(self, account):
        return self._get("identities", account)

    def channel(self, channel):
        return self._get("channels", channel)
