import os

import click
import uvicorn


@click.group()
def main():
    """open-terminal — terminal interaction API"""
    pass


BANNER = r"""
   ____                    _____                   _             _
  / __ \                  |_   _|                 (_)           | |
 | |  | |_ __   ___ _ __   | | ___ _ __ _ __ ___  _ _ __   __ _| |
 | |  | | '_ \ / _ | '_ \  | |/ _ | '__| '_ ` _ \| | '_ \ / _` | |
 | |__| | |_) |  __| | | | | |  __| |  | | | | | | | | | | (_| | |
  \____/| .__/ \___|_| |_| \_/\___|_|  |_| |_| |_|_|_| |_|\__,_|_|
        | |
        |_|
"""


@main.command()
@click.option("--host", default=None, help="Bind host (default: 0.0.0.0)")
@click.option("--port", default=None, type=int, help="Bind port (default: 8000)")
@click.option(
    "--config",
    "config_path",
    default=None,
    type=click.Path(exists=True, dir_okay=False, resolve_path=True),
    help="Path to a TOML config file (overrides user-level config location).",
)
@click.option(
    "--cwd",
    type=click.Path(exists=True, file_okay=False, resolve_path=True, path_type=str),
    default=None,
    help="Working directory for the server process.",
)
@click.option(
    "--api-key",
    default="",
    envvar="OPEN_TERMINAL_API_KEY",
    help="Bearer API key (or set OPEN_TERMINAL_API_KEY env var)",
)
@click.option(
    "--cors-allowed-origins",
    default="*",
    envvar="OPEN_TERMINAL_CORS_ALLOWED_ORIGINS",
    help="Allowed CORS origins, comma-separated (default: * for all)",
)
def run(
    host: str | None,
    port: int | None,
    config_path: str | None,
    cwd: str | None,
    api_key: str,
    cors_allowed_origins: str,
):
    """Start the sandbox API server."""
    import secrets

    from open_terminal import config

    # Load config files before resolving other settings.
    cfg = config.init(config_path)

    # Resolve host/port: CLI flag > config file > built-in default
    host = host or cfg.get("host", "0.0.0.0")
    port = port if port is not None else cfg.get("port", 8000)

    if cwd:
        os.chdir(cwd)

    # Support Docker secrets: load from _FILE variant if no key was given
    if not api_key:
        file_path = os.environ.get("OPEN_TERMINAL_API_KEY_FILE")
        if file_path:
            with open(file_path) as f:
                api_key = f.read().strip()

    # Fall back to config file value
    if not api_key:
        api_key = cfg.get("api_key", "")

    generated = not api_key
    if not api_key:
        api_key = secrets.token_urlsafe(48)

    os.environ["OPEN_TERMINAL_API_KEY"] = api_key
    os.environ["OPEN_TERMINAL_CORS_ALLOWED_ORIGINS"] = cors_allowed_origins

    click.echo(BANNER)

    # -- Startup info block --
    local_url = f"http://{'localhost' if host in ('0.0.0.0', '127.0.0.1') else host}:{port}"

    click.echo(f"  {click.style('Local:', bold=True)}    {click.style(local_url, fg='cyan')}")
    if host == "0.0.0.0":
        import socket
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            network_ip = s.getsockname()[0]
            s.close()
            click.echo(f"  {click.style('Network:', bold=True)}  http://{network_ip}:{port}")
        except Exception:
            pass
    click.echo()

    if generated:
        click.echo(f"  {click.style('API Key:', bold=True)}  {api_key}")
        click.echo()

    if host == "0.0.0.0":
        click.echo(click.style("  Warning: Listening on all network interfaces.", fg="yellow"))
        click.echo(click.style("  Use --host 127.0.0.1 to restrict to this machine.", dim=True))
        click.echo()

    if cors_allowed_origins.strip() == "*":
        click.echo(click.style("  ┌─────────────────────────────────────────────────────────────┐", fg="yellow"))
        click.echo(click.style("  │  ⚠  CORS is set to '*' (allow all origins)                 │", fg="yellow"))
        click.echo(click.style("  │                                                             │", fg="yellow"))
        click.echo(click.style("  │  Any website can make requests to this server.              │", fg="yellow"))
        click.echo(click.style("  │  For production, restrict with:                             │", fg="yellow"))
        click.echo(click.style("  │    --cors-allowed-origins https://your-domain.com           │", fg="yellow"))
        click.echo(click.style("  └─────────────────────────────────────────────────────────────┘", fg="yellow"))
        click.echo()

    from open_terminal.env import UVICORN_LOOP
    uvicorn.run("open_terminal.main:app", host=host, port=port, loop=UVICORN_LOOP)


@main.command()
@click.option(
    "--transport",
    default="stdio",
    type=click.Choice(["stdio", "streamable-http"]),
    help="MCP transport (default: stdio)",
)
@click.option("--host", default=None, help="Bind host (streamable-http only)")
@click.option(
    "--port", default=None, type=int, help="Bind port (streamable-http only)"
)
@click.option(
    "--config",
    "config_path",
    default=None,
    type=click.Path(exists=True, dir_okay=False, resolve_path=True),
    help="Path to a TOML config file (overrides user-level config location).",
)
@click.option(
    "--cwd",
    type=click.Path(exists=True, file_okay=False, resolve_path=True, path_type=str),
    default=None,
    help="Working directory for the server process.",
)
@click.option(
    "--api-key",
    default="",
    envvar="OPEN_TERMINAL_API_KEY",
    help="Bearer API key (or set OPEN_TERMINAL_API_KEY env var)",
)
def mcp(
    transport: str,
    host: str | None,
    port: int | None,
    config_path: str | None,
    cwd: str | None,
    api_key: str,
):
    """Start the MCP server (requires 'pip install open-terminal[mcp]')."""
    from open_terminal import config

    cfg = config.init(config_path)

    host = host or cfg.get("host", "0.0.0.0")
    port = port if port is not None else cfg.get("port", 8000)

    if cwd:
        os.chdir(cwd)

    # Resolve API key: CLI flag > env var > Docker secret file > config file.
    # Must be set in os.environ BEFORE importing mcp_server so that env.py
    # picks it up at module-import time (it captures API_KEY at import).
    if not api_key:
        file_path = os.environ.get("OPEN_TERMINAL_API_KEY_FILE")
        if file_path:
            with open(file_path) as f:
                api_key = f.read().strip()
    if not api_key:
        api_key = cfg.get("api_key", "")
    if api_key:
        os.environ["OPEN_TERMINAL_API_KEY"] = api_key

    try:
        from open_terminal.mcp_server import mcp as mcp_server
    except ImportError:
        click.echo(
            "Missing MCP dependencies. Install with:\n"
            "  pip install open-terminal[mcp]",
            err=True,
        )
        raise SystemExit(1)

    mcp_server.run(transport=transport, host=host, port=port)


if __name__ == "__main__":
    main()
