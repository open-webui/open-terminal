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

## Desktop

Linux defaults to a **separate virtual desktop**. It prefers installed GNOME,
otherwise uses XFCE. Docker `latest` includes XFCE. To choose explicitly:

```sh
open-terminal run --desktop-environment gnome
# Or: open-terminal run --desktop-environment xfce
```

On **Ubuntu 26.04 and other Wayland-only GNOME installations**, install:

```sh
sudo apt install gnome-shell dbus-daemon pipewire wireplumber gstreamer1.0-tools gstreamer1.0-pipewire gstreamer1.0-plugins-base libgl1-mesa-dri x11vnc
open-terminal run --desktop-mode virtual --desktop-environment gnome
```

Open Terminal starts a headless GNOME Shell with its own virtual monitor, D-Bus
session, and PipeWire services. It works over SSH without a graphical login or
an on-screen permission prompt. It uses noVNC, like XFCE. Existing GNOME Wayland
installations need `sudo apt install x11vnc` after upgrading. Commands launched through the API inherit this virtual
desktop's display and bus so Wayland apps open there. The logged-in host desktop
is not used. This backend currently requires single-user mode and does not stream
audio; multi-user deployments keep the XFCE fallback when GNOME is Wayland-only.
It is tested with Ubuntu 26.04/GNOME 50.

