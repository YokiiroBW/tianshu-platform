"""Short-lived metadata reuse only; credentials and download URLs are never cached."""

import asyncio
import copy
import time
from collections import OrderedDict

from ...contracts import digest


class MetadataCache:
    def __init__(self, *, clock=time.monotonic):
        self.clock = clock
        self.values = OrderedDict()
        self.lock = asyncio.Lock()

    async def read(self, name, cookie, parameters, fetch, *, fresh=False, ttl=3600):
        key = digest(
            {
                "operation": name,
                "account": (cookie or {}).get("_music_account_id"),
                "revision": (cookie or {}).get("_music_account_revision"),
                "credentials": digest(cookie or {}),
                "parameters": parameters,
            }
        )
        async with self.lock:
            cached = self.values.get(key)
            if not fresh and cached and cached[0] > self.clock():
                self.values.move_to_end(key)
                return copy.deepcopy(cached[1])
            result = await fetch()
            self.values[key] = (self.clock() + ttl, copy.deepcopy(result))
            self.values.move_to_end(key)
            while len(self.values) > 256:
                self.values.popitem(last=False)
            return result
