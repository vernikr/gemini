"""Progress funnel (FR-E1..E3, b03): every stage → MCP notifications/progress (when the
client sent a progressToken) + notifications/message + always into result `progress_trace`.
Router events arrive synchronously (on_event callback) and are scheduled onto the loop.

FR-G1 job visibility: an emitter handed a `live` dict mirrors its latest state into it
(phase/eta/model/key_id/stage line) so JobManager can show WHAT a running job is doing
on the polling surface — no subscription needed."""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager

from gemini_router import RouterEvent

# Canonical phases for the polling surface (FR-G1). Unknown stage names pass through
# verbatim — honesty over a closed vocabulary.
_GATEWAY_PHASE = {"uploading": "uploading", "measured": "measuring", "packed": "packing",
                  "chunking": "chunking", "consolidating": "consolidating",
                  "group": "generating", "done": "finishing"}
_ROUTER_PHASE = {"dispatch": "generating", "retry": "retrying",
                 "switch": "switching_model", "pacing": "pacing", "ok": "cached"}


class ProgressEmitter:
    def __init__(self, ctx=None, logger: logging.Logger | None = None,
                 live: dict | None = None):
        self.ctx = ctx
        self.logger = logger or logging.getLogger("gemini_mcp")
        self.trace: list[dict] = []
        self.pct: float = 0.0
        self.live = live                     # shared with JobManager; None = inline run
        self._span: tuple[float, float] | None = None
        self._tasks: set[asyncio.Task] = set()

    @contextmanager
    def progress_span(self, low: float, high: float) -> Iterator[None]:
        """Map one subtask's 0–100 into a bounded portion of its parent job.

        In a multi-file call, a child pipeline must not announce 100% while sibling
        files and final consolidation remain. Nested spans restore their parent.
        """
        parent = self._span
        if parent is not None:
            a, b = parent
            low, high = a + (b - a) * low / 100, a + (b - a) * high / 100
        self._span = (low, high)
        try:
            yield
        finally:
            self._span = parent

    def _update_live(self, **fields) -> None:
        if self.live is None:
            return
        if fields.get("wait_until", True) is None:   # explicit None → a gate lifted
            self.live.pop("wait_until", None)
        self.live.update({k: v for k, v in fields.items() if v is not None})
        self.live["updated"] = time.time()

    async def stage(self, name: str, pct: float | None = None,
                    message: str | None = None, **detail) -> None:
        if pct is not None:
            value = max(0.0, min(100.0, float(pct)))
            if self._span is not None:
                low, high = self._span
                value = low + (high - low) * value / 100
            self.pct = max(self.pct, value)  # a later stage cannot un-finish work
        msg = message or name
        self.trace.append({"stage": name, "pct": self.pct, "message": msg,
                           "detail": detail, "ts": time.time()})
        phase = ("file_complete" if name == "done" and self._span is not None else
                 _GATEWAY_PHASE.get(name, name))
        self._update_live(phase=phase, pct=self.pct,
                          stage_line=msg, wait_until=None)
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
        self._live_from_router(ev)
        self.logger.info("        · %s", entry["message"])
        if self.ctx is None:
            return
        try:
            task = asyncio.get_running_loop().create_task(self._notify(ev.message()))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        except RuntimeError:                       # sync facade: trace-only
            pass

    def _live_from_router(self, ev: RouterEvent) -> None:
        """FR-G1: canonical phase from the router's event vocabulary. `waiting` carries
        the gate reason (rpm/tpm/cooldown); waits expose an eta, activity clears it."""
        if self.live is None:
            return
        if ev.stage == "waiting":
            reason = ev.detail.get("reason", "")
            phase = f"waiting_{reason}" if reason in ("rpm", "tpm", "cooldown") \
                else "waiting"
        else:
            phase = _ROUTER_PHASE.get(ev.stage, f"router:{ev.stage}")
        wait_until = time.time() + ev.wait_s if ev.wait_s else None
        if phase in ("generating", "switching_model"):
            wait_until = None
        self._update_live(phase=phase, stage_line=ev.message(), model=ev.model,
                          key_id=ev.key_id if ev.key_id != "cache" else None,
                          attempt=ev.detail.get("attempt"), wait_until=wait_until)

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
