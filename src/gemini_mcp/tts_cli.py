"""`gemini tts`: make one local MP3 through the configured Gemini backend."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from gemini_router import Router
from gemini_router.cli_progress import tts_progress
from gemini_router.config import Config as RouterConfig
from rich.prompt import Prompt

from .backends import make_routers, resolve_mode
from .config import AppCfg
from .fleet_profile import FleetProfile, load_profile, save_profile
from .tts_client import FleetProxy, ServerProxyTunnel


def _profile(args, console) -> FleetProfile:
    saved = load_profile()
    host = (args.vps_host or (saved.host if saved else "")).strip()
    user = (args.vps_user or (saved.user if saved else "root")).strip() or "root"
    if not host and sys.stdin.isatty():
        host = Prompt.ask("VPS host/IP for the Gemini fleet", console=console).strip()
        user = Prompt.ask("SSH user on the VPS", default=user, console=console).strip() or "root"
    if not host:
        raise ValueError(
            "fleet host is not configured; run `gemini setup --backend router` "
            "or pass `--vps-host <host>`"
        )
    if not saved or saved.host != host or saved.user != user:
        save_profile(host, user)
    return FleetProfile(host=host, user=user)


async def _synthesize(router: Router, args, source: Path, output: Path, on_event):
    try:
        return await router.synthesize_file(
            source,
            output_path=output,
            voice=args.voice,
            style=args.style,
            bitrate_kbps=args.bitrate,
            wait=False if args.no_wait else None,
            source="gemini-cli",
            initiator="gemini-cli",
            request_id=args.request_id,
            on_event=on_event,
        )
    finally:
        await router.aclose()


def _run_with_router(router: Router, args, source: Path, output: Path, on_event):
    return asyncio.run(_synthesize(router, args, source, output, on_event))


def _remote_router_config(router_cfg: RouterConfig, proxy: FleetProxy) -> RouterConfig:
    return router_cfg.model_copy(
        update={
            "api_keys": [proxy.token],
            "base_url": proxy.base_url,
            "upload_base_url": proxy.base_url.replace("/v1beta", "/upload/v1beta"),
            "state_path": ":memory:",
        }
    )


def run_tts(args, app_cfg: AppCfg, router_cfg: RouterConfig, console) -> int:
    source = Path(args.file).expanduser()
    if not source.is_file():
        console.print(f"[red]input file not found:[/red] {source}")
        return 2
    if source.suffix.lower() not in {".txt", ".md"}:
        console.print("[red]TTS supports UTF-8 .txt and .md files only[/red]")
        return 2
    try:
        has_content = False
        with source.open("r", encoding="utf-8-sig") as stream:
            while chunk := stream.read(8192):
                has_content |= bool(chunk.strip())
    except UnicodeDecodeError:
        console.print("[red]TTS input must be UTF-8 text[/red]")
        return 2
    except OSError as exc:
        console.print(f"[red]could not read input file:[/red] {exc}")
        return 2
    if not has_content:
        console.print("[red]TTS input file is empty[/red]")
        return 2
    if not 32 <= args.bitrate <= 320:
        console.print("[red]MP3 bitrate must be between 32 and 320 kbps[/red]")
        return 2
    output = Path(args.output).expanduser() if args.output else source.with_suffix(".mp3")
    mode = args.backend or resolve_mode(app_cfg, router_cfg)

    if mode == "router":
        profile = _profile(args, console)
        console.print(
            f"[dim]Connecting to the Gemini fleet at {profile.user}@{profile.host}…[/dim]"
        )
        with ServerProxyTunnel(profile) as proxy:
            console.print(
                f"[dim]VPS router {proxy.server_version}; forwarding privately over SSH[/dim]"
            )
            router = Router(_remote_router_config(router_cfg, proxy))
            with tts_progress(console, enabled=not args.json) as on_event:
                result = _run_with_router(router, args, source, output, on_event)
    else:
        router, optimal, mode = make_routers(app_cfg, router_cfg)
        try:
            with tts_progress(console, enabled=not args.json) as on_event:
                result = _run_with_router(router, args, source, output, on_event)
        finally:
            if optimal:
                asyncio.run(optimal.aclose())

    payload = {
        "ok": True,
        "path": str(result.path),
        "model": result.model,
        "chunks": result.chunks,
        "tokens_in": result.tokens_in,
        "key_ids": result.key_ids,
        "latency_s": round(result.latency_s, 2),
        "backend": mode,
    }
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        console.print(
            f"[green]Wrote {result.path}[/green] · {result.chunks} chunk(s) · "
            f"{result.tokens_in} input tokens · {result.latency_s:.1f}s · "
            f"backend={mode}"
        )
    return 0
