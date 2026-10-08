"""Background job registry: long analyze/generate calls survive client timeouts.

Clients (Freebuff etc.) cut MCP calls after ~30-60s; thinking-heavy jobs take minutes.
Flow: submit(key, factory, wait_seconds) runs the job as a detached asyncio task and
waits inline up to wait_seconds; on timeout the caller returns a running-ticket and the
job keeps computing server-side. fetch(key, wait_seconds) re-attaches and returns the
final payload, 'running' again, or the original error. In-memory, single-process (the
daemon is one uvicorn worker); request_id idempotency in the router cache makes a
post-restart retry cheap."""
from __future__ import annotations

import asyncio
import logging
import time

MAX_DONE = 64          # finished jobs kept for late fetches before pruning


class JobManager:
    def __init__(self, logger: logging.Logger | None = None):
        self.log = logger or logging.getLogger("gemini_mcp")
        self._jobs: dict[str, dict] = {}

    async def submit(self, key: str, factory, wait_seconds: float | None):
        """factory() -> awaitable payload. Returns (status, payload):
        status: 'done' | 'running' (ticket) | 'error'."""
        j = self._jobs.get(key)
        if j is None:
            j = {"state": "running", "created": time.time(),
                 "task": asyncio.create_task(self._run(key, factory()))}
            self._jobs[key] = j
            self.log.info("job %s submitted (inline wait %.0fs)", key, wait_seconds or 0)
        else:
            self.log.info("job %s: re-attaching (state=%s)", key, j["state"])
        return await self._await(key, j, wait_seconds)

    async def fetch(self, key: str, wait_seconds: float | None):
        j = self._jobs.get(key)
        if j is None:
            return "unknown", {
                "status": "unknown", "request_id": key,
                "hint": ("no in-memory job with this request_id (never submitted, or the "
                         "daemon restarted). Re-run analyze_large/gemini_generate with "
                         "the SAME request_id — finished chunks/consolidations are served "
                         "from the router cache, so the retry is cheap and idempotent.")}
        return await self._await(key, j, wait_seconds)

    async def _run(self, key: str, coro) -> None:
        j = self._jobs[key]
        try:
            j["result"] = await coro
            j["state"] = "done"
            self.log.info("job %s done in %.1fs", key, time.time() - j["created"])
        except Exception as e:  # noqa: BLE001 — payload travels as str to the fetcher
            j["state"] = "error"
            j["error"] = str(e)
            self.log.warning("job %s failed after %.1fs: %s",
                             key, time.time() - j["created"], e)
        finally:
            j["finished"] = time.time()
            self._prune()

    async def _await(self, key: str, j: dict, wait_seconds: float | None):
        if j["state"] == "running":
            try:
                # shield: an inline-wait timeout must NOT cancel the background job
                await asyncio.wait_for(asyncio.shield(j["task"]), timeout=wait_seconds)
            except TimeoutError:
                pass
            except Exception:  # noqa: BLE001 — job state below is the source of truth
                pass
        if j["state"] == "done":
            return "done", j["result"]
        if j["state"] == "error":
            return "error", {"status": "error", "error": "job_failed",
                             "detail": j.get("error", "unknown"), "request_id": key}
        return "running", {
            "status": "running", "request_id": key,
            "elapsed_s": round(time.time() - j["created"], 1),
            "poll": (f"job still computing server-side; call analyze_result with "
                     f"request_id='{key}' (it waits up to its wait_seconds per call) "
                     f"until you receive the final result — do not resubmit, quota is "
                     f"already committed")}

    def _prune(self) -> None:
        done = [k for k, j in self._jobs.items() if j["state"] != "running"]
        for k in done[:max(0, len(done) - MAX_DONE)]:
            self._jobs.pop(k, None)
