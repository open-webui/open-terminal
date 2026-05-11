# OTX Router Setup (One Session, Two Machines)

`otx` is a target router for OpenTerminal. It lets you run commands and edit files on either host from one CLI without hopping terminals.

## Purpose

- one control plane from Z490
- explicit target dispatch (`z490`, `nuc`)
- command execution + file read/write/edit through OpenTerminal APIs

## Prereqs

1. OpenTerminal running on both hosts (systemd user service preferred).
2. API key available for each host.
3. Network path from this machine to remote OpenTerminal URL.

## Config

Create:

`~/.config/open-terminal/targets.json`

Example:

```json
{
  "default": "z490",
  "targets": {
    "z490": {
      "base_url": "http://127.0.0.1:8010",
      "api_key": "REPLACE_LOCAL_KEY",
      "cwd": "/home/trotsky/Projects",
      "session_id": "otx-z490"
    },
    "nuc": {
      "base_url": "http://NUC_IP_OR_HOSTNAME:8010",
      "api_key": "REPLACE_NUC_KEY",
      "cwd": "/home/<user>/Projects",
      "session_id": "otx-nuc"
    }
  }
}
```

## Recommended NUC Connectivity (Loopback + SSH Tunnel)

Keep NUC OpenTerminal bound to `127.0.0.1:8010` and expose it on Z490 through a user tunnel.

Example user service on Z490:

`~/.config/systemd/user/otx-nuc-tunnel.service`

```ini
[Unit]
Description=OTX SSH tunnel to NUC OpenTerminal
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/bin/ssh -NT -i %h/.ssh/id_ed25519_z490_to_trotsky -o ExitOnForwardFailure=yes -o ServerAliveInterval=20 -o ServerAliveCountMax=3 -L 18010:127.0.0.1:8010 trotsky@100.68.242.114
Restart=always
RestartSec=2

[Install]
WantedBy=default.target
```

Enable it:

```bash
systemctl --user daemon-reload
systemctl --user enable --now otx-nuc-tunnel.service
systemctl --user status otx-nuc-tunnel.service
```

Then configure NUC target as:

- `base_url`: `http://127.0.0.1:18010`

## Commands

List targets:

```bash
scripts/otx --config ~/.config/open-terminal/targets.json targets
```

Health check remote:

```bash
scripts/otx --target nuc health
```

Run command:

```bash
scripts/otx --target nuc run -- "git status"
```

Run command in login shell (recommended when target relies on shell profile setup):

```bash
scripts/otx --target nuc run --login-shell -- "node -v"
```

Run Node/NPM command on NVM-based target without editing system PATH:

```bash
scripts/otx --target nuc run --login-shell --node-bin /home/trotsky/.nvm/versions/node/v22.22.0/bin -- "npm -v"
```

Source a remote env file before execution (keeps secrets on the target host):

```bash
scripts/otx --target nuc run --source-env /home/trotsky/.local/share/ba-midnight/probe-wallet-seed.env --node-bin /home/trotsky/.nvm/versions/node/v22.22.0/bin -- "cd /home/trotsky/Projects/ba-midnight/midnight-gateway && npm run -s probe:wallet-sdk-dust-sync-diagnostics"
```

Read file:

```bash
scripts/otx --target nuc read /home/<user>/Projects/repo/README.md
```

Edit file through local `$EDITOR` and write back:

```bash
scripts/otx --target nuc edit /home/<user>/Projects/repo/path/to/file.py
```

Tail logs from target:

```bash
scripts/otx --target nuc logs --unit open-terminal --lines 200
```

## One-Command Cross-Host Benchmark

Run the policy-path benchmark on both targets and refresh local comparison artifacts:

```bash
scripts/otx-benchmark-crosshost
```

Optional flags:

```bash
scripts/otx-benchmark-crosshost --retries 3 --wait 300 --local z490 --remote nuc
```

## Safety Notes

- `otx edit` shows a diff by default and asks before applying.
- keep `targets.json` private (`chmod 600`) because it contains API keys.
- prefer target-specific `cwd` values to avoid accidental writes outside project roots.