GNOME still runs on Wayland: PipeWire supplies frames to
[`x11vnc`'s raw-framebuffer mode](https://github.com/LibVNC/x11vnc/blob/master/x11vnc.1),
and input goes back through the owned Mutter session. LibVNCServer handles the
VNC protocol. Each authenticated viewer gets a private socket pair; no VNC TCP
port is opened. Browser paste requires an updated Open WebUI and types text into
the desktop; clipboard synchronization and audio are not provided by this backend.

Upstream [GNOME Remote Desktop](https://github.com/GNOME/gnome-remote-desktop/blob/50.2/README.md)
also supports headless VNC, but Ubuntu 26.04 builds it without VNC support.
[TigerVNC's `w0vncserver`](https://tigervnc.org/doc/w0vncserver.html) shares an
existing Wayland session through the remote-desktop portal; it does not create
the separate desktop. [`wayvnc`](https://github.com/any1/wayvnc) does not support GNOME.

For **XFCE or older GNOME installations with an X11 session**, install the VNC tools:

```sh
sudo apt install tigervnc-standalone-server dbus-x11 x11-utils xauth xdotool scrot xclip
```

You also need a compatible desktop session: `gnome-session` with X11 support,
or `xfce4 xfce4-terminal`. Debian 13 packages GNOME's X11 session separately as
`gnome-session-xsession`; install it as well when using GNOME there.
Keep your existing display manager if the installer
asks; Open Terminal starts its own session and does not need LightDM or GDM.
When GNOME has no X11 session, Open Terminal selects the Wayland backend instead
of TigerVNC. Missing dependencies produce an error for the selected backend;
they never cause a connection to the host desktop. Restart after installation.

To share **your actual logged-in desktop and its open windows**, opt in explicitly:

```sh
open-terminal run --desktop-mode host
```

Host mode requires single-user operation as the desktop's OS user. On Linux
X11, run from a graphical terminal so `DISPLAY` and `XAUTHORITY` identify that
session; install `xclip` for clipboard input. On Wayland/GNOME, install:

```sh
sudo apt install xdg-desktop-portal xdg-desktop-portal-gnome gstreamer1.0-tools gstreamer1.0-pipewire gstreamer1.0-plugins-base
```

Approve monitor, keyboard, and pointer access once in the dialog on your Ubuntu
desktop. Open Terminal requests persistent authorization and remembers it when
the portal grants it, so later connections and server restarts can reuse that
approval, including over SSH. Older portals, revoked permissions, or a changed
monitor may require approval again. Sharing can be stopped from the desktop's
sharing indicator. A GNOME VNC/RDP server is not needed.
From SSH, run as the same OS user: Open Terminal can reach that user's existing
desktop portal even when `XDG_SESSION_TYPE=tty`. Someone must still be logged in
to the graphical desktop and approve the initial dialog there. Host mode never starts
a new desktop as a fallback. Wayland text input sends Unicode key events; audio
sharing is currently available only in VNC virtual sessions.

SSH host mode uses the same OS user's desktop bus at `/run/user/<uid>/bus`,
even if the SSH shell has a private D-Bus address. Startup errors identify the
failed portal request or PipeWire capture stage and include GStreamer errors.
A permission timeout means you should check the sharing dialog on the unlocked
Ubuntu desktop; it will not appear in the SSH terminal.

The authorization token is stored with owner-only permissions in
`$XDG_STATE_HOME/open-terminal/desktop-restore-token` (default:
`~/.local/state/open-terminal/desktop-restore-token`) and renewed after each
successful portal connection. To forget saved approval, stop Open Terminal and
delete that file; the next connection will ask again. If the portal does not
grant persistent access or the token cannot be saved, the server logs a warning.

The equivalent environment variables are `OPEN_TERMINAL_DESKTOP_MODE`
(`virtual` or `host`) and `OPEN_TERMINAL_DESKTOP_ENVIRONMENT`
(`auto`, `gnome`, or `xfce`); TOML keys are `desktop_mode` and
`desktop_environment`. CLI flags take precedence. Restart after changing modes.
macOS and Windows keep their existing host-desktop behavior by default.

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
managed Browser tabs, and in the full Docker image also to Chromium launched from
the Linux Desktop or from commands. Container isolation remains unchanged, but
Chromium's own sandbox no longer protects files and processes inside the container
from a browser exploit.

To enable Chromium's sandbox in Docker, pass
`-e OPEN_TERMINAL_BROWSER_NO_SANDBOX=false` when creating the container; the runtime
must permit Chromium's sandbox operations. Outside Docker, set the variable to
`true` only if your deployment cannot support the sandbox. Values `true`, `1`, and
`yes` (case-insensitive) disable the sandbox. Restart or recreate Open Terminal
after changing this setting.

## Memory management

File and command-output responses stream from disk, uploads are copied in chunks,
and generated ZIP/PDF files are removed after sending or cancellation. Log rotation
retains the newest half without loading the entire log into memory. Search listings
and ranking use temporary disk storage while preserving full matches.

Process, terminal, notebook, and document-worker cleanup releases readers, file
handles, and child processes on completion, cancellation, or server shutdown.
Command-log retention and the notebook idle timeout still apply.

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

## Start Directory

Set `OPEN_TERMINAL_START_DIR` to choose where every session starts: new shells, commands
run without a `cwd`, and the working directory `GET /files/cwd` reports before a client
changes it. Directories a client sets for a session still take precedence.

| Value | Behavior |
|---|---|
| unset or `~` | Default. Start in the home directory |
| `~/project` or `project` | Start in a folder inside home |
| `/home/user/project` | An absolute path, used only if it is inside home |

Paths outside home are ignored, because a managed terminal keeps only its home folder
between restarts. The folder is created at startup if it doesn't exist.

## Setup Script

Set `OPEN_TERMINAL_SETUP_SCRIPT` to a shell script that runs each time the server starts,
before it accepts requests. It runs in the start directory as the terminal user, with
`bash -l -e` (or `sh -e` when bash isn't installed), so it stops at the first failing
command. Open Terminal's own `OPEN_TERMINAL_*` settings, including the API key, are not
passed to it.

```bash
test -d app || git clone https://github.com/acme/app app
cd app && npm install
```

Only the home folder survives a restart, so write the script to be safe to run again.
Output goes to `~/.open-terminal/setup.log`. A failing script does not stop the server:
`GET /system/info` reports `setup.status` as `failed` with the exit code and the end of
the log. Multi-user servers skip the setup script. Because the server isn't ready until
the script finishes, keep it within your orchestrator's start timeout.

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
