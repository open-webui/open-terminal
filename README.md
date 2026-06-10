# Open Terminal — YKPS Custom Image

A custom Docker image based on [open-webui/open-terminal](https://github.com/open-webui/open-terminal) with pre-installed packages for YKPS.

## Quick Start

```bash
# Build
docker build -t open-terminal-ykps .

# Run
docker run -d --name open-terminal -p 8000:8000 \
  -e OPEN_TERMINAL_API_KEY=your-secret-key \
  open-terminal-ykps
```

Or with Docker Compose:

```bash
docker compose up -d
```

## Adding Packages

Edit `Dockerfile` and add packages to the appropriate section:

```dockerfile
# apt packages
RUN apt-get update && apt-get install -y --no-install-recommends \
    vim nano jq \
    && rm -rf /var/lib/apt/lists/*

# pip packages
RUN pip install --no-cache-dir \
    httpx polars

# npm packages (uncomment to enable)
# RUN npm install -g \
#     typescript tsx
```

Rebuild after changes: `docker compose build`

## Updating the Base Image

To pull the latest upstream image and rebuild:

```bash
docker compose build --no-cache
```

Or pin to a specific version in `Dockerfile`:

```dockerfile
FROM ghcr.io/open-webui/open-terminal:0.11.34
```

## License

MIT
