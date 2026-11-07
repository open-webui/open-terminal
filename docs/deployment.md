# Deployment

[← README](../README.md)

Docker is the recommended setup for an isolated environment with tools preinstalled.
Bare metal runs directly on your machine, using your files, tools, environment, and
user permissions. Open Terminal is a standard Python package and runs wherever its
Python requirements are met.

## Image variants

| Tag | Base | Runtime package installs | Multi-user mode | Egress firewall |
| --- | --- | --- | --- | --- |
| `latest` | Full Debian environment | Yes | Yes | Yes |
| `agents` | Full environment + Codex and Claude Code | Yes | Yes | Yes |
| `cuda` | Full environment + CUDA toolkit and PyTorch | Yes | Yes | Yes |
| `slim` | Debian | No | No | Yes |
| `alpine` | Alpine | No | No | Yes |
| `openshift` | Debian, restricted non-root policies | No | No | No |

The full images include Python, Node.js, git, gcc and other build tools, ffmpeg,
Chromium, a Linux desktop, LibreOffice, LaTeX, Docker CLI, and data science libraries.
They include `sudo` for runtime package installation. The minimal images include
git, curl, and jq. Slim and Alpine have the same feature set: Slim uses Debian's
glibc for broader binary compatibility; Alpine uses musl for a smaller footprint,
but some C-extension pip packages may need to compile from source.

Use `latest` for AI agent sandboxes, `agents` for coding agents, `cuda` for NVIDIA GPU
workloads, `slim` for minimal
or hardened production setups, `alpine` for edge/CI deployments, and `openshift`
for OpenShift restricted SCC. The previous README estimated sizes of ~4 GB for
`latest`, ~430 MB for `slim` and `openshift`, and ~230 MB for `alpine`; `agents`
adds the CLIs to the full image. These are historical estimates, not current
measurements; sizes vary by release and architecture.

```bash
docker run -d -p 8000:8000 -e OPEN_TERMINAL_API_KEY=secret ghcr.io/open-webui/open-terminal:slim
docker run -d -p 8000:8000 -e OPEN_TERMINAL_API_KEY=secret ghcr.io/open-webui/open-terminal:alpine
docker run -d -p 8000:8000 -e OPEN_TERMINAL_API_KEY=secret ghcr.io/open-webui/open-terminal:openshift
```

These are alternative commands; run one image per port. Slim, Alpine, and OpenShift
do not support the `OPEN_TERMINAL_PACKAGES`, `OPEN_TERMINAL_PIP_PACKAGES`, or
`OPEN_TERMINAL_NPM_PACKAGES` startup installers.

To add tools to a minimal image, extend [Dockerfile.slim](../Dockerfile.slim),
[Dockerfile.alpine](../Dockerfile.alpine), or [Dockerfile.openshift](../Dockerfile.openshift).
OpenShift also excludes Docker socket access, the iptables egress firewall, and
`OPEN_TERMINAL_MULTI_USER=true`. Build custom images ahead of time when extra tools
are needed. Full images include LibreOffice for Word, Excel, and PowerPoint to PDF
conversion; install it in a custom image if a minimal variant needs these tools.

## Coding agents

The `agents` image supports AMD64 and ARM64, with the same runtime package installs
and multi-user mode as the full image. Start it and open a shell:

```bash
docker run -d --name open-terminal --restart unless-stopped -p 8000:8000 \
  -v open-terminal:/home/user \
  -e OPEN_TERMINAL_API_KEY=your-secret-key \
  ghcr.io/open-webui/open-terminal:agents

docker exec -it open-terminal bash
```

