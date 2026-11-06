# Configuration

[← README](../README.md)

Open Terminal can be configured via a TOML config file, environment variables, and CLI flags. Settings are resolved in this order (highest priority wins):

1. **CLI flags** (`--host`, `--port`, `--api-key`, etc.)
2. **Environment variables** (`OPEN_TERMINAL_API_KEY`, etc.)
3. **User config** — `$XDG_CONFIG_HOME/open-terminal/config.toml` (defaults to `~/.config/open-terminal/config.toml`)
4. **System config** — `/etc/open-terminal/config.toml`
5. **Built-in defaults**

Create a config file at either location with any of these keys (all optional):

```toml
host = "0.0.0.0"
port = 8000
api_key = "sk-my-secret-key"
cors_allowed_origins = "*"
log_dir = "/var/log/open-terminal"
binary_mime_prefixes = "image,audio"
execute_timeout = 5  # seconds to wait for command output (unset by default)
file_browser_root = "home"
enable_browser = true
enable_desktop = true
```

Browser and desktop support are enabled by default. Set `OPEN_TERMINAL_ENABLE_BROWSER=false`
or `OPEN_TERMINAL_ENABLE_DESKTOP=false` to disable their HTTP and WebSocket endpoints
and advertise them as disabled in `/api/config`. Values `false`, `0`, and `no`
(case-insensitive) disable the feature. Restart the server after changing these settings.
Desktop support still requires its platform dependencies to be available.

> [!TIP]
> Use the system config at `/etc/open-terminal/config.toml` to set site-wide defaults for host and port, and the user config for personal settings like the API key — this keeps the key out of `ps` / `htop`.

You can also point to a specific config file:

```bash
open-terminal run --config /path/to/my-config.toml
```

## Use your running Chrome

Browser defaults to `managed`: Open Terminal launches a separate Chrome profile.
To open tabs in your running Chrome with its signed-in sessions, select `personal`:

```sh
export OPEN_TERMINAL_BROWSER_MODE=personal
```

In Chrome 144+, enable debugging at `chrome://inspect/#remote-debugging` and approve
the connection when Chrome asks. Open Terminal reads `DevToolsActivePort` from the
current OS user's normal Chrome data directory. To select another instance, set
one of these optional overrides:

```sh
export OPEN_TERMINAL_BROWSER_USER_DATA_DIR="/path/to/chrome-user-data"
# Or connect to an explicit HTTP debugging address or ws:// / wss:// endpoint:
export OPEN_TERMINAL_BROWSER_CDP_URL="http://127.0.0.1:9222"
```

An explicit CDP URL takes precedence over the data directory. If the selected
instance is unavailable, Open Terminal returns an error rather than launching or
choosing another browser. The equivalent TOML keys are `browser_mode`,
`browser_user_data_dir`, and `browser_cdp_url`. Restart Open Terminal after changes.

Use Chrome mode in WebUI as usual. Open Terminal creates a tab in the configured
browser using the same session API, tools, and viewer. No WebUI changes are needed.
Closing the session or stopping Open Terminal closes the tabs it created, leaving
your browser and other tabs open. Personal mode requires single-user operation.

Chromium's sandbox is enabled by default outside Docker. All published Docker image
variants set `OPEN_TERMINAL_BROWSER_NO_SANDBOX=true` so Browser can start under
Docker's default security restrictions when Chromium is installed. This applies to
both managed Browser tabs and Chromium launched from the Linux Desktop in the full
Docker image. Container isolation remains unchanged, but Chromium's own sandbox
no longer protects files and processes inside the container from a browser exploit.

To enable Chromium's sandbox in Docker, pass
`-e OPEN_TERMINAL_BROWSER_NO_SANDBOX=false` when creating the container; the runtime
must permit Chromium's sandbox operations. Outside Docker, set the variable to
`true` only if your deployment cannot support the sandbox. Values `true`, `1`, and
`yes` (case-insensitive) disable the sandbox. Restart or recreate Open Terminal
after changing this setting.

## File Browser Root

Open Terminal reports file-browser root metadata from `GET /files/cwd` so clients can hide parent navigation above a friendly starting point.

Set `OPEN_TERMINAL_FILE_BROWSER_ROOT` to:

| Value | Behavior |
|---|---|
| `home` | Default. Report the current user's home directory as `Home` |
| `/workspace` | Report an explicit path as the visual root |
| `{{home}}/project` | Report a path under the current user's home |
| `filesystem` | Opt out and report no visual root metadata |

This is a UI hint for clients. It does not restrict terminal commands or file APIs.

## External Workspace Storage

Open Terminal uses the filesystem it is mounted on. To store workspace files
externally, mount that storage into the container:

- Single-user: `/home/user`
- Multi-user: `/home`

This can be a Docker volume, Kubernetes persistent volume, NFS/Azure Files
mount, or FUSE mount such as blobfuse, s3fs, or rclone.

## Office Previews

`GET /files/view?path=...` always returns the original file by default. Clients
that want a rendered preview can add `preview=true`; when LibreOffice is
available the server can return DOCX and PPTX previews as PDF. When rendered
preview support is not available, the same endpoint falls back to returning the
original file bytes.
