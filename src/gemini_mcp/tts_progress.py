"""Rich terminal progress for gateway TTS, driven by router file-progress events."""
from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager

from gemini_router.events import RouterEvent
from rich.console import Console
from rich.progress import BarColumn, Progress, TaskProgressColumn, TextColumn


def _format_eta(seconds: float) -> str:
    """Render a compact approximate duration for the terminal indicator."""
    total = max(0, int(float(seconds) + 0.5))
    if total >= 3600:
        return f"{total // 3600}h {(total % 3600) // 60:02d}m"
    if total >= 60:
        return f"{total // 60}:{total % 60:02d}"
    return f"{total}s"


@contextmanager
def tts_progress(
    console: Console, *, enabled: bool = True
) -> Iterator[Callable[[RouterEvent], None]]:
    """Render compact, monotonic TTS progress with an explicitly approximate ETA.

    Completed chunks and ffmpeg output time provide measured progress. The router
    estimates movement inside an active chunk and attaches an ETA to file events.
    """
    progress = Progress(
        TextColumn("{task.description}", markup=False),
        BarColumn(bar_width=28),
        TaskProgressColumn(),
        console=console,
        transient=False,
        refresh_per_second=10,
        disable=not (enabled and console.is_terminal),
    )
    with progress:
        task = progress.add_task("Preparing audio", total=100)
        completed = 0.0

        def report(event: RouterEvent) -> None:
            nonlocal completed
            if event.pct is not None:
                completed = max(completed, min(100.0, float(event.pct)))

            eta_s = None
            if event.stage == "file_progress":
                description = str(event.detail.get("label") or "Preparing audio")
                eta_s = event.detail.get("eta_s")
            elif event.stage == "waiting":
                description = "Waiting"
                eta_s = event.wait_s
            elif event.stage == "dispatch":
                chunk, chunks = event.detail.get("chunk"), event.detail.get("chunks")
                description = f"TTS · {chunk}/{chunks}" if chunk and chunks else "TTS"
            elif event.stage == "retry":
                chunk, chunks = event.detail.get("chunk"), event.detail.get("chunks")
                description = f"Retry · {chunk}/{chunks}" if chunk and chunks else "Retry"
            elif event.stage == "switch":
                chunk, chunks = event.detail.get("chunk"), event.detail.get("chunks")
                description = f"Switch · {chunk}/{chunks}" if chunk and chunks else "Switch"
            else:
                return

            if eta_s is not None:
                description += f" · ~{_format_eta(eta_s)}"
            progress.update(task, completed=completed, description=description)
            progress.refresh()

        yield report
