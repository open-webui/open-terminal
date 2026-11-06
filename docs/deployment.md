# Deployment

[← README](../README.md)

## Image variants

| Tag | Base | Runtime package installs | Multi-user mode | Egress firewall |
| --- | --- | --- | --- | --- |
| `latest` | Full Debian environment | Yes | Yes | Yes |
| `agents` | Full environment + Codex and Claude Code | Yes | Yes | Yes |
| `slim` | Debian | No | No | Yes |
| `alpine` | Alpine | No | No | Yes |
| `openshift` | Debian, restricted non-root policies | No | No | No |

The full images include Node.js, build tools, Chromium, a Linux desktop, LibreOffice,
LaTeX, Docker CLI, and data science libraries. The minimal images include git, curl,
and jq. Slim uses glibc; Alpine uses musl, which can affect binary compatibility.

To add tools to a minimal image, extend [Dockerfile.slim](../Dockerfile.slim),
[Dockerfile.alpine](../Dockerfile.alpine), or [Dockerfile.openshift](../Dockerfile.openshift).
OpenShift also excludes Docker socket access. Use a full image for Office-to-PDF
conversion, or install LibreOffice in a custom image.

## Coding agents

Use `ghcr.io/open-webui/open-terminal:agents` in the Docker quickstart, then open a shell:

```bash
docker exec -it open-terminal bash
```

Run `codex login --device-auth` and then `codex`, or run `claude` and follow its login
prompts. Each user signs in with their own account. See [Codex authentication](https://developers.openai.com/codex/auth)
and [Claude Code setup](https://code.claude.com/docs/en/setup#authenticate) for API keys and other login options.

Both CLIs are installed outside home directories. Persist `/home/user` for a single
user or `/home` in multi-user mode to retain configuration and file-based credentials.
Each published build installs the latest CLI releases. Claude Code background updates
are disabled; pull a newer image for updates.

To build locally, refresh the agent layer while reusing the base image cache:

```bash
docker build --target agents --no-cache-filter agents -t open-terminal:agents .
```

Available tags follow the existing conventions: `agents`, `latest-agents`,
`<version>-agents`, and `<major>.<minor>-agents`.

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
