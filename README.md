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

Open Terminal integrates directly with [Open WebUI](https://github.com/open-webui/open-webui), giving your AI assistants the ability to run commands, manage files, and interact with the terminal — right from the chat interface.

Once connected, you get:

- 🤖 **AI tool access** — your models can execute commands, read/write files, and search your codebase as part of a conversation
- 📁 **Built-in file browser** — browse, upload, download, and manage files on the terminal instance directly from the Open WebUI sidebar

### Setup

1. **Start an Open Terminal instance** (see [Getting Started](#getting-started) above)
2. In Open WebUI, go to **User Settings → Integrations**
3. Under **Open Terminal**, click the **+** button to add a connection
4. Enter the **URL** (e.g. `http://localhost:8000`) and your **API key**
5. **Enable** the connection — only one terminal can be active at a time

That's it — your AI assistants now have access to the terminal, and you can browse files from the sidebar.

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
