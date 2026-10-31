"""Short-lived SSH bridge from the local TTS CLI to the VPS router proxy."""

from __future__ import annotations

import json
import socket
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

from .fleet_profile import FleetProfile


class FleetTunnelError(RuntimeError):
    """The local CLI could not establish an authenticated route to the VPS."""


@dataclass(frozen=True, slots=True)
class FleetProxy:
    token: str
    base_url: str
    server_version: str


_TOKEN_COMMAND = (
    'if [ "$(id -u)" -eq 0 ]; then '
    'awk -F= \'$1 == "GEMINI_ROUTER_PROXY_TOKEN" {sub(/^[^=]*=/, ""); print; exit}\' '
    "/opt/apps/gemini-mcp/.env; "
    'else sudo -n awk -F= \'$1 == "GEMINI_ROUTER_PROXY_TOKEN" '
    '{sub(/^[^=]*=/, ""); print; exit}\' /opt/apps/gemini-mcp/.env; fi'
)


def _ssh_options() -> list[str]:
    return [
        "ssh",
        "-T",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=10",
        "-o",
        "StrictHostKeyChecking=yes",
    ]


def _target(profile: FleetProfile) -> str:
    return f"{profile.user}@{profile.host}"


def _read_proxy_token(profile: FleetProfile) -> str:
    try:
        result = subprocess.run(
            [*_ssh_options(), _target(profile), _TOKEN_COMMAND],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise FleetTunnelError(
            "Could not read the VPS router token over SSH; "
            "check that OpenSSH is installed and the saved host/key "
            "work."
        ) from exc
    token = result.stdout.strip()
    if result.returncode != 0 or not token or any(c.isspace() for c in token):
        detail = result.stderr.strip().splitlines()[-1][:180] if result.stderr.strip() else ""
        suffix = f" ({detail})" if detail else ""
        raise FleetTunnelError(
            "Could not read GEMINI_ROUTER_PROXY_TOKEN on the VPS" + suffix + ". "
            "The SSH user needs read access to the gateway environment file."
        )
    return token


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _health(port: int) -> dict | None:
    req = urllib.request.Request(f"http://127.0.0.1:{port}/healthz")
    try:
        with urllib.request.urlopen(req, timeout=1.5) as response:
            data = json.loads(response.read(4096))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError, urllib.error.URLError):
        return None


class ServerProxyTunnel:
    """Acquire the proxy token, open a loopback-only SSH forward, and always close it."""

    def __init__(self, profile: FleetProfile):
        self.profile = profile
        self.process: subprocess.Popen | None = None

    def __enter__(self) -> FleetProxy:
        token = _read_proxy_token(self.profile)
        port = _free_port()
        command = [
            *_ssh_options(),
            "-N",
            "-o",
            "ExitOnForwardFailure=yes",
            "-o",
            "ServerAliveInterval=30",
            "-o",
            "ServerAliveCountMax=2",
            "-L",
            f"127.0.0.1:{port}:127.0.0.1:8787",
            _target(self.profile),
        ]
        try:
            self.process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
            )
        except OSError as exc:
            raise FleetTunnelError(
                "Could not start the SSH tunnel; check that OpenSSH is installed."
            ) from exc
        deadline = time.monotonic() + 12
        health: dict | None = None
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                detail = self._stderr()
                self.close()
                suffix = f" ({detail})" if detail else ""
                raise FleetTunnelError(
                    "SSH could not open the VPS tunnel" + suffix + ". "
                    "Check the saved host and SSH host key."
                )
            health = _health(port)
            if health and health.get("ok") and health.get("service") == "gemini-router-proxy":
                return FleetProxy(
                    token=token,
                    base_url=f"http://127.0.0.1:{port}/v1beta",
                    server_version=str(health.get("version", "unknown")),
                )
            time.sleep(0.2)
        self.close()
        raise FleetTunnelError(
            "The VPS tunnel opened, but the Gemini Router proxy did not answer "
            "its health check on port 8787."
        )

    def _stderr(self) -> str:
        if not self.process or not self.process.stderr:
            return ""
        try:
            line = self.process.stderr.readline().strip()
            return line[:180]
        except OSError:
            return ""

    def close(self) -> None:
        process, self.process = self.process, None
        if not process:
            return
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
        if process.stderr:
            process.stderr.close()

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()
