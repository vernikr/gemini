from io import StringIO

from gemini_router.events import RouterEvent
from rich.console import Console

from gemini_mcp.tts_progress import tts_progress


def test_progress_bar_uses_router_events_and_finishes_at_100():
    output = StringIO()
    console = Console(file=output, force_terminal=True, width=100, color_system=None)

    with tts_progress(console) as report:
        report(RouterEvent(stage="file_progress", pct=0,
                           detail={"label": "Synthesizing audio · 0/2 chunks"}))
        report(RouterEvent(stage="file_progress", pct=50,
                           detail={"label": "Synthesizing audio · 1/2 chunks"}))
        report(RouterEvent(stage="file_progress", pct=40,
                           detail={"label": "Waiting for quota"}))
        report(RouterEvent(stage="file_progress", pct=100,
                           detail={"label": "Encoding MP3"}))

    rendered = output.getvalue()
    assert "Encoding MP3" in rendered
    assert "100%" in rendered
