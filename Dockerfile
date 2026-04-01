# Pin to a specific patch version for reproducible builds.
# To pick up security patches, bump this version and rebuild.
FROM python:3.12.13

RUN apt-get update && apt-get install -y --no-install-recommends \
    # Core utilities
    coreutils findutils grep sed gawk diffutils patch \
    less file tree bc man-db \
    # Networking
    curl wget net-tools iputils-ping dnsutils netcat-openbsd socat telnet \
    openssh-client rsync \
    # Editors
    vim nano \
    # Version control
    git \
    # Build tools
    build-essential cmake make \
    # Scripting & languages
    perl ruby-full lua5.4 \
    # Data processing
    jq xmlstarlet sqlite3 \
    # Media & documents
    ffmpeg pandoc imagemagick texlive-latex-base \
    # Compression
    zip unzip tar gzip bzip2 xz-utils zstd p7zip-full \
    # System
    procps htop lsof strace sysstat \
    sudo tmux screen tini iptables ipset dnsmasq \
    ca-certificates gnupg apt-transport-https \
    # Capabilities (needed for setcap on Python binary)
    libcap2-bin \
    # Virtual desktop ("Computer Use")
    xvfb x11vnc novnc openbox xdotool scrot xauth \
    xterm x11-xserver-utils \
    fonts-liberation fonts-noto-color-emoji \
    dmz-cursor-theme \
    && rm -rf /var/lib/apt/lists/*

# Node.js (LTS)
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# Chromium (for headful browser automation via the virtual desktop)
RUN apt-get update && apt-get install -y --no-install-recommends chromium \
    && rm -rf /var/lib/apt/lists/*

# Docker CLI + Compose + Buildx (mount socket at runtime for access)
RUN curl -fsSL https://get.docker.com | sh

# Uncomment to apply security patches beyond what the base image provides.
# Not recommended for reproducible builds; prefer bumping the base image tag.
# RUN apt-get update && apt-get upgrade -y && rm -rf /var/lib/apt/lists/*


WORKDIR /app

RUN pip install --no-cache-dir \
    numpy pandas scipy scikit-learn \
    matplotlib seaborn plotly \
    jupyter ipython \
    requests beautifulsoup4 lxml \
    sqlalchemy psycopg2-binary \
    pyyaml toml jsonlines \
    tqdm rich \
    openpyxl weasyprint \
    python-docx python-pptx pypdf csvkit

COPY . .
# Create a capability-bearing Python copy for the server process only.
# The system python3 stays clean so user-spawned Python processes remain
# dumpable (readable via /proc/[pid]/fd/ for port detection).
RUN pip install --no-cache-dir . \
    && cp "$(readlink -f "$(which python3)")" /usr/local/bin/python3-ot \
    && setcap cap_setgid+ep /usr/local/bin/python3-ot \
    && sed -i "1s|.*|#!/usr/local/bin/python3-ot|" "$(which open-terminal)"

RUN useradd -m -s /bin/bash user && echo 'user ALL=(ALL) NOPASSWD:ALL' >> /etc/sudoers

# Chromium needs a writable /dev/shm for shared memory.  When running
# without --no-sandbox (the default inside Docker) we need at least 64 MB.
# Some container runtimes mount /dev/shm as 64 MB which is too small for
# Chromium; the kernel will silently OOM the renderer.  We work around
# this by creating a small tmpfs in the user's home directory.
RUN echo "kernel.shmmax = 268435456" >> /etc/sysctl.conf || true

RUN printf '#!/bin/sh\nexport CHROMIUM_FLAGS="$CHROMIUM_FLAGS --no-sandbox --disable-gpu --disable-software-rasterizer"\n' \
    > /etc/chromium.d/00-container

USER user
ENV SHELL=/bin/bash
ENV PATH="/home/user/.local/bin:${PATH}"
WORKDIR /home/user

EXPOSE 8000 6080

COPY entrypoint.sh /app/entrypoint.sh

ENTRYPOINT ["/usr/bin/tini", "--", "/app/entrypoint.sh"]
CMD ["run"]
