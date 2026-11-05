# ⚡ Open Terminal

A lightweight, self-hosted terminal that gives AI agents and automation tools a dedicated environment to run commands, manage files, and execute code — all through a simple API.

## Why Open Terminal?

AI assistants are great at writing code, but they need somewhere to *run* it. Open Terminal is that place — a remote shell with file management, search, and more, accessible over a simple REST API.

You can run it two ways:

- **Docker (sandboxed)** — runs in an isolated container with a full toolkit pre-installed: Python, Node.js, git, build tools, data science libraries, ffmpeg, and more. Great for giving AI agents a safe playground without touching your host system.
- **Bare metal** — install it with `pip` and run it anywhere Python runs. Commands run directly on your machine with access to your real files, your real tools, and your real environment, perfect for local development, personal automation, or giving an AI assistant full access to your actual projects.

## Getting Started

### Docker (recommended)

```bash
docker run -d --name open-terminal --restart unless-stopped -p 8000:8000 -v open-terminal:/home/user -e OPEN_TERMINAL_API_KEY=your-secret-key ghcr.io/open-webui/open-terminal
```

That's it — you're up and running at `http://localhost:8000`.

> [!TIP]
> If you don't set an API key, one is generated automatically. Grab it with `docker logs open-terminal`.

#### Image Variants

| | `latest` | `slim` | `alpine` | `openshift` |
|---|---|---|---|---|
| **Best for** | AI agent sandboxes | Production / hardened | Edge / CI / minimal footprint | OpenShift restricted SCC |
| **Size** | ~4 GB | ~430 MB | ~230 MB | ~430 MB |
| **Bundled tooling** | Node.js, gcc, ffmpeg, LibreOffice, LaTeX, Docker CLI, data science libs | git, curl, jq | git, curl, jq | git, curl, jq |
| **Install packages at runtime** | ✔ (has `sudo`) | ✘ | ✘ | ✘ |
| **Multi-user mode** | ✔ | ✘ | ✘ | ✘ |
| **Egress firewall** | ✔ | ✔ | ✔ | ✘ |

**`slim`** and **`alpine`** have the same feature set. Slim uses Debian (glibc) for broader binary compatibility; Alpine uses musl libc and is smaller, but some C-extension pip packages may need to compile from source.

```bash
docker run -d -p 8000:8000 -e OPEN_TERMINAL_API_KEY=secret ghcr.io/open-webui/open-terminal:slim
docker run -d -p 8000:8000 -e OPEN_TERMINAL_API_KEY=secret ghcr.io/open-webui/open-terminal:alpine
docker run -d -p 8000:8000 -e OPEN_TERMINAL_API_KEY=secret ghcr.io/open-webui/open-terminal:openshift
```

> [!NOTE]
> Slim and Alpine don't support `OPEN_TERMINAL_PACKAGES` / `OPEN_TERMINAL_PIP_PACKAGES` / `OPEN_TERMINAL_NPM_PACKAGES`. To add packages, extend [Dockerfile.slim](Dockerfile.slim) or [Dockerfile.alpine](Dockerfile.alpine).

> [!NOTE]
> The default `latest` image includes LibreOffice for Word, Excel, and PowerPoint to PDF conversions. The `slim`, `alpine`, and `openshift` images stay minimal; build a custom image if those variants need Office conversion tools.

> [!NOTE]
> The OpenShift image is for restricted non-root pod policies. It does not support runtime package installs, Docker socket access, the iptables egress firewall, or `OPEN_TERMINAL_MULTI_USER=true`. Build a custom image ahead of time when OpenShift users need extra tools.

#### Updating

```bash
docker pull ghcr.io/open-webui/open-terminal
docker rm -f open-terminal
```

Then re-run the `docker run` command above.

### Bare Metal

No Docker? No problem. Open Terminal is a standard Python package:

```bash
# One-liner with uvx (no install needed)
uvx open-terminal run --host 0.0.0.0 --port 8000 --api-key your-secret-key

# Or install globally with pip
pip install open-terminal
open-terminal run --host 0.0.0.0 --port 8000 --api-key your-secret-key
```

