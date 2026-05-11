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

## Safety Notes

- `otx edit` shows a diff by default and asks before applying.
- keep `targets.json` private (`chmod 600`) because it contains API keys.
- prefer target-specific `cwd` values to avoid accidental writes outside project roots.
