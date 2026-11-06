# Temporary public access

[← README](../README.md)

Add `--cftunnel` to publish the REST API at a temporary HTTPS URL:

```bash
# Set OPEN_TERMINAL_API_KEY securely, or set api_key in your TOML config first.
uvx open-terminal run --cftunnel

# Approve downloading cloudflared when running without an interactive terminal.
uvx open-terminal run --cftunnel --yes
```

An explicitly configured API key is required: use `OPEN_TERMINAL_API_KEY`,
`OPEN_TERMINAL_API_KEY_FILE`, TOML `api_key`, or `--api-key`. Environment variables
and TOML avoid exposing the key in command arguments. Unlike ordinary `run`, this
mode does not generate or print a key. The key is for Open Terminal; no Cloudflare
account, domain, or Cloudflare API key is needed.

On first use, Open Terminal uses `cloudflared` from `PATH` if available. Otherwise,
it offers to download a pinned official release after showing its source and
Cloudflare's license/terms links. The prompt defaults to no. `--yes` approves that
download and is valid only with `--cftunnel`. In noninteractive sessions, install
`cloudflared` yourself or supply `--yes`. Verified cached installations are reused
without prompting or checking for a newer release.

Managed downloads support macOS and Linux on x86-64/ARM64, and Windows on x86-64.
Other platforms need an executable on `PATH`. Both download and executable hashes
are checked; cached executables are verified on every launch. An executable found
on `PATH` is managed by you and does not receive this checksum verification.
The managed version and hashes are updated with Open Terminal releases.

Caches contain version/platform subdirectories under:

| Platform | Cache location |
|---|---|
| Linux | `$XDG_CACHE_HOME/open-terminal`, or `~/.cache/open-terminal` |
| macOS | `~/Library/Caches/open-terminal` |
| Windows | `%LOCALAPPDATA%\open-terminal\Cache`, or the corresponding `AppData\Local` directory under your home |

The server defaults to `127.0.0.1` in this mode. An explicit `--host` or configured
`host` still takes precedence. Use a fixed port between 1 and 65535. Once the
public health check succeeds, the console prints `Public: https://….trycloudflare.com`.
Point your client at that URL and configure its existing bearer API key separately.
Terminal WebSockets retain first-message authentication.

Press Ctrl+C to stop both the API and tunnel. If tunnel startup fails or the
connector exits, Open Terminal stops instead of silently continuing locally.
Transient connection failures are handled by `cloudflared`'s own reconnection.
The connector uses HTTP/2 so this HTTP API does not depend on QUIC/UDP connectivity.
Existing Cloudflare configuration and tunnel environment overrides are isolated
from this launch. No browser, background service, or persistent hostname is created.

> [!CAUTION]
> This publishes the API running on your machine. Anyone holding the key can run
> commands and access files with your permissions. Health, capability discovery,
> and API documentation remain public; the OpenAPI schema includes some host
> metadata. CORS remains configurable with `--cors-allowed-origins` and is not a
> replacement for API-key authentication.

[Quick Tunnels](https://developers.cloudflare.com/tunnel/get-started/quick-tunnels/)
are for temporary development use: hostnames change on each launch, there is no
uptime guarantee, at most 200 requests can be in flight, and SSE is unsupported.
Command-output polling works without SSE; proxied applications using SSE remain
subject to this limitation. The local machine and both processes must stay running.
This option applies to `run`, not the MCP command.

If setup fails:

- **Download blocked or cache unwritable:** check network access to official
  GitHub release downloads and cache permissions, or
  [install cloudflared manually](https://developers.cloudflare.com/tunnel/downloads/).
- **Checksum mismatch:** nothing unverified is executed. Retry the verified
  download; do not bypass verification. A corrupt cache requires download consent
  again.
- **Installed executable incompatible:** update `cloudflared` on `PATH`, or remove
  that installation from `PATH` to use the managed version.
- **Startup timeout:** inspect the displayed connector diagnostics and local port
  binding. Ensure outbound Cloudflare Tunnel connectivity is allowed. A public URL
  is not announced until its HTTPS health check succeeds. The last health-check
  error identifies DNS/TLS failures; the assigned `trycloudflare.com` hostname must
  resolve from this machine. A DNS resolver may briefly cache a not-yet-published
  hostname as missing even after the connector has registered successfully.