> [!CAUTION]
> On bare metal, commands run directly on your machine with your user's permissions. Use Docker if you want sandboxed execution.

#### Temporary public access with Cloudflare

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

#### Customizing the Docker Environment

The easiest way to add extra packages is with environment variables — no fork needed:

```bash
docker run -d --name open-terminal -p 8000:8000 \
  -e OPEN_TERMINAL_PACKAGES="cowsay figlet" \
  -e OPEN_TERMINAL_PIP_PACKAGES="httpx polars" \
  -e OPEN_TERMINAL_NPM_PACKAGES="typescript tsx" \
  ghcr.io/open-webui/open-terminal
```

| Variable | Description |
|---|---|
| `OPEN_TERMINAL_PACKAGES` | Space-separated list of **apt** packages to install at startup |
| `OPEN_TERMINAL_PIP_PACKAGES` | Space-separated list of **pip** packages to install at startup |
| `OPEN_TERMINAL_NPM_PACKAGES` | Space-separated list of **npm** packages to install globally at startup |

> [!NOTE]
> Packages are installed each time the container starts, so startup will take longer with large package lists. For heavy customization, build a custom image instead.

#### Docker Access

The image includes the Docker CLI, Compose, and Buildx. To let agents build images, run containers, etc., mount the host's Docker socket:

```bash
docker run -d --name open-terminal -p 8000:8000 \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v open-terminal:/home/user \
  ghcr.io/open-webui/open-terminal
```

> [!CAUTION]
> Mounting the Docker socket gives the container **full control over the host's Docker daemon**, which is effectively root access on the host machine. Anyone with access to the terminal can pull/run arbitrary containers (including `--privileged` ones), mount host directories, access host networking, and manage all containers on the host. Only do this in fully trusted environments.

For full control, fork the repo, edit the [Dockerfile](Dockerfile), and build your own image:

```bash
docker build -t my-terminal .
docker run -d --name open-terminal -p 8000:8000 my-terminal
```


## Configuration

Open Terminal can be configured via a TOML config file, environment variables, and CLI flags. Settings are resolved in this order (highest priority wins):

1. **CLI flags** (`--host`, `--port`, `--api-key`, etc.)
2. **Environment variables** (`OPEN_TERMINAL_API_KEY`, etc.)
3. **User config** — `$XDG_CONFIG_HOME/open-terminal/config.toml` (defaults to `~/.config/open-terminal/config.toml`)
4. **System config** — `/etc/open-terminal/config.toml`
5. **Built-in defaults**

Create a config file at either location with any of these keys (all optional):

```toml
host = "0.0.0.0"
port = 8000
api_key = "sk-my-secret-key"
cors_allowed_origins = "*"
log_dir = "/var/log/open-terminal"
binary_mime_prefixes = "image,audio"
execute_timeout = 5  # seconds to wait for command output (unset by default)
file_browser_root = "home"
enable_browser = true
enable_desktop = true
```

Browser and desktop support are enabled by default. Set `OPEN_TERMINAL_ENABLE_BROWSER=false`
or `OPEN_TERMINAL_ENABLE_DESKTOP=false` to disable their HTTP and WebSocket endpoints
and advertise them as disabled in `/api/config`. Values `false`, `0`, and `no`
(case-insensitive) disable the feature. Restart the server after changing these settings.
Desktop support still requires its platform dependencies to be available.

### Use your running Chrome

Browser defaults to `managed`: Open Terminal launches a separate Chrome profile.
To use your existing Chrome tabs and signed-in sessions, select `personal`:

```sh
export OPEN_TERMINAL_BROWSER_MODE=personal
```

In Chrome 144+, enable debugging at `chrome://inspect/#remote-debugging` and approve
the connection when Chrome asks. Open Terminal reads `DevToolsActivePort` from the
current OS user's normal Chrome data directory. To select another instance, set
one of these optional overrides:

```sh
export OPEN_TERMINAL_BROWSER_USER_DATA_DIR="/path/to/chrome-user-data"
# Or connect to an explicit HTTP debugging address or ws:// / wss:// endpoint:
export OPEN_TERMINAL_BROWSER_CDP_URL="http://127.0.0.1:9222"
```

An explicit CDP URL takes precedence over the data directory. If the selected
instance is unavailable, Open Terminal returns an error rather than launching or
choosing another browser. The equivalent TOML keys are `browser_mode`,
`browser_user_data_dir`, and `browser_cdp_url`. Restart Open Terminal after changes.

Clients can list available HTTP/HTTPS tabs with authenticated `GET /browser/targets`,
then attach one with `POST /browser/sessions` and `{"target_id":"<id>"}`. An existing
proxy session can attach using `PATCH /browser/sessions/{id}` with
`{"mode":"chrome","target_id":"<id>"}`. The same browser tools and viewer stream work
on the attachment. Session responses and `/api/config` report `browser_mode`.
Clients should use the stream state's `width` and `height` for personal-tab input
coordinates; viewport and mobile-emulation requests do not resize the real page.

Closing an attachment or stopping Open Terminal leaves personal Chrome and its
tabs open. Personal mode requires single-user operation. It does not automatically
focus background tabs; a tab may need to be brought forward in Chrome for live
viewing. WebUI's tab-selection UI must support these attachment endpoints.

Chromium's sandbox is enabled by default outside Docker. All published Docker image
variants set `OPEN_TERMINAL_BROWSER_NO_SANDBOX=true` so Browser can start under
Docker's default security restrictions when Chromium is installed. This applies to
both managed Browser tabs and Chromium launched from the Linux Desktop in the full
Docker image. Container isolation remains unchanged, but Chromium's own sandbox
no longer protects files and processes inside the container from a browser exploit.

To enable Chromium's sandbox in Docker, pass
`-e OPEN_TERMINAL_BROWSER_NO_SANDBOX=false` when creating the container; the runtime
must permit Chromium's sandbox operations. Outside Docker, set the variable to
`true` only if your deployment cannot support the sandbox. Values `true`, `1`, and
`yes` (case-insensitive) disable the sandbox. Restart or recreate Open Terminal
after changing this setting.

> [!TIP]
> Use the system config at `/etc/open-terminal/config.toml` to set site-wide defaults for host and port, and the user config for personal settings like the API key — this keeps the key out of `ps` / `htop`.

You can also point to a specific config file:

```bash
open-terminal run --config /path/to/my-config.toml
```

### Memory management

File and command-output responses stream from disk, uploads are copied in chunks,
and generated ZIP/PDF files are removed after sending or cancellation. Log rotation
keeps the original newest-half behavior without loading the entire log into memory.
Search listings and ranking use temporary disk storage while preserving full matches.

Process, terminal, notebook, and document-worker cleanup releases readers, file
handles, and child processes on completion, cancellation, or server shutdown.
The existing command-log retention and notebook idle timeout still apply.

### File Browser Root

Open Terminal reports file-browser root metadata from `GET /files/cwd` so clients can hide parent navigation above a friendly starting point.

Set `OPEN_TERMINAL_FILE_BROWSER_ROOT` to:

| Value | Behavior |
|---|---|
| `home` | Default. Report the current user's home directory as `Home` |
| `/workspace` | Report an explicit path as the visual root |
| `{{home}}/project` | Report a path under the current user's home |
| `filesystem` | Opt out and report no visual root metadata |

This is a UI hint for clients. It does not restrict terminal commands or file APIs.

### External Workspace Storage

Open Terminal uses the filesystem it is mounted on. To store workspace files
externally, mount that storage into the container:

- Single-user: `/home/user`
- Multi-user: `/home`

This can be a Docker volume, Kubernetes persistent volume, NFS/Azure Files
mount, or FUSE mount such as blobfuse, s3fs, or rclone.

### Office Previews

`GET /files/view?path=...` always returns the original file by default. Clients
that want a rendered preview can add `preview=true`; when LibreOffice is
available the server can return DOCX and PPTX previews as PDF. When rendered
preview support is not available, the same endpoint falls back to returning the
original file bytes.

