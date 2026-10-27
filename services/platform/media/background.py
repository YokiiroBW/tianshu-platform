"""Own and observe long-running media loops, including recovery after an exit."""

import asyncio
import time

from ..local_work import background_scope


class Background:
    def __init__(self):
        self.tasks = {}
        self.states = {}

    def start(self, name, operation, *args):
        self.states[name] = {"state": "running", "restarts": 0, "code": None}
        self.tasks[name] = asyncio.create_task(
            self.run(name, operation, args), name="media:" + name
        )

    async def run(self, name, operation, args):
        state = self.states[name]
        while True:
            try:
                state["state"] = "running"
                with background_scope():
                    await operation(*args)
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            # Expose a fixed diagnostic; exception text can contain source data.
            state.update(
                state="recovering", code="media_background_restarting", failed_at=time.time()
            )
            state["restarts"] += 1
            await asyncio.sleep(min(30, 2 ** min(state["restarts"], 5)))

    def view(self):
        return [
            {
                "name": name,
                **state,
                "state": "stopped" if self.tasks[name].done() else state["state"],
            }
            for name, state in self.states.items()
        ]

    def healthy(self):
        return bool(self.tasks) and all(row["state"] == "running" for row in self.view())

    async def close(self):
        for task in self.tasks.values():
            task.cancel()
        await asyncio.gather(*self.tasks.values(), return_exceptions=True)
        self.tasks.clear()
        self.states.clear()
