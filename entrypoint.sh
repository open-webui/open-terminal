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
    if [ "${!var+set}" = "set" ] && [ "${!fileVar+set}" = "set" ]; then
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
    sudo apt-get update -qq && sudo DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends $OPEN_TERMINAL_PACKAGES
    sudo rm -rf /var/lib/apt/lists/*
fi

# Auto-install Python packages
if [ -n "${OPEN_TERMINAL_PIP_PACKAGES:-}" ]; then
    echo "Installing pip packages: $OPEN_TERMINAL_PIP_PACKAGES"
    if [ "${OPEN_TERMINAL_MULTI_USER:-false}" = "true" ]; then
        sudo pip install --no-cache-dir $OPEN_TERMINAL_PIP_PACKAGES
    else
        pip install --no-cache-dir $OPEN_TERMINAL_PIP_PACKAGES
    fi
fi

# Auto-install npm packages
if [ -n "${OPEN_TERMINAL_NPM_PACKAGES:-}" ]; then
    echo "Installing npm packages: $OPEN_TERMINAL_NPM_PACKAGES"
    if [ "${OPEN_TERMINAL_MULTI_USER:-false}" = "true" ]; then
        sudo npm install -g $OPEN_TERMINAL_NPM_PACKAGES
    else
        npm install -g $OPEN_TERMINAL_NPM_PACKAGES
    fi
fi

# -----------------------------------------------------------------------
# Network egress filtering via DNS whitelist + iptables + capability drop
#
#   OPEN_TERMINAL_ALLOWED_DOMAINS unset    → full access
#   OPEN_TERMINAL_ALLOWED_DOMAINS=""       → block ALL outbound
#   OPEN_TERMINAL_ALLOWED_DOMAINS="a,b"    → DNS whitelist (dnsmasq)
#
# Restricted mode runs a local dnsmasq that only resolves whitelisted
# domains.  iptables blocks external DNS so the container must use the
# local resolver.  CAP_NET_ADMIN is permanently dropped via capsh.
# -----------------------------------------------------------------------
if [ "${OPEN_TERMINAL_ALLOWED_DOMAINS+set}" = "set" ]; then
    if ! command -v iptables &>/dev/null; then
        echo "ERROR: iptables is required for egress filtering" >&2
        exit 1
    fi

    # Flush any prior OUTPUT rules
    sudo iptables -F OUTPUT 2>/dev/null || true

    # Capture upstream DNS before replacing resolv.conf, including in deny-all mode.
    UPSTREAM_DNS=$(grep -m1 '^nameserver' /etc/resolv.conf | awk '{print $2}')
    UPSTREAM_DNS="${UPSTREAM_DNS:-8.8.8.8}"

    if [ -z "$OPEN_TERMINAL_ALLOWED_DOMAINS" ]; then
        # ── Deny-all mode ──────────────────────────────────────────────
        echo "Egress: blocking ALL outbound traffic"
    else
        # ── Restricted mode (DNS whitelist + ipset) ────────────────────
        echo "Egress: DNS whitelist — $OPEN_TERMINAL_ALLOWED_DOMAINS"

        # Create ipset for dynamically resolved IPs
        sudo ipset create allowed hash:ip -exist

        # Generate dnsmasq config:
        #   - NXDOMAIN for everything by default
        #   - Forward allowed domains to upstream DNS
        #   - Auto-add resolved IPs to the 'allowed' ipset
        sudo mkdir -p /etc/dnsmasq.d
        {
            echo "user=dnsmasq"
            echo "no-resolv"
            echo "no-hosts"
            echo "listen-address=127.0.0.1"
            echo "port=53"
            echo "address=/#/"   # NXDOMAIN for everything by default

            IFS=',' read -ra DOMAINS <<< "$OPEN_TERMINAL_ALLOWED_DOMAINS"
            for domain in "${DOMAINS[@]}"; do
                domain=$(echo "$domain" | xargs)  # trim
                [ -z "$domain" ] && continue
                # Strip wildcard prefix — dnsmasq matches all subdomains natively
                domain="${domain#\*.}"
                echo "server=/${domain}/${UPSTREAM_DNS}"
                echo "ipset=/${domain}/allowed"
                echo "  ✓ ${domain} (+ subdomains)" >&2
            done
        } | sudo tee /etc/dnsmasq.d/egress.conf > /dev/null

        # Start dnsmasq as a background daemon
        sudo dnsmasq --conf-file=/etc/dnsmasq.d/egress.conf
        echo "dnsmasq started (upstream: ${UPSTREAM_DNS})"

        # Point the container at our local resolver
        echo "nameserver 127.0.0.1" | sudo tee /etc/resolv.conf > /dev/null

        # Match the original destination: Docker rewrites its embedded DNS port.
        # Check DNS before allowing loopback or established connections.
        for protocol in udp tcp; do
            sudo iptables -A OUTPUT -p "$protocol" -m conntrack --ctdir ORIGINAL \
                --ctorigdst "$UPSTREAM_DNS" --ctorigdstport 53 \
                -m owner --uid-owner dnsmasq -j ACCEPT
            sudo iptables -A OUTPUT -p "$protocol" -m conntrack --ctdir ORIGINAL \
                --ctorigdst 127.0.0.1 --ctorigdstport 53 -j ACCEPT
        done
    fi

    # Also block Docker's loopback DNS in deny-all mode.
    for protocol in udp tcp; do
        sudo iptables -A OUTPUT -p "$protocol" -m conntrack --ctdir ORIGINAL \
            --ctorigdstport 53 -j DROP
    done
    # Prevent direct access to Docker DNS's translated high ports as well.
    sudo iptables -A OUTPUT -o lo -d "$UPSTREAM_DNS" -j DROP
    sudo iptables -A OUTPUT -o lo -j ACCEPT
    sudo iptables -A OUTPUT -m state --state ESTABLISHED,RELATED -j ACCEPT
    if [ -n "$OPEN_TERMINAL_ALLOWED_DOMAINS" ]; then
        sudo iptables -A OUTPUT -m set --match-set allowed dst -j ACCEPT
    fi
    sudo iptables -A OUTPUT -j DROP

    echo "Egress firewall active — dropping CAP_NET_ADMIN permanently"
    exec sudo -E PATH="$PATH" capsh --drop=cap_net_admin --user=user -- -c 'exec open-terminal "$@"' -- "$@"
fi

exec open-terminal "$@"
