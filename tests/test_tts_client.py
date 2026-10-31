from __future__ import annotations

import io
import subprocess
from types import SimpleNamespace

import pytest

from gemini_mcp import tts_client
from gemini_mcp.fleet_profile import FleetProfile


def test_token_command_is_an_ssh_argument_after_the_target(monkeypatch):
    calls = []
    monkeypatch.setattr(
        tts_client.subprocess,
        "run",
        lambda cmd, **kw: (
            calls.append((cmd, kw))
            or SimpleNamespace(returncode=0, stdout="secret-token\n", stderr="")
        ),
    )
    token = tts_client._read_proxy_token(FleetProfile("test-vps", "deploy"))
    cmd, kwargs = calls[0]
    assert token == "secret-token"
    assert cmd[0] == "ssh" and cmd.index("-o") < cmd.index("deploy@test-vps")
    assert cmd[-2] == "deploy@test-vps" and cmd[-1] == tts_client._TOKEN_COMMAND
    assert kwargs["timeout"] == 15 and kwargs["check"] is False


def test_proxy_token_is_not_included_in_failure_message(monkeypatch):
    secret = "never-print-this-token"
    monkeypatch.setattr(
        tts_client.subprocess,
        "run",
        lambda *a, **kw: SimpleNamespace(returncode=1, stdout=secret, stderr="permission denied"),
    )
    with pytest.raises(tts_client.FleetTunnelError) as err:
        tts_client._read_proxy_token(FleetProfile("test-vps"))
    assert secret not in str(err.value)


def test_proxy_token_rejects_multiline_shell_output(monkeypatch):
    monkeypatch.setattr(
        tts_client.subprocess,
        "run",
        lambda *a, **kw: SimpleNamespace(returncode=0, stdout="token\nwarning", stderr=""),
    )
    with pytest.raises(tts_client.FleetTunnelError):
        tts_client._read_proxy_token(FleetProfile("test-vps"))


def test_tunnel_binds_loopback_and_closes(monkeypatch):
    commands = []

    class FakeProcess:
        def __init__(self, cmd, **kwargs):
            commands.append((cmd, kwargs))
            self.stderr = io.StringIO("")
            self.terminated = False

        def poll(self):
            return None

        def terminate(self):
            self.terminated = True

        def wait(self, timeout=None):
            return 0

        def kill(self):
            self.terminated = True

    monkeypatch.setattr(tts_client, "_read_proxy_token", lambda profile: "memory-only")
    monkeypatch.setattr(tts_client, "_free_port", lambda: 41237)
    monkeypatch.setattr(
        tts_client,
        "_health",
        lambda port: {"ok": True, "service": "gemini-router-proxy", "version": "0.13.0"},
    )
    monkeypatch.setattr(tts_client.subprocess, "Popen", FakeProcess)

    tunnel = tts_client.ServerProxyTunnel(FleetProfile("test-vps", "deploy"))
    with tunnel as proxy:
        assert proxy.base_url == "http://127.0.0.1:41237/v1beta"
        assert proxy.token == "memory-only"
    cmd, kwargs = commands[0]
    destination = cmd.index("deploy@test-vps")
    forward = cmd.index("-L")
    assert forward < destination and cmd[-1] == "deploy@test-vps"
    assert cmd[forward + 1] == "127.0.0.1:41237:127.0.0.1:8787"
    assert "-N" in cmd and kwargs["stdin"] == subprocess.DEVNULL
    assert tunnel.process is None
