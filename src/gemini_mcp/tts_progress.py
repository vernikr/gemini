"""Rich terminal progress for gateway TTS, driven by router file-progress events."""
from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager

from gemini_router.events import RouterEvent
from rich.console import Console
from rich.progress import BarColumn, Progress, SpinnerColumn, TaskProgressColumn, TextColumn


@contextmanager
def tts_progress(
    console: Console, *, enabled: bool = True
) -> Iterator[Callable[[RouterEvent], None]]:
    """Render deterministic file-level TTS progress from router events.

    The percentage advances on completed synthesis chunks and during MP3 encoding.
    Routing, quota waits, and retries update the label without estimating elapsed work.
    """
    progress = Progress(
        SpinnerColumn("dots"),
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
            progress.update(task, completed=completed, description=event.message())

        yield report
