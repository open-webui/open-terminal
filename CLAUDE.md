# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Custom Docker image based on [open-webui/open-terminal](https://github.com/open-webui/open-terminal) with pre-installed packages for YKPS. This repo only contains the customization layer — the upstream source code is not included. The image builds on `ghcr.io/open-webui/open-terminal:latest` (full variant) which already bundles Python, Node.js, Docker CLI, gcc, ffmpeg, data science libs, etc.

## Commands

```bash
# Build image
docker compose build

# Run (API key required)
docker compose up -d

# Rebuild after adding packages
docker compose build --no-cache

# Build without compose
docker build -t open-terminal-ykps .
```

## Repository Structure

- `Dockerfile` — Customization layer on top of upstream image. Add apt/pip/npm packages here.
- `docker-compose.yml` — Build and run config.
- `.github/workflows/docker.yml` — CI: builds multi-arch image and pushes to ghcr.io.

## Key Conventions

- **Base image**: Always `FROM ghcr.io/open-webui/open-terminal:latest`. Pin to a specific tag for reproducible builds.
- **User switching**: Install packages as `USER root`, then switch back to `USER user`.
- **Upstream env vars**: All prefixed with `OPEN_TERMINAL_` (e.g., `OPEN_TERMINAL_API_KEY`). See [upstream README](https://github.com/open-webui/open-terminal) for full list.
- **CI**: Single-variant build (no slim/alpine). Multi-arch (amd64 + arm64).
