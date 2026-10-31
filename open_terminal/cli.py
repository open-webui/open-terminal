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

DEFAULT_CORS_ALLOWED_ORIGINS = "*"


def resolve_cors_allowed_origins(cli_value: str | None, cfg: dict) -> str:
    """Resolve the allowed CORS origins for ``open-terminal run``.

    Precedence (highest wins):

    1. ``--cors-allowed-origins`` CLI flag
    2. ``OPEN_TERMINAL_CORS_ALLOWED_ORIGINS`` environment variable
    3. ``cors_allowed_origins`` from the config file(s)
    4. Built-in default (``"*"``)

    Click resolves levels 1 and 2 into *cli_value*.  The option deliberately
    has no Click-level default, so ``None`` means "not supplied" and any
    explicit value -- including ``"*"`` or ``""`` -- is returned unchanged.
    """
    if cli_value is not None:
        return cli_value

    value = cfg.get("cors_allowed_origins")
    if value is None:
        return DEFAULT_CORS_ALLOWED_ORIGINS
    if not isinstance(value, str):
        raise click.BadParameter(
            "expected a comma-separated string, got "
            f"{type(value).__name__} {value!r}",
            param_hint="'cors_allowed_origins' in config file",
        )
    return value


@main.command()
@click.option("--host", default=None, help="Bind host (default: 127.0.0.1 with --cftunnel, otherwise 0.0.0.0)")
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
    # No Click default: a default here would always win over the config file.
    default=None,
    envvar="OPEN_TERMINAL_CORS_ALLOWED_ORIGINS",
    help=(
        "Allowed CORS origins, comma-separated (or set "
        "OPEN_TERMINAL_CORS_ALLOWED_ORIGINS; falls back to the config file, "
        "then * for all)"
    ),
)
@click.option(
    "--cftunnel", is_flag=True,
    help="Publish a temporary public HTTPS URL using Cloudflare. Requires a configured API key; offers to download cloudflared if needed.",
)
@click.option("--yes", is_flag=True, help="Approve the pinned cloudflared download (requires --cftunnel).")
def run(
    host: str | None,
    port: int | None,
    config_path: str | None,
    cwd: str | None,
    api_key: str,
    cors_allowed_origins: str | None,
    cftunnel: bool,
    yes: bool,
):
    """Start the sandbox API server."""
    import secrets
    import re

    from open_terminal import config

    if yes and not cftunnel:
        raise click.UsageError("--yes requires --cftunnel.")

    # Load config files before resolving other settings.
    cfg = config.init(config_path)

    # Resolve host/port: CLI flag > config file > built-in default
    host = host or cfg.get("host", "127.0.0.1" if cftunnel else "0.0.0.0")
    port = port if port is not None else cfg.get("port", 8000)
    if cftunnel and (type(port) is not int or not 1 <= port <= 65535):
        raise click.UsageError("--cftunnel requires a port between 1 and 65535.")

    # Resolve CORS origins: CLI flag > env var > config file > built-in default
    cors_allowed_origins = resolve_cors_allowed_origins(cors_allowed_origins, cfg)

    if cwd:
        os.chdir(cwd)

    explicit_key = click.get_current_context().get_parameter_source("api_key") == click.core.ParameterSource.COMMANDLINE
    if not explicit_key:
        file_path = os.environ.get("OPEN_TERMINAL_API_KEY_FILE")
        if "OPEN_TERMINAL_API_KEY" in os.environ and "OPEN_TERMINAL_API_KEY_FILE" in os.environ:
            raise click.UsageError("OPEN_TERMINAL_API_KEY and OPEN_TERMINAL_API_KEY_FILE are mutually exclusive.")
        if "OPEN_TERMINAL_API_KEY" in os.environ:
            api_key = os.environ["OPEN_TERMINAL_API_KEY"]
        elif file_path:
            try:
                with open(file_path) as f:
                    api_key = f.read().strip()
            except (OSError, UnicodeError) as exc:
                raise click.ClickException(f"Could not read API key file: {exc}") from exc
        else:
            api_key = cfg.get("api_key", "")

    if cftunnel and (not isinstance(api_key, str) or not re.fullmatch(r"[A-Za-z0-9._~+/-]+=*", api_key)):
        raise click.UsageError("--cftunnel requires a configured, nonblank bearer API key. Set --api-key, OPEN_TERMINAL_API_KEY, OPEN_TERMINAL_API_KEY_FILE, or api_key in TOML.")
    if not cftunnel and not api_key:
        api_key = cfg.get("api_key", "")

    generated = not api_key
    if not api_key:
        api_key = secrets.token_urlsafe(48)

    os.environ["OPEN_TERMINAL_API_KEY"] = api_key
    # The CLI has already resolved _FILE; env.py must not see both sources.
    os.environ.pop("OPEN_TERMINAL_API_KEY_FILE", None)
    os.environ["OPEN_TERMINAL_CORS_ALLOWED_ORIGINS"] = cors_allowed_origins

    if cftunnel:
        from open_terminal.tunnel import origin_url, resolve_binary, run_server
        binary = resolve_binary(yes=yes)

    click.echo(BANNER)

    # -- Startup info block --
    local_url = f"http://{'localhost' if host in ('0.0.0.0', '127.0.0.1') else host}:{port}"
    if cftunnel:
        local_url = origin_url(host, port)

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

    if host in ("0.0.0.0", "::"):
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

    from open_terminal.env import UVICORN_LOOP, UVICORN_TIMEOUT_KEEP_ALIVE

    uvicorn_options = {}
    if UVICORN_TIMEOUT_KEEP_ALIVE is not None:
        uvicorn_options["timeout_keep_alive"] = UVICORN_TIMEOUT_KEEP_ALIVE
    if cftunnel:
        run_server(binary, host, port, loop=UVICORN_LOOP, **uvicorn_options)
        return
    uvicorn.run(
        "open_terminal.main:app", host=host, port=port, loop=UVICORN_LOOP,
        **uvicorn_options,
    )


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
def mcp(
    transport: str,
    host: str | None,
    port: int | None,
    config_path: str | None,
    cwd: str | None,
):
    """Start the MCP server (requires 'pip install open-terminal[mcp]')."""
    from open_terminal import config

    cfg = config.init(config_path)

    host = host or cfg.get("host", "0.0.0.0")
    port = port if port is not None else cfg.get("port", 8000)

    if cwd:
        os.chdir(cwd)

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
