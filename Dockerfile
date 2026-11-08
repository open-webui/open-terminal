# Pin to a specific patch version for reproducible builds.
# To pick up security patches, bump this version and rebuild.
FROM python:3.12.16 AS base

RUN apt-get update && apt-get install -y --no-install-recommends \
    # Core utilities
    coreutils findutils grep sed gawk diffutils patch \
    less file tree bc man-db \
    # Networking
    curl wget net-tools iputils-ping dnsutils netcat-openbsd socat telnet \
    openssh-client rsync \
    # Browser tabs
    chromium chromium-sandbox fonts-noto-cjk fonts-noto-color-emoji \
    # Editors
    vim nano \
    # Version control
    git gh \
    # Build tools
    build-essential cmake make \
    # Scripting & languages
    perl ruby-full lua5.4 \
    # Data processing
    jq xmlstarlet sqlite3 \
    # Media & documents
    ffmpeg pandoc poppler-utils imagemagick texlive-latex-base \
    libreoffice-nogui fonts-dejavu fonts-crosextra-caladea fonts-crosextra-carlito \
    # Compression
    zip unzip tar gzip bzip2 xz-utils zstd p7zip-full \
    # System
    procps htop lsof strace sysstat \
    sudo tmux screen tini iptables ipset dnsmasq \
    ca-certificates gnupg apt-transport-https \
    # Capabilities (needed for setcap on Python binary)
    libcap2-bin \
    && rm -rf /var/lib/apt/lists/*

# Node.js (LTS)
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# Docker CLI + Compose + Buildx (mount socket at runtime for access)
RUN curl -fsSL https://get.docker.com | sh

# Uncomment to apply security patches beyond what the base image provides.
# Not recommended for reproducible builds; prefer bumping the base image tag.
# RUN apt-get update && apt-get upgrade -y && rm -rf /var/lib/apt/lists/*


WORKDIR /app

RUN pip install --no-cache-dir \
    uv \
    numpy pandas scipy scikit-learn \
    matplotlib seaborn plotly \
    jupyter ipython \
    requests beautifulsoup4 lxml \
    sqlalchemy psycopg2-binary \
    pyyaml toml jsonlines \
    tqdm rich \
    openpyxl weasyprint \
    python-docx python-pptx pypdf csvkit

# Desktop sessions are started on demand and streamed through the terminal API.
RUN apt-get update && apt-get install -y --no-install-recommends \
    tigervnc-standalone-server xfce4 xfce4-terminal dbus-x11 \
    xauth xdotool xclip scrot libreoffice libreoffice-gtk3 pulseaudio pulseaudio-utils \
    && rm -rf /var/lib/apt/lists/*

# Xfce's default backdrop, converted to PNG for its image loader.
COPY assets/home.webp /tmp/wallpaper.webp
RUN convert /tmp/wallpaper.webp /usr/share/backgrounds/xfce/open-webui.png \
    && ln -sf open-webui.png /usr/share/backgrounds/xfce/xfce-x.svg \
    && rm /tmp/wallpaper.webp

# Debian's launcher resets CHROMIUM_FLAGS before reading this directory.
RUN printf '%s\n' \
    'case "${OPEN_TERMINAL_BROWSER_NO_SANDBOX:-false}" in' \
    '  [tT][rR][uU][eE]|1|[yY][eE][sS]) export CHROMIUM_FLAGS="$CHROMIUM_FLAGS --no-sandbox" ;;' \
    'esac' > /etc/chromium.d/open-terminal

COPY . .
# Create a capability-bearing Python copy for the server process only.
# The system python3 stays clean so user-spawned Python processes remain
# dumpable (readable via /proc/[pid]/fd/ for port detection).
RUN pip install --no-cache-dir . \
    && cp "$(readlink -f "$(which python3)")" /usr/local/bin/python3-ot \
    && setcap cap_setgid+ep /usr/local/bin/python3-ot \
    && sed -i "1s|.*|#!/usr/local/bin/python3-ot|" "$(which open-terminal)"

RUN useradd -m -s /bin/bash user && echo 'user ALL=(ALL) NOPASSWD:ALL' >> /etc/sudoers

USER user
ENV SHELL=/bin/bash
ENV OPEN_TERMINAL_BROWSER_NO_SANDBOX=true
ENV PATH="/home/user/.local/bin:${PATH}"
WORKDIR /home/user

EXPOSE 8000

COPY entrypoint.sh /app/entrypoint.sh

ENTRYPOINT ["/usr/bin/tini", "--", "/app/entrypoint.sh"]
CMD ["run"]

# Optional coding agents, installed outside home directories so mounts and
# multi-user sessions share the binaries while keeping their own credentials.
FROM base AS agents

USER root
RUN npm install --global --prefix /usr/local --include=optional \
    @openai/codex@latest \
    @anthropic-ai/claude-code@latest \
    && npm cache clean --force

# Agent updates are delivered by rebuilding the image.
ENV DISABLE_AUTOUPDATER=1
USER user
RUN codex --version && claude --version

# CUDA development tools and GPU-enabled PyTorch share the full environment.
FROM base AS cuda

USER root
RUN set -eux; \
    case "$(dpkg --print-architecture)" in \
        amd64) cuda_arch=x86_64 ;; \
        arm64) cuda_arch=sbsa ;; \
        *) echo "Unsupported CUDA architecture" >&2; exit 1 ;; \
    esac; \
    . /etc/os-release; \
    curl -fsSL "https://developer.download.nvidia.com/compute/cuda/repos/debian${VERSION_ID}/${cuda_arch}/cuda-keyring_1.1-1_all.deb" -o /tmp/cuda-keyring.deb; \
    dpkg -i /tmp/cuda-keyring.deb; \
    rm /tmp/cuda-keyring.deb; \
    apt-get update; \
    apt-get install -y --no-install-recommends cuda-toolkit-13-2=13.2.2-1; \
    rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir torch==2.14.1+cu132 \
    --index-url https://download.pytorch.org/whl/cu132

ENV CUDA_HOME=/usr/local/cuda
ENV PATH="/usr/local/cuda/bin:${PATH}"
ENV LD_LIBRARY_PATH=/usr/local/cuda/lib64:/usr/local/nvidia/lib:/usr/local/nvidia/lib64
ENV NVIDIA_VISIBLE_DEVICES=all
ENV NVIDIA_DRIVER_CAPABILITIES=compute,utility
ENV NVIDIA_REQUIRE_CUDA="cuda>=13.2"

USER user
RUN nvcc --version \
    && python -c "import torch; assert torch.version.cuda == '13.2', torch.version.cuda"

# Keep an ordinary `docker build .` equivalent to the existing full image.
FROM base AS default