## Using with Open WebUI

Open Terminal integrates with [Open WebUI](https://github.com/open-webui/open-webui), giving your AI assistants the ability to run commands, manage files, and interact with a terminal right from the AI interface. Make sure to add it under **Open Terminal** in the integrations settings, not as a tool server. Adding it as an Open Terminal connection gives you a built-in file navigation sidebar where you can browse directories, upload, download, and edit files. There are two ways to connect:

### Direct Connection

Users can connect their own Open Terminal instance from their user settings. This is useful when the terminal is running on their local machine or a network only they can reach, since requests go directly from the **browser**.

1. Go to **User Settings → Integrations → Open Terminal**
2. Add the terminal **URL** and **API key**
3. Enable the connection

### System-Level Connection (Multi-User)

Admins can configure Open Terminal connections for all their users from the admin panel. No additional services required. Multiple terminals can be set up with access controlled at the user or group level. Requests are proxied through the Open WebUI **backend**, so the terminal only needs to be reachable from the server.

1. Go to **Admin Settings → Integrations → Open Terminal**
2. Add the terminal **URL** and **API key**
3. Enable the connection

#### Built-in Multi-User Mode

> [!CAUTION]
> **Multi-user mode separates workspaces. It does not create a security boundary between users, and it is not designed for production multi-user deployments.**
> Everyone shares one container: one kernel, one process list, one network stack, one temp directory. Users can watch each other's running commands, reach ports other users open, exhaust CPU, memory and disk for everyone, and interfere with shared system state. Provisioning accounts requires elevated privileges inside the container, so a user who sets out to reach root inside that container will find a way. Run this mode only where every user on the instance is trusted at the same level, and give each user their own instance whenever that trust does not hold. See [SECURITY.md](SECURITY.md) for how reports about this mode are handled.

For small, trusted deployments you can give each user their own workspace inside a single container:

```bash
docker run -d --name open-terminal -p 8000:8000 \
  -v open-terminal:/home \
  -e OPEN_TERMINAL_MULTI_USER=true \
  -e OPEN_TERMINAL_API_KEY=your-secret-key \
  ghcr.io/open-webui/open-terminal
```

Each user automatically gets a dedicated Linux account with its own home directory, so files, commands, and terminals stay in their own workspace and users do not walk over each other's work. Standard Unix permissions keep the workspaces apart in day-to-day use. They do not protect users from each other, and they are not meant to.

In multi-user mode, requests that access a user's workspace must include `X-User-Id` alongside the API key. Missing user IDs return HTTP 400. `X-Session-Id` tracks the working directory separately for each user, so users can reuse a session ID without changing each other's working directory. Single-user mode does not require `X-User-Id`.

## API Docs

Full interactive API documentation is available at [http://localhost:8000/docs](http://localhost:8000/docs) once your instance is running.

## Star History

<a href="https://star-history.com/#open-webui/open-terminal&Date">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://api.star-history.com/svg?repos=open-webui/open-terminal&type=Date&theme=dark" />
    <source media="(prefers-color-scheme: light)" srcset="https://api.star-history.com/svg?repos=open-webui/open-terminal&type=Date" />
    <img alt="Star History Chart" src="https://api.star-history.com/svg?repos=open-webui/open-terminal&type=Date" />
  </picture>
</a>

> [!TIP]
> **Need real isolation between users?** **[Terminals](https://github.com/open-webui/terminals)** provisions and manages a separate Open Terminal container per user, with its own files, processes, resource limits and network boundary. That is the setup to use when users must be protected from each other. Terminals requires an [Open WebUI Enterprise License](https://docs.openwebui.com/enterprise/) for production use, see the [Terminals documentation](https://docs.openwebui.com/features/open-terminal/terminals/) for deployment and licensing details. Built-in multi-user mode (`OPEN_TERMINAL_MULTI_USER=true`) only gives each user their own workspace inside one shared container.

## License

MIT — see [LICENSE](LICENSE) for details.
