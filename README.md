# ⚡ Open Terminal

A lightweight, self-hosted terminal that gives AI agents and automation tools a dedicated environment to run commands, manage files, and execute code — all through a simple API.

## Why Open Terminal?

AI assistants are great at writing code, but they need somewhere to *run* it. Open Terminal is that place — a remote shell with file management, search, and more, accessible over a simple REST API.

You can run it two ways:

- **Docker (sandboxed)** — runs in an isolated container with a full toolkit pre-installed: Python, Node.js, git, build tools, data science libraries, ffmpeg, and more. Great for giving AI agents a safe playground without touching your host system.
- **Bare metal** — install it directly with `pip` and run it anywhere Python runs. Commands execute on your machine, so you get full access to your local environment.

## Getting Started

### Docker (recommended)

```bash
docker run -d --name open-terminal --restart unless-stopped -p 8000:8000 -v open-terminal:/home/user -e OPEN_TERMINAL_API_KEY=your-secret-key ghcr.io/open-webui/open-terminal
```

That's it — you're up and running at `http://localhost:8000`.

> [!TIP]
> If you don't set an API key, one is generated automatically. Grab it with `docker logs open-terminal`.

#### Customizing the Docker Environment

The default image ships with a broad set of tools, but you can tailor it to your needs. Fork the repo, edit the [Dockerfile](Dockerfile) to add or remove system packages, Python libraries, or language runtimes, then build your own image:

```bash
docker build -t my-terminal .
docker run -d --name open-terminal -p 8000:8000 my-terminal
```

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


## Using with Open WebUI

Open Terminal integrates with [Open WebUI](https://github.com/open-webui/open-webui), giving your AI assistants the ability to run commands, manage files, and interact with a terminal right from the AI interface. There are two ways to connect:

### Direct Connection

Users can connect their own Open Terminal instance from their user settings. This is useful when the terminal is running on their local machine or a network only they can reach, since requests go directly from the **browser**.

1. Go to **User Settings → Integrations → Open Terminal**
2. Add the terminal **URL** and **API key**
3. Enable the connection

### System-Level Connection

Admins can configure Open Terminal connections for their users from the admin panel. Multiple terminals can be set up with access controlled at the user or group level. Requests are proxied through the Open WebUI **backend**, so the terminal only needs to be reachable from the server.

1. Go to **Admin Settings → Integrations → Open Terminal**
2. Add the terminal **URL** and **API key**
3. Enable the connection

For isolated, per-user terminal containers, see **[Terminals](https://github.com/open-webui/terminals)**, which requires an enterprise license for production use.

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
> **Need multi-tenant?** Check out **[Terminals](https://github.com/open-webui/terminals)**, which provisions and manages isolated Open Terminal containers per user with a single authenticated API entry point.

## License

MIT — see [LICENSE](LICENSE) for details.
