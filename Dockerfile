# Custom Open Terminal image with pre-installed packages.
# Based on the upstream full image which already includes:
# Python, Node.js, Docker CLI, gcc, ffmpeg, data science libs, etc.
#
# Pin to a specific tag for reproducible builds:
#   FROM ghcr.io/open-webui/open-terminal:0.11.34
# Use "latest" to always track the newest upstream release.

FROM ghcr.io/open-webui/open-terminal:latest

USER root

# ── Extra apt packages ──────────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
    # <add your apt packages here> \
    && rm -rf /var/lib/apt/lists/*

# ── Extra pip packages ──────────────────────────────────────────────
RUN pip install --no-cache-dir \
    # <add your pip packages here> \
    true

# ── Extra npm packages (global) ─────────────────────────────────────
# RUN npm install -g \
#     # <add your npm packages here> \
#     true

USER user
