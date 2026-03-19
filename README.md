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

| | `latest` | `slim` | `alpine` |
|---|---|---|---|
| **Best for** | AI agent sandboxes | Production / hardened | Edge / CI / minimal footprint |
| **Size** | ~4 GB | ~430 MB | ~230 MB |
| **Bundled tooling** | Node.js, gcc, ffmpeg, LaTeX, Docker CLI, data science libs | git, curl, jq | git, curl, jq |
| **Install packages at runtime** | ✔ (has `sudo`) | ✘ | ✘ |
| **Multi-user mode** | ✔ | ✘ | ✘ |
| **Egress firewall** | ✔ | ✔ | ✔ |

**`slim`** and **`alpine`** have the same feature set. Slim uses Debian (glibc) for broader binary compatibility; Alpine uses musl libc and is smaller, but some C-extension pip packages may need to compile from source.

```bash
docker run -d -p 8000:8000 -e OPEN_TERMINAL_API_KEY=secret ghcr.io/open-webui/open-terminal:slim
docker run -d -p 8000:8000 -e OPEN_TERMINAL_API_KEY=secret ghcr.io/open-webui/open-terminal:alpine
```

> [!NOTE]
> Slim and Alpine don't support `OPEN_TERMINAL_PACKAGES` / `OPEN_TERMINAL_PIP_PACKAGES`. To add packages, extend [Dockerfile.slim](Dockerfile.slim) or [Dockerfile.alpine](Dockerfile.alpine).

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

#### Customizing the Docker Environment

The easiest way to add extra packages is with environment variables — no fork needed:

```bash
docker run -d --name open-terminal -p 8000:8000 \
  -e OPEN_TERMINAL_PACKAGES="cowsay figlet" \
  -e OPEN_TERMINAL_PIP_PACKAGES="httpx polars" \
  ghcr.io/open-webui/open-terminal
```

| Variable | Description |
|---|---|
| `OPEN_TERMINAL_PACKAGES` | Space-separated list of **apt** packages to install at startup |
| `OPEN_TERMINAL_PIP_PACKAGES` | Space-separated list of **pip** packages to install at startup |

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
```

> [!TIP]
> Use the system config at `/etc/open-terminal/config.toml` to set site-wide defaults for host and port, and the user config for personal settings like the API key — this keeps the key out of `ps` / `htop`.

You can also point to a specific config file:

```bash
open-terminal run --config /path/to/my-config.toml
```

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

#### Built-in Multi-User Isolation

> [!CAUTION]
> Single-container multi-user mode is **not designed for production multi-user deployments**. All users share the same kernel, network, and system resources with no hard isolation boundaries between them. If one user's process misbehaves, it can affect every other user on the system. This mode exists as a lightweight convenience for small, trusted groups — not as a security model you should rely on.

For small, trusted deployments you can enable per-user isolation inside a single container:

```bash
docker run -d --name open-terminal -p 8000:8000 \
  -v open-terminal:/home \
  -e OPEN_TERMINAL_MULTI_USER=true \
  -e OPEN_TERMINAL_API_KEY=your-secret-key \
  ghcr.io/open-webui/open-terminal
```

Each user automatically gets a dedicated Linux account with its own home directory. Files, commands, and terminals are isolated between users via standard Unix permissions.

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
> **Need container-per-user isolation?** Check out **[Terminals](https://github.com/open-webui/terminals)**, which provisions and manages separate Open Terminal containers per user. For lighter deployments, built-in multi-user mode (`OPEN_TERMINAL_MULTI_USER=true`) provides per-user isolation inside a single container.

## License

MIT — see [LICENSE](LICENSE) for details.
