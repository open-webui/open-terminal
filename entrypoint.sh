#!/bin/bash
set -e

# -----------------------------------------------------------------------
# Docker-secrets support: resolve <VAR>_FILE → <VAR>
# Follows the convention used by the official PostgreSQL image.
# -----------------------------------------------------------------------
file_env() {
    local var="$1"
    local fileVar="${var}_FILE"
    local def="${2:-}"
    if [ "${!var:-}" ] && [ "${!fileVar:-}" ]; then
        printf >&2 'error: both %s and %s are set (but are exclusive)\n' "$var" "$fileVar"
        exit 1
    fi
    local val="$def"
    if [ "${!var:-}" ]; then
        val="${!var}"
    elif [ "${!fileVar:-}" ]; then
        val="$(< "${!fileVar}")"
    fi
    export "$var"="$val"
    unset "$fileVar"
}

file_env 'OPEN_TERMINAL_API_KEY'

# Fix permissions of the home directory if the user doesn't own it
# Find out who owns /home/user
OWNER=$(stat -c '%U' /home/user 2>/dev/null || echo "user")

if [ "$OWNER" != "user" ]; then
    # We use sudo because the container runs as 'user' but has passwordless sudo
    sudo chown -R user:user /home/user 2>/dev/null || true
fi

# Seed essential dotfiles when /home/user is bind-mounted empty
# (Docker does not populate bind-mounts with image contents)
if [ ! -f "$HOME/.bashrc" ]; then
    cp /etc/skel/.bashrc "$HOME/.bashrc" 2>/dev/null || true
fi
if [ ! -f "$HOME/.profile" ]; then
    cp /etc/skel/.profile "$HOME/.profile" 2>/dev/null || true
fi
mkdir -p "$HOME/.local/bin"

# Docker socket access — add user to the socket's group if mounted
if [ -S /var/run/docker.sock ]; then
    SOCK_GID=$(stat -c '%g' /var/run/docker.sock)
    if ! getent group "$SOCK_GID" > /dev/null 2>&1; then
        sudo groupadd -g "$SOCK_GID" docker-host
    fi
    SOCK_GROUP=$(getent group "$SOCK_GID" | cut -d: -f1)
    sudo usermod -aG "$SOCK_GROUP" user
fi

# Auto-install system packages
if [ -n "${OPEN_TERMINAL_PACKAGES:-}" ]; then
    echo "Installing system packages: $OPEN_TERMINAL_PACKAGES"
    sudo apt-get update -qq && sudo apt-get install -y --no-install-recommends $OPEN_TERMINAL_PACKAGES
    sudo rm -rf /var/lib/apt/lists/*
fi

# Auto-install Python packages
if [ -n "${OPEN_TERMINAL_PIP_PACKAGES:-}" ]; then
    echo "Installing pip packages: $OPEN_TERMINAL_PIP_PACKAGES"
    uv pip install --no-cache-dir --system $OPEN_TERMINAL_PIP_PACKAGES
fi

exec open-terminal "$@"
