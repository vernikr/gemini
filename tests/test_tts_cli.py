from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from rich.console import Console

from conftest import make_app_cfg, real_router_cfg
from gemini_mcp import tts_cli
from gemini_mcp.tts_client import FleetProxy


def _args(source: Path, **overrides):
    values = {
        "file": source,
        "output": None,
        "voice": None,
        "style": None,
        "bitrate": 128,
        "no_wait": False,
        "request_id": None,
        "backend": "router",
        "vps_host": None,
        "vps_user": None,
        "json": True,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _result(path: Path):
    return SimpleNamespace(
        path=path,
        model="gemini-3.8-flash-tts",
        chunks=1,
        tokens_in=12,
        key_ids=["key-id"],
        latency_s=1.25,
    )


def test_router_mode_uses_ephemeral_proxy_token_and_server_settings(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("GEMINI_MCP_HOME", str(tmp_path / "home"))
    source = tmp_path / "story.md"
    source.write_text("Hello world.")
    app_cfg = make_app_cfg(tmp_path / "logs")
    router_cfg = real_router_cfg(tmp_path)
    router_cfg = router_cfg.model_copy(update={"api_keys": ["local-key-must-not-be-used"]})
    calls = {}
    proxy = FleetProxy("ephemeral-token", "http://127.0.0.1:41237/v1beta", "0.13.0")

    class FakeTunnel:
        def __init__(self, profile):
            calls["profile"] = profile

        def __enter__(self):
            return proxy

        def __exit__(self, *exc):
            calls["closed"] = True

    class FakeRouter:
        def __init__(self, cfg):
            calls["config"] = cfg

    monkeypatch.setattr(tts_cli, "ServerProxyTunnel", FakeTunnel)
    monkeypatch.setattr(tts_cli, "Router", FakeRouter)
    monkeypatch.setattr(
        tts_cli,
        "_run_with_router",
        lambda router, args, src, out, console: (
            calls.update(source=src, output=out) or _result(out)
        ),
    )

    rc = tts_cli.run_tts(
        _args(source, vps_host="fleet.example", vps_user="deploy"), app_cfg, router_cfg, Console()
    )
    cfg = calls["config"]
    assert rc == 0 and calls["closed"]
    assert cfg.api_keys == ["ephemeral-token"]
    assert cfg.base_url == proxy.base_url
    assert cfg.upload_base_url == "http://127.0.0.1:41237/upload/v1beta"
    assert cfg.state_path == ":memory:"
    assert calls["source"] == source and calls["output"] == source.with_suffix(".mp3")
    assert "ephemeral-token" not in capsys.readouterr().out


def test_direct_mode_does_not_create_ssh_tunnel(tmp_path, monkeypatch):
    source = tmp_path / "story.txt"
    source.write_text("Hello world.")
    app_cfg = make_app_cfg(tmp_path / "logs")
    router_cfg = real_router_cfg(tmp_path)
    router = object()
    monkeypatch.setattr(
        tts_cli,
        "ServerProxyTunnel",
        lambda *a: (_ for _ in ()).throw(AssertionError("direct mode must not open an SSH tunnel")),
    )
    monkeypatch.setattr(tts_cli, "make_routers", lambda *a: (router, None, "direct"))
    monkeypatch.setattr(tts_cli, "_run_with_router", lambda *a: _result(source.with_suffix(".mp3")))

    rc = tts_cli.run_tts(_args(source, backend="direct"), app_cfg, router_cfg, Console())
    assert rc == 0


def test_invalid_file_fails_before_router_or_ssh(tmp_path, monkeypatch):
    app_cfg = make_app_cfg(tmp_path / "logs")
    router_cfg = real_router_cfg(tmp_path)
    monkeypatch.setattr(
        tts_cli,
        "ServerProxyTunnel",
        lambda *a: (_ for _ in ()).throw(
            AssertionError("invalid input must fail before connecting")
        ),
    )
    rc = tts_cli.run_tts(_args(tmp_path / "missing.pdf"), app_cfg, router_cfg, Console())
    assert rc == 2

    invalid = tmp_path / "invalid.txt"
    invalid.write_bytes(b"\xff")
    assert tts_cli.run_tts(_args(invalid), app_cfg, router_cfg, Console()) == 2

    empty = tmp_path / "empty.md"
    empty.write_text("  \n")
    assert tts_cli.run_tts(_args(empty), app_cfg, router_cfg, Console()) == 2
