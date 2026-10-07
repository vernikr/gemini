"""Progress funnel (FR-E1..E3, b03): every stage → MCP notifications/progress (when the
client sent a progressToken) + notifications/message + always into result `progress_trace`.
Router events arrive synchronously (on_event callback) and are scheduled onto the loop."""
from __future__ import annotations

import asyncio
import logging
import time

from gemini_router import RouterEvent


class ProgressEmitter:
    def __init__(self, ctx=None, logger: logging.Logger | None = None):
        self.ctx = ctx
        self.logger = logger or logging.getLogger("gemini_mcp")
        self.trace: list[dict] = []
        self.pct: float = 0.0
        self._tasks: set[asyncio.Task] = set()

    async def stage(self, name: str, pct: float | None = None,
                    message: str | None = None, **detail) -> None:
        if pct is not None:
            self.pct = float(pct)
        msg = message or name
        self.trace.append({"stage": name, "pct": self.pct, "message": msg,
                           "detail": detail, "ts": time.time()})
        self.logger.info("[%3.0f%%] %s", self.pct, msg)
        if self.ctx is not None:
            try:
                await self.ctx.report_progress(self.pct, 100.0, msg)
            except TypeError:                      # older SDK without message kwarg
                try:
                    await self.ctx.report_progress(self.pct, 100.0)
                except Exception:  # noqa: BLE001 — degradation level 2/3 (b03)
                    pass
            except Exception:  # noqa: BLE001
                pass
            try:
                await self.ctx.info(msg)
            except Exception:  # noqa: BLE001
                pass

    def router_event_sync(self, ev: RouterEvent) -> None:
        entry = {"stage": f"router:{ev.stage}", "pct": self.pct, "message": ev.message(),
                 "detail": {"model": ev.model, "key_id": ev.key_id,
                            "wait_s": ev.wait_s, **ev.detail}, "ts": ev.ts}
        self.trace.append(entry)
        self.logger.info("        · %s", entry["message"])
        if self.ctx is None:
            return
        try:
            task = asyncio.get_running_loop().create_task(self._notify(ev.message()))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        except RuntimeError:                       # sync facade: trace-only
            pass

    async def _notify(self, msg: str) -> None:
        try:
            await self.ctx.report_progress(self.pct, 100.0, msg)
        except Exception:  # noqa: BLE001
            pass
        try:
            await self.ctx.info(msg)
        except Exception:  # noqa: BLE001
            pass

    async def drain(self) -> None:
        if self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)
