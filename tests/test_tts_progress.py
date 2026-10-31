from io import StringIO

from gemini_router.events import RouterEvent
from rich.console import Console

from gemini_mcp.tts_progress import _format_eta, tts_progress


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


def test_eta_format_is_compact():
    assert _format_eta(8) == "8s"
    assert _format_eta(84) == "1:24"
    assert _format_eta(3_840) == "1h 04m"


def test_progress_uses_short_status_and_approximate_eta():
    output = StringIO()
    console = Console(file=output, force_terminal=True, width=100, color_system=None)

    with tts_progress(console) as report:
        report(RouterEvent(
            stage="file_progress", pct=62,
            detail={"label": "TTS · 3/8", "eta_s": 84},
        ))
        report(RouterEvent(stage="waiting", wait_s=18, detail={"reason": "rpm"}))
        report(RouterEvent(
            stage="file_progress", pct=95,
            detail={"label": "Encoding MP3", "eta_s": 8},
        ))

    rendered = output.getvalue()
    assert "TTS · 3/8 · ~1:24" in rendered
    assert "Waiting · ~18s" in rendered
    assert "Encoding MP3 · ~8s" in rendered
