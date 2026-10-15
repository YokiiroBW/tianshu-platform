"""Shared due-scan loop; each source family runs independently in Media's lifecycle."""

import asyncio

from ..contracts import Fault


async def scan_due(media, kind, identity_field, scan):
    for row in await media.local(media.repo.list, kind):
        if row["state"] == "active" and row["next_scan_at"] <= media.repo.clock():
            try:
                await scan(row[identity_field])
            except Fault:
                pass


async def pump_scans(media, kind, identity_field, scan):
    while True:
        await scan_due(media, kind, identity_field, scan)
        await asyncio.sleep(10)