Run `codex login --device-auth` and then `codex` from your project directory, or run
`claude` and follow its login prompts. Codex device login must be enabled in your
ChatGPT account or workspace. Each user signs in from their own terminal with their
own account. For API-key authentication, provide the provider key at runtime:
Codex supports `printenv OPENAI_API_KEY | codex login --with-api-key`, and Claude
Code accepts `ANTHROPIC_API_KEY`. See [Codex authentication](https://developers.openai.com/codex/auth)
and [Claude Code setup](https://code.claude.com/docs/en/setup#authenticate) for details.

Both CLIs are installed globally outside home directories, so home mounts do not
hide them. Persist `/home/user` for a single
user or `/home` in multi-user mode to retain configuration and file-based credentials.
Each published build installs the latest CLI releases. Claude Code background updates
are disabled; pull a newer image for updates.

To build locally, refresh the agent layer while reusing the base image cache:

```bash
docker build --target agents --no-cache-filter agents -t open-terminal:agents .
```

Available tags follow the existing conventions: `agents`, `latest-agents`,
`<version>-agents`, and `<major>.<minor>-agents`.

The container starts the Open Terminal server. Launch the agents from a terminal
or through the command API when needed.

## Bare metal

```bash
# Run without installing globally.
uvx open-terminal run --host 0.0.0.0 --port 8000 --api-key your-secret-key

# Or install with pip.
pip install open-terminal
open-terminal run --host 0.0.0.0 --port 8000 --api-key your-secret-key
```

Commands run with your user's permissions and access to your real files. Use Docker
for sandboxed execution. See [configuration](configuration.md) for environment and
TOML options that keep the API key out of command arguments.

## Custom packages

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

## Docker access

The image includes the Docker CLI, Compose, and Buildx. To let agents build images, run containers, etc., mount the host's Docker socket:

```bash
docker run -d --name open-terminal -p 8000:8000 \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v open-terminal:/home/user \
  ghcr.io/open-webui/open-terminal
```

> [!CAUTION]
> Mounting the Docker socket gives the container **full control over the host's Docker daemon**, which is effectively root access on the host machine. Anyone with access to the terminal can pull/run arbitrary containers (including `--privileged` ones), mount host directories, access host networking, and manage all containers on the host. Only do this in fully trusted environments.

For full control, fork the repo, edit the [Dockerfile](../Dockerfile), and build your own image:

```bash
docker build -t my-terminal .
docker run -d --name open-terminal -p 8000:8000 my-terminal
```

## Updating

```bash
docker pull ghcr.io/open-webui/open-terminal
docker rm -f open-terminal
```

Then re-run your original `docker run` command with the same volumes and settings. See the [Docker quickstart](../README.md#docker).

Use the same image tag you installed, such as `:agents` or `:slim`.

## Connecting to Open WebUI

Add the connection under **Open Terminal** in Integrations, not as a tool server.
This enables the terminal panel and file navigation sidebar for browsing directories,
uploading, downloading, and editing files alongside chat.

### Direct connection

Users can connect their own instance through **User Settings → Integrations → Open
Terminal**: add its URL and API key, then enable it. Requests go directly from the
browser, so this works for a terminal on the user's machine or a network only they
can reach. See the [connection guide](https://docs.openwebui.com/features/open-terminal/setup/connecting/)
for availability of personal integrations in your Open WebUI deployment.

### System-level connection

Admins configure connections through **Admin Settings → Integrations → Open
Terminal**: add the URL and API key, then enable the connection. Requests are proxied
through the Open WebUI backend, so the terminal only needs to be reachable from the
server. No additional services are required. Multiple terminals can be configured,
with access controlled per user or group.

## Built-in multi-user mode

> [!CAUTION]
> **Multi-user mode separates workspaces. It does not create a security boundary between users, and it is not designed for production multi-user deployments.**
> Everyone shares one container: one kernel, one process list, one network stack, one temp directory. Users can watch each other's running commands, reach ports other users open, exhaust CPU, memory and disk for everyone, and interfere with shared system state. Provisioning accounts requires elevated privileges inside the container, so a user who sets out to reach root inside that container will find a way. Run this mode only where every user on the instance is trusted at the same level, and give each user their own instance whenever that trust does not hold. See [SECURITY.md](../SECURITY.md) for how reports about this mode are handled.

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

For isolation between users, [Terminals](https://github.com/open-webui/terminals)
provisions and manages a separate container per user, with its own files, processes,
resource limits, and network boundary. Production use requires an
[Open WebUI Enterprise License](https://docs.openwebui.com/enterprise/); see the
[Terminals documentation](https://docs.openwebui.com/features/open-terminal/terminals/)
for deployment and licensing details.
