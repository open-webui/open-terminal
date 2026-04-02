# Wrapper image — extends ghcr.io/open-webui/open-terminal with
# additional tools and environment configuration.
FROM ghcr.io/open-webui/open-terminal:latest

USER root

WORKDIR /additional-tools

# kubectl — official Kubernetes apt repository
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        apt-transport-https \
        gnupg \
    && KUBE_VER=v1.34 \
    && curl -fsSL "https://pkgs.k8s.io/core:/stable:/${KUBE_VER}/deb/Release.key" \
        | gpg --dearmor -o /etc/apt/keyrings/kubernetes-apt-keyring.gpg \
    && echo "deb [signed-by=/etc/apt/keyrings/kubernetes-apt-keyring.gpg] https://pkgs.k8s.io/core:/stable:/${KUBE_VER}/deb/ /" \
        | tee /etc/apt/sources.list.d/kubernetes.list \
    && apt-get update && apt-get install -y --no-install-recommends kubectl \
    && rm -rf /var/lib/apt/lists/*

# ACT — run GitHub Actions workflows locally
RUN curl --proto '=https' --tlsv1.2 -sSf https://raw.githubusercontent.com/nektos/act/master/install.sh | bash -s -- -b /usr/local/bin

# GitHub CLI
RUN curl -fsSLo /usr/share/keyrings/githubcli-archive-keyring.gpg \
        https://cli.github.com/packages/githubcli-archive-keyring.gpg \
    && printf 'Types: deb\nURIs: https://cli.github.com/packages\nSuites: stable\nComponents: main\nArchitectures: %s\nSigned-By: /usr/share/keyrings/githubcli-archive-keyring.gpg\n' \
        "$(dpkg --print-architecture)" \
        | tee /etc/apt/sources.list.d/github-cli.sources > /dev/null \
    && apt-get update && apt-get install -y --no-install-recommends gh \
    && rm -rf /var/lib/apt/lists/*

# ArgoCD CLI
RUN ARCH=$(dpkg --print-architecture) \
    && VERSION=$(curl -fsSL https://raw.githubusercontent.com/argoproj/argo-cd/stable/VERSION) \
    && curl -fsSL -o /tmp/argocd \
        "https://github.com/argoproj/argo-cd/releases/download/v${VERSION}/argocd-linux-${ARCH}" \
    && install -m 555 /tmp/argocd /usr/local/bin/argocd \
    && rm /tmp/argocd

# YQ — YAML processor
ARG YQ_VERSION=v4.52.5
RUN ARCH=$(dpkg --print-architecture) \
    && curl -sfL "https://github.com/mikefarah/yq/releases/download/${YQ_VERSION}/yq_linux_${ARCH}" -o yq \
    && curl -sfL "https://github.com/mikefarah/yq/releases/download/${YQ_VERSION}/checksums" -o checksums \
    && grep "^yq_linux_${ARCH} " checksums | awk '{print $19 "  yq"}' | sha256sum -c - \
    && rm checksums \
    && chmod +x yq \
    && cp yq /usr/local/bin/yq

RUN apt-get update && apt-get install -y --no-install-recommends \
        ripgrep \
        fd-find \
        bat \
        tmux \
        sqlite3 \
        httpie \
        tree \
        htop \
        pigz \
        unar \
        rsync \
        zip \
        unzip \
        diffutils \
        jq \
        redis-tools \
        postgresql-client \
        ansible \
        gnupg2 \
    && rm -rf /var/lib/apt/lists/*

# Terraform — HashiCorp apt repository
RUN curl -fsSL https://apt.releases.hashicorp.com/gpg \
        | gpg --dearmor -o /usr/share/keyrings/hashicorp-archive-keyring.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/hashicorp-archive-keyring.gpg] https://apt.releases.hashicorp.com $(. /etc/os-release && echo $VERSION_CODENAME) main" \
        | tee /etc/apt/sources.list.d/hashicorp.list \
    && apt-get update && apt-get install -y --no-install-recommends terraform \
    && rm -rf /var/lib/apt/lists/*

# Helm — official binary install
RUN ARCH=$(dpkg --print-architecture) \
    && VERSION=$(curl -fsSL https://api.github.com/repos/helm/helm/releases/latest | grep '"tag_name"' | cut -d'"' -f4) \
    && curl -fsSL "https://get.helm.sh/helm-${VERSION}-linux-${ARCH}.tar.gz" | tar -xz \
    && install -m 555 "linux-${ARCH}/helm" /usr/local/bin/helm \
    && rm -rf "linux-${ARCH}"

# Pre-configure kubectl for in-cluster serviceaccount
RUN mkdir -p /etc/skel/.kube && \
    cat > /etc/skel/.kube/config << 'EOF'
apiVersion: v1
kind: Config
clusters:
- cluster:
    server: https://kubernetes.default.svc
    certificate-authority: /var/run/secrets/kubernetes.io/serviceaccount/ca.crt
  name: in-cluster
contexts:
- context:
    cluster: in-cluster
    user: open-terminal
  name: default
current-context: default
users:
- name: open-terminal
  user:
    tokenFile: /var/run/secrets/kubernetes.io/serviceaccount/token
EOF

# Apply security patches on top of the upstream base image
RUN apt-get upgrade -y && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*

# Custom entrypoint and helper scripts
COPY entrypoint.sh /app/entrypoint.sh
COPY helpers/ /app/helpers/
RUN chmod +x /app/entrypoint.sh

USER user

ENTRYPOINT ["/usr/bin/tini", "--", "/app/entrypoint.sh"]
CMD ["run"]
