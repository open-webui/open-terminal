# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [0.11.32] - 2026-03-30

### Changed

- 🗂️ **Session-aware relative path resolution** — all LLM-exposed file endpoints (`list_files`, `read_file`, `write_file`, `display_file`, `replace_file_content`, `grep_search`, `glob_search`) now read the `X-Session-Id` header and resolve relative paths (e.g. `.`) against the session's working directory instead of always defaulting to `fs.home`. Added an optional `cwd` parameter to `UserFS.resolve_path()` to support this. Absolute paths are unaffected.

## [0.11.31] - 2026-03-30

### Changed

- 🗂️ **Per-session working directory** — replaced the process-global `os.chdir()` with an in-memory, session-aware dictionary keyed by `X-Session-Id` header. Multiple concurrent chat sessions now maintain independent working directories. `GET/POST /files/cwd`, `POST /execute`, and `POST /api/terminals` all read the header to resolve the correct cwd. Sessions without a header fall back to `fs.home`. Entries expire after 7 days of inactivity (sliding TTL), configurable via `OPEN_TERMINAL_SESSION_CWD_TTL` (or `session_cwd_ttl` in config.toml).

## [0.11.30] - 2026-03-25

### Changed

- 🔑 **Stronger auto-generated API keys** — increased from 192-bit (32 chars) to 384-bit (64 chars) entropy, making brute-force attacks computationally infeasible.
- 🔒 **API key required to start** — the server now refuses to start without an API key configured. The CLI auto-generates one as before, but running via `uvicorn` directly without setting `OPEN_TERMINAL_API_KEY` is no longer allowed.
- 🛡️ **Constant-time API key comparison** — both HTTP and WebSocket authentication now use `hmac.compare_digest()` instead of `!=`, preventing timing-based key extraction.
- ⚠️ **CORS default warning** — a prominent yellow box is printed at startup when `--cors-allowed-origins` is left at the default `*`, warning operators to restrict it for production.

## [0.11.29] - 2026-03-24

### Fixed

- 🔒 **File save fails in multi-user mode** — `_chown()` transferred file ownership to the provisioned user but didn't set group-write permission, leaving files at `644`. The server process (a group member) could create new files but couldn't overwrite them on subsequent saves, returning `PermissionError`. Now sets `chmod g+w` after `chown`, matching the `2770` treatment already applied to directories. ([#93](https://github.com/open-webui/open-terminal/issues/93))

## [0.11.28] - 2026-03-24

### Added

- 📦 **Compressed directory download** (`POST /files/archive`) — bundle one or more files and directories into a ZIP archive for download. Cross-platform compatible (Windows, macOS, Linux). Multi-user access control enforced.

## [0.11.27] - 2026-03-22

### Added

- 🧠 **System prompt endpoint** (`GET /system`) — returns a structured system prompt for LLM integration, grounding the model in the environment (OS, hostname, user, shell, Python version) with directives for tool usage. Gated by `OPEN_TERMINAL_ENABLE_SYSTEM_PROMPT` (default `true`); advertised via `features.system` in `GET /api/config` so consumers can check support before fetching.
- ⚙️ **`OPEN_TERMINAL_ENABLE_SYSTEM_PROMPT`** — environment variable (or `enable_system_prompt` in config.toml) to enable/disable the `/system` endpoint and feature flag. Defaults to `true`.
- ⚙️ **`OPEN_TERMINAL_SYSTEM_PROMPT`** — environment variable (or `system_prompt` in config.toml) to fully override the generated system prompt with custom content.

## [0.11.26] - 2026-03-20

### Changed

- ♻️ **`utils/documents.py`** — extracted all document text extraction logic from `read_file` into a dedicated module with an `EXTRACTORS` registry. Adding a new format now requires only a single function and a one-line registry entry. No behaviour changes.

## [0.11.25] - 2026-03-20

### Added

- 📄 **Extended document text extraction** in `read_file` — `.rtf`, `.xls`, `.odt`, `.ods`, `.odp`, `.epub`, and `.eml` files are now automatically converted to text and returned in the standard JSON format. RTF strips formatting to plain text, legacy Excel (`.xls`) renders sheets as tab-separated values, OpenDocument formats (`.odt`/`.ods`/`.odp`) parse the underlying XML, EPUB extracts body text in reading order, and `.eml` extracts headers and body (with HTML tag stripping). All support `start_line`/`end_line` range selection.

## [0.11.24] - 2026-03-20

### Added

- 📄 **Office document text extraction** in `read_file` — `.docx`, `.xlsx`, and `.pptx` files are now automatically converted to text and returned in the standard JSON format, making them readable by LLMs. Word documents extract paragraphs and table contents, Excel spreadsheets render all sheets as tab-separated values, and PowerPoint presentations extract text from all slides. Supports `start_line`/`end_line` range selection.

## [0.11.23] - 2026-03-19

### Fixed

- 🔐 **`_FILE` mutual exclusivity bypassed by empty env vars** — setting e.g. `OPEN_TERMINAL_API_KEY=""` alongside `OPEN_TERMINAL_API_KEY_FILE` silently skipped the conflict check because empty strings are falsy. The Python helper (`_resolve_file_env`), `entrypoint.sh`, and `entrypoint-slim.sh` now test whether the variable is *set* (not merely non-empty), so any explicit assignment — including `=""` — correctly triggers the mutual-exclusivity error.

## [0.11.22] - 2026-03-19

### Fixed

- 🐛 **`/ports` returns 500 in multi-user mode on restricted runtimes** — the endpoint triggered full user provisioning (`useradd`) just to filter ports by UID. On container runtimes that reject `useradd` (e.g. Azure Container Apps), this crashed with an unhandled exception. Now returns an empty port list when provisioning fails — an unprovisioned user has no ports to show. ([#80](https://github.com/open-webui/open-terminal/issues/80))
- 🐳 **Docker-in-Docker broken in multi-user mode** — mounting the Docker socket (`-v /var/run/docker.sock:/var/run/docker.sock`) with `OPEN_TERMINAL_MULTI_USER=true` failed because only the default `user` account was added to the socket's group. Dynamically provisioned users now automatically inherit Docker socket group membership when the socket is mounted. ([#83](https://github.com/open-webui/open-terminal/issues/83))

## [0.11.21] - 2026-03-19

### Fixed

- 🔍 **Port detection broken since v0.11.2** — `setcap cap_setgid+ep` on the system Python binary (added for multi-user `os.setgroups()`) made all Python processes non-dumpable, blocking `/proc/[pid]/fd/` access needed to resolve socket inodes to PIDs. Ports from user-spawned Python servers were silently filtered out. Fixed by copying the Python binary and granting `cap_setgid` only to the copy (`python3-ot`), used exclusively by the open-terminal server. The system `python3` stays capability-free so user processes remain dumpable. Slim and Alpine images had `setcap` removed entirely since they don't support multi-user mode. ([#85](https://github.com/open-webui/open-terminal/issues/85), [#63](https://github.com/open-webui/open-terminal/issues/63))
- 📖 **README** — Image Variants table incorrectly listed multi-user mode as supported on slim and alpine images. Multi-user mode requires `sudo`, which only the full image includes.

## [0.11.20] - 2026-03-15

### Fixed

- 👥 **Multi-user mode works when running as root** — `ensure_os_user()` no longer unconditionally requires `sudo`; when the process is already root (e.g. `user: "0:0"` in Docker Compose), user provisioning commands run directly. `check_environment()` now only requires `sudo` when not running as root, with an actionable error message pointing to the standard image or Terminals when neither root nor sudo is available. ([#60](https://github.com/open-webui/open-terminal/issues/60))

## [0.11.19] - 2026-03-15

### Fixed

- 🔒 **`write_file` permission denied in multi-user subdirectories** — file writes into directories created by `run_command` failed because `mkdir -p` (via `sudo -u`) creates directories with default `755` permissions, leaving the server process without group-write access. `UserFS` now creates parent directories as the provisioned user (`sudo -u mkdir -p`) and sets `2770` (setgid + group rwx) on the entire directory chain, matching the home directory's permissions. Writing to the home root was unaffected. ([#70](https://github.com/open-webui/open-terminal/issues/70))

## [0.11.18] - 2026-03-15

### Added

- ⚡ **Configurable log flush strategy** — new `OPEN_TERMINAL_LOG_FLUSH_INTERVAL` and `OPEN_TERMINAL_LOG_FLUSH_BUFFER` environment variables (or `log_flush_interval` / `log_flush_buffer` in config.toml) control how frequently process output is flushed to disk. Default `0` preserves the existing per-chunk flush behaviour. Setting `OPEN_TERMINAL_LOG_FLUSH_INTERVAL=1` reduces fsyncs from ~250/sec to ~1/sec for high-output commands, preventing I/O storms that can make ARM/eMMC systems unresponsive. ([#65](https://github.com/open-webui/open-terminal/issues/65))

### Changed

- 🔧 **Centralized flush control** — per-chunk `flush()` calls removed from `PtyRunner`, `PipeRunner`, and `WinPtyRunner`; flushing is now managed entirely by `BoundedLogWriter` based on the configured interval and buffer settings. An explicit final flush is performed before writing the process end marker.

## [0.11.17] - 2026-03-14

### Fixed

- 🌐 **Global pip packages in multi-user mode** — `OPEN_TERMINAL_PIP_PACKAGES` now installs to the system-wide site-packages (`sudo pip install`) when `OPEN_TERMINAL_MULTI_USER=true`, so all provisioned users share the same packages. Previously, packages were installed to `/home/user/.local/` and only accessible to the default user. ([#68](https://github.com/open-webui/open-terminal/issues/68))

## [0.11.16] - 2026-03-13

### Removed

- 🧹 **Removed experimental `url` parameter from `/files/upload`** — this feature was never used by any known consumer (Open WebUI uses multipart uploads). The endpoint now only accepts direct file uploads.

## [0.11.15] - 2026-03-13

### Changed

- 🖥️ **Redesigned startup output** — the CLI now displays Local and Network URLs, the generated API key, and a bind warning in a clean, modern key-value layout with color-coded labels. Network URL auto-detects your LAN IP when binding to `0.0.0.0`.
- 🔒 **Bind warning** — a yellow warning is printed at startup when binding to `0.0.0.0`, nudging bare-metal users to restrict access with `--host 127.0.0.1`.

## [0.11.14] - 2026-03-13

### Fixed

- 🏠 **Multi-user home directory hints** — `get_system_info()` no longer includes `as user 'user'` in the OpenAPI description when multi-user mode is active, removing a misleading hint that caused smaller LLMs to write to `/home/user` instead of their assigned directory. ([#57](https://github.com/open-webui/open-terminal/issues/57))
- 🔄 **`/home/usr` path rewrite** — `resolve_path()` now also rewrites `/home/usr` (a common LLM hallucination) to the provisioned user's home directory, matching the existing `/home/user` rewrite.

## [0.11.13] - 2026-03-13

### Fixed

- 🐛 **Recursive home directory ownership fix** — `chown` in `ensure_os_user()` now uses `-R` to recursively fix ownership of all files within a user's home directory when the OS user is recreated with a different UID (e.g. after container recreation with a persistent volume). Previously only the top-level directory was re-owned, leaving files inside with a mismatched UID. ([#62](https://github.com/open-webui/open-terminal/issues/62))

## [0.11.12] - 2026-03-12

### Added

- 🔒 **Network egress filtering** (Docker only) — restrict which domains the container can access via the `OPEN_TERMINAL_ALLOWED_DOMAINS` env var. Supports wildcards (e.g. `*.github.com`). Set to empty string to block all outbound traffic; omit for full access. Skips gracefully on bare-metal installs.

## [0.11.11] - 2026-03-11

### Fixed

- 🔒 **Upload path traversal** — `/files/upload` now resolves the `directory` parameter through `fs.resolve_path()` and sanitizes the uploaded filename with `os.path.basename()`, preventing path traversal attacks (e.g. `../../etc/passwd`) that could escape the user's home directory in multi-user mode. The composed path is normalized with `os.path.normpath()` and validated by `_check_path` before writing. All other file endpoints already had these protections.

## [0.11.10] - 2026-03-11

### Fixed

- 🧟 **Zombie process cleanup** — process runner `kill()` methods now use `os.killpg()` to signal the entire process group instead of just the direct child PID. Background processes started inside terminals or `/execute` sessions (e.g. `sleep 100 &`) are now properly terminated on cleanup. `_cleanup_session()` always calls `process.wait()` after force-killing to prevent zombie entries in the process table.
- 🐳 **Docker PID 1 reaping** — added `tini` as the container's init process (`ENTRYPOINT ["/usr/bin/tini", "--", ...]`). Python/uvicorn no longer runs as PID 1, so orphaned grandchild processes are automatically reaped instead of accumulating as zombies.

## [0.11.9] - 2026-03-11

### Added

- ⏰ **Timestamp-sortable process IDs** — process IDs now use a `YYYYMMDD-HHMMSS-<random>` format so log files sort chronologically in the filesystem. The most recent log is always at the bottom of `ls`. ([#54](https://github.com/open-webui/open-terminal/issues/54))
- ⚙️ **`OPEN_TERMINAL_MAX_LOG_SIZE`** — environment variable (or `max_log_size` in config.toml) to set the per-process log file size limit in bytes. Default: 50 MB.
- ⚙️ **`OPEN_TERMINAL_LOG_RETENTION`** — environment variable (or `log_retention` in config.toml) to set how long finished-process log files are kept on disk before automatic cleanup. Default: 7 days.
- 📁 **`utils/log.py`** — extracted process log management code (`BoundedLogWriter`, `log_process`, `read_log`, `tail_log`) into a dedicated module to reduce `main.py` size.

### Fixed

- 🐛 **Memory leak — unbounded process log growth** — JSONL log files for background processes now rotate when they exceed a configurable size limit (`OPEN_TERMINAL_MAX_LOG_SIZE`, default 50 MB). When the limit is reached, the oldest half of the file is discarded and writing continues, so the most recent output is always available. Previously, a long-running process could grow its log file without limit, and `_read_log()` loaded the entire file into memory on every status poll — causing the container to consume all available host RAM (~26 GB) and trigger the OOM killer. ([#52](https://github.com/open-webui/open-terminal/issues/52))

## [0.11.8] - 2026-03-11

### Fixed

- 🔒 **Multi-user search isolation** — `glob_search`, `grep_search`, `listdir`, and `walk` now filter out entries belonging to other users' home directories during traversal. Previously, searching a parent directory like `/home` would expose all users' files. Added `is_path_allowed()` to `UserFS` for per-entry validation during `os.walk`. ([#46](https://github.com/open-webui/open-terminal/issues/46))

## [0.11.7] - 2026-03-11

### Fixed

- 🐛 **Multi-user relative path resolution** — relative paths (e.g. `abcdef.txt`, `.`) now resolve against the provisioned user's home directory instead of the server process's `/home/user`. All file, search, and execute endpoints use the new `UserFS.resolve_path()` method. ([#47](https://github.com/open-webui/open-terminal/issues/47))
- 🔄 **Auto-swap `/home/user` paths** — in multi-user mode, absolute paths under `/home/user` are automatically rewritten to the provisioned user's home directory, handling LLMs that hardcode the default path from the system description.

## [0.11.6] - 2026-03-10

### Added

- ℹ️ **Conditional `/info` endpoint** — new `OPEN_TERMINAL_INFO` environment variable (or `info` in config.toml) registers a `GET /info` endpoint that returns operator-provided context to the AI. Use it to describe the environment (e.g. container base OS, available tools, GPU access). When the variable is unset, the endpoint is not registered.

## [0.11.5] - 2026-03-09

### Fixed

- 🐛 **Terminal PTY warnings** — wrapped multi-user terminal sessions with `script -qc` for proper PTY allocation, eliminating `cannot set terminal process group` and `no job control` warnings.
- 🐛 **Stale home directory ownership** — added `chown` after `useradd` to handle pre-existing home directories with mismatched UID/GID from previous container runs.

### Changed

- 📖 **README** — updated multi-user documentation with accurate description and production warning.

## [0.11.4] - 2026-03-09

### Added

- 🔌 **Per-user port visibility** — in multi-user mode, `/ports` now filters by socket UID so each user only sees their own listening ports.

### Changed

- 📁 **Module reorganization** — moved `runner.py`, `notebooks.py`, and `user_isolation.py` into `open_terminal/utils/` for a cleaner package layout.

## [0.11.3] - 2026-03-09

### Fixed

- 🔒 **Cross-user file API isolation** — file endpoints now block access to other users' home directories via path validation, returning `403 Forbidden`. System paths (`/etc`, `/usr`, etc.) remain accessible.
- 🐛 **Terminal spawn directory** — interactive terminals now start in the user's home directory instead of `/home/user` (`sudo -i -u`).

### Changed

- ♻️ **Native Python I/O for writes** — replaced `sudo tee`, `sudo mkdir -p`, `sudo rm -rf`, `sudo mv` with native `aiofiles`/`os`/`shutil`. The only remaining subprocess is `sudo chown` for ownership fixup after writes. Home directories use `chmod 2770` (setgid + group rwx).

## [0.11.2] - 2026-03-09

### Changed

- ♻️ **Native Python I/O for multi-user reads** — replaced subprocess-based file reads (`cat`, `find -printf`, `stat -c`, `test`) with native `aiofiles`/`os` calls. Home directories now use `chmod 750` with group membership so the server can read directly. Writes still use `sudo -u` for correct ownership. Cross-user isolation preserved via Unix group permissions.
- 🐳 **Dockerfile** — grants `CAP_SETGID` to the Python binary via `setcap` so the server can refresh supplementary groups at runtime when provisioning new users.

## [0.11.1] - 2026-03-09

### Fixed

- 🐛 **Multi-user file operations** — all file endpoints (list, read, view, display, replace, grep, glob, upload) now correctly run as the provisioned user. Previously only write/delete/move were handled, causing `PermissionError` on reads in user home directories.

### Changed

- ♻️ **UserFS abstraction** (`open_terminal/utils/fs.py`) — unified filesystem interface that transparently routes I/O through `sudo -u` in multi-user mode. Endpoints receive a `UserFS` instance via dependency injection and no longer branch on mode. Replaces per-endpoint sudo wrappers.

## [0.11.0] - 2026-03-09

### Added

- 👥 **Multi-user mode** (`OPEN_TERMINAL_MULTI_USER=true`) — per-user OS accounts inside a single container, with standard Unix permissions (`chmod 700`) providing kernel-enforced isolation between users. When enabled, Open Terminal reads the `X-User-Id` header (set by the Open WebUI proxy), provisions a dedicated Linux user on first access via `useradd`, and runs all commands, file operations, and terminal sessions as that user via `sudo -u`. No Docker socket, no per-user containers, no enterprise license required. Fails fast with a clear error on non-Linux platforms. ([#38](https://github.com/open-webui/open-terminal/issues/38))
- ⚙️ **`OPEN_TERMINAL_UVICORN_LOOP`** — environment variable (or `uvicorn_loop` in config.toml) to configure the Uvicorn event loop implementation. Defaults to `auto`.

## [0.10.2] - 2026-03-06

### Added

- 🐳 **Docker CLI, Compose, and Buildx** bundled in the container image via [get.docker.com](https://get.docker.com). Mount the host's Docker socket (`-v /var/run/docker.sock:/var/run/docker.sock`) to let agents clone repos, build images, and run containers. The entrypoint automatically fixes socket group permissions so `docker` commands work without `sudo`.

## [0.10.1] - 2026-03-06

### Fixed

- 🌐 **UTF-8 encoding on Windows** — all text file I/O now explicitly uses UTF-8 encoding instead of the system default. Fixes Chinese (and other non-ASCII) content being written as GB2312 on Chinese Windows, which broke tool-call chaining and produced garbled files. ([#21](https://github.com/open-webui/open-terminal/issues/21))

## [0.10.0] - 2026-03-05

### Added

- 📓 **Notebook execution** (`/notebooks`) — multi-session Jupyter notebook execution via REST endpoints. Each session gets its own kernel via `nbclient`. Supports per-cell execution with rich outputs (images, HTML, LaTeX). `nbclient` and `ipykernel` are now core dependencies.
- ⚙️ **`OPEN_TERMINAL_ENABLE_NOTEBOOKS`** — environment variable (or `enable_notebooks` in config.toml) to enable/disable notebook execution endpoints. Defaults to `true`. Exposed in `GET /api/config` features.

## [0.9.3] - 2026-03-05

### Added

- 📓 **Notebook execution support** — new `notebooks` optional extra (`pip install open-terminal[notebooks]`) adds `nbclient` and `ipykernel` for running Jupyter notebooks with per-cell execution and full rich output (images, HTML, LaTeX). Keeps the core package lightweight for users who don't need notebook support.

## [0.9.2] - 2026-03-05

### Added

- 📝 **Custom execute description** — new `OPEN_TERMINAL_EXECUTE_DESCRIPTION` environment variable (or `execute_description` in config.toml) appends custom text to the execute endpoint's OpenAPI description, letting you tell AI models about installed tools, capabilities, or conventions.

## [0.9.1] - 2026-03-05

### Added

- 📦 **Startup package installation** — new `OPEN_TERMINAL_PACKAGES` and `OPEN_TERMINAL_PIP_PACKAGES` environment variables install additional apt and pip packages automatically when the Docker container starts. No need to fork the Dockerfile for common customizations.

## [0.9.0] - 2026-03-04

### Added

- 🔍 **Port detection** (`GET /ports`) — discovers TCP ports listening on localhost, scoped to descendant processes of open-terminal (servers started via the terminal or `/execute`). Cross-platform: parses `/proc/net/tcp` on Linux, `lsof` on macOS, `netstat` on Windows. Zero new dependencies.
- 🔀 **Port proxy** (`/proxy/{port}/{path}`) — reverse-proxies HTTP requests to `localhost:{port}`, enabling browser access to servers running inside the terminal environment. Supports all HTTP methods, forwards headers and body, returns 502 on connection refused. Uses the existing `httpx` dependency.
- 📦 **`utils.port` module** — port detection and process-tree utilities extracted into `open_terminal/utils/port.py` for reusability.

## [0.8.3] - 2026-03-04

### Added

- ⏱️ **Default execute timeout** — new `OPEN_TERMINAL_EXECUTE_TIMEOUT` environment variable (or `execute_timeout` in config.toml) sets a default wait duration for command execution. Smaller models that don't set timeouts now get command output inline instead of assuming failure.

## [0.8.2] - 2026-03-02

### Added

- 🎨 **Terminal color support** — terminal sessions now set the `TERM` environment variable (default `xterm-256color`) so programs emit ANSI color codes. Configurable via `OPEN_TERMINAL_TERM` environment variable or `term` in config.toml.

## [0.8.1] - 2026-03-02

### Added

- ⚙️ **Configurable terminal feature** — new `OPEN_TERMINAL_ENABLE_TERMINAL` environment variable (or `enable_terminal` in config.toml) to enable or disable the interactive terminal. When disabled, all `/api/terminals` routes and the WebSocket endpoint are not mounted. Defaults to `true`.
- 🔍 **Config discovery endpoint** (`GET /api/config`) — returns server feature flags so clients like Open WebUI can discover whether the terminal is enabled and adapt the UI accordingly.

## [0.8.0] - 2026-03-02

### Added

- 🪟 **Windows PTY support** — terminal sessions and command execution now work on Windows via [pywinpty](https://github.com/andfoy/pywinpty) (ConPTY). `pywinpty` is auto-installed on Windows. Interactive terminals (`/api/terminals`), colored output, and TUI apps now work on Windows instead of returning 503.
- 🏭 **WinPtyRunner** — new `ProcessRunner` implementation using `winpty.PtyProcess` for full PTY semantics on Windows, including resize support. The `create_runner` factory now prefers Unix PTY → WinPTY → pipe fallback.

## [0.7.2] - 2026-03-02

### Added

- 🔒 **Terminal session limit** — new `OPEN_TERMINAL_MAX_SESSIONS` environment variable (default `16`) caps the number of concurrent interactive terminal sessions. Dead sessions are automatically pruned before the limit is checked. Returns `429` when the limit is reached.

### Fixed

- 🐳 **PTY device exhaustion** — fixed `OSError: out of pty devices` by closing leaked file descriptors when subprocess creation fails after `pty.openpty()`. Both `PtyRunner` (command execution) and `create_terminal` (interactive sessions) now properly clean up on error paths.
- 🛡️ **Graceful PTY error handling** — `create_terminal` now returns a clear `503` with a descriptive message when the system runs out of PTY devices, instead of an unhandled server error.

## [0.7.1] - 2026-03-02

### Fixed

- 🐳 **Docker terminal shell** — fixed `can't access tty; job control turned off` error by setting the default shell to `/bin/bash` for the container user. Previously the user was created with `/bin/sh` (dash), which does not support interactive job control in a PTY.

## [0.7.0] - 2026-03-02

### Added

- 🖥️ **Interactive terminal sessions** — full PTY-based terminal accessible via WebSocket, following the JupyterLab/Kubernetes resource pattern. `POST /api/terminals` to create a session, `GET /api/terminals` to list, `DELETE /api/terminals/{id}` to kill, and `WS /api/terminals/{id}` to attach. Non-blocking I/O ensures the terminal never starves other API requests. Sessions are automatically cleaned up on disconnect.

## [0.6.0] - 2026-03-02

### Added

- 📄 **Configuration file support** — settings can now be loaded from TOML config files at /etc/open-terminal/config.toml (system-wide) and $XDG_CONFIG_HOME/open-terminal/config.toml (per-user, defaults to ~/.config/open-terminal/config.toml). Supports host, port, api_key, cors_allowed_origins, log_dir, and binary_mime_prefixes. CLI flags and environment variables still take precedence. Use --config to point to a custom config file. This keeps the API key out of ps / htop output.

## [0.5.0] - 2026-03-02

### Changed

- 📂 **XDG Base Directory support** — the default log directory moved from ~/.open-terminal/logs to the XDG-compliant path $XDG_STATE_HOME/open-terminal/logs (defaults to ~/.local/state/open-terminal/logs when XDG_STATE_HOME is not set). The OPEN_TERMINAL_LOG_DIR environment variable still overrides the default.

## [0.4.3] - 2026-03-02

### Added

- 🔐 **Docker secrets support** — set OPEN_TERMINAL_API_KEY_FILE to load the API key from a file (e.g. /run/secrets/...), following the convention used by the official PostgreSQL Docker image.

## [0.4.2] - 2026-03-02

### Added

- 📦 **Move endpoint** (POST /files/move) for moving and renaming files and directories. Uses shutil.move for cross-filesystem support. Hidden from OpenAPI schema.

## [0.4.1] - 2026-03-01

### Fixed

- 🙈 **Hidden upload_file from OpenAPI schema** — the /files/upload endpoint is now excluded from the public API docs, consistent with other internal-only file endpoints.

## [0.4.0] - 2026-03-01

### Removed

- 📥 **Temporary download links** (GET /files/download/link and GET /files/download/{token}) — deprecated in favour of direct file navigation built into Open WebUI.
- 🔗 **Temporary upload links** (POST /files/upload/link, GET /files/upload/{token}, and POST /files/upload/{token}) — deprecated in favour of direct file navigation built into Open WebUI.

## [0.3.0] - 2026-02-25

### Added

- 🖥️ **Pseudo-terminal (PTY) execution** — commands now run under a real PTY by default, enabling colored output, interactive programs (REPLs, TUI apps), and proper isatty() detection. Falls back to pipe-based execution on Windows.
- 🏭 **Process runner abstraction** — new ProcessRunner factory pattern (PtyRunner / PipeRunner) in runner.py for clean, extensible process management.
- 🔡 **Escape sequence conversion** in send_process_input — literal escape strings from LLMs (\n, \x03 for Ctrl-C, \x04 for Ctrl-D, etc.) are automatically converted to real characters.

### Changed

- 📦 **Merged output stream** — PTY output is logged as type "output" (merged stdout/stderr) instead of separate streams, matching real terminal behavior.

## [0.2.9] - 2026-02-25

### Added

- 📺 **Display file endpoint** (GET /files/display) — a signaling endpoint that lets AI agents request a file be shown to the user. The consuming client is responsible for handling the response and presenting the file in its own UI.

### Changed

- ⏳ **Improved wait behavior** — wait=0 on the status endpoint now correctly triggers a wait instead of being treated as falsy, so commands that finish quickly return immediately rather than requiring a non-zero wait value.

## [0.2.8] - 2026-02-25

### Added

- 📄 **PDF text extraction** in read_file — PDF files are now automatically converted to text using pypdf and returned in the standard text-file JSON format, making them readable by LLMs. Supports start_line/end_line range selection.

## [0.2.7] - 2026-02-25

### Added

- 👁️ **File view endpoint** (GET /files/view) for serving raw binary content of any file type with the correct Content-Type. Designed for UI previewing (PDFs, images, etc.) without the MIME restrictions of read_file.
- 📂 **--cwd CLI option** for both run and mcp commands to set the server's working directory on startup.
- 📍 **Working directory endpoints** — GET /files/cwd and POST /files/cwd to query and change the current working directory at runtime.
- 📁 **mkdir endpoint** (POST /files/mkdir) to create directories with automatic parent directory creation.
- 🗑️ **delete endpoint** (DELETE /files/delete) to remove files and directories.

### Changed

- 📄 **Binary-aware read_file** returns raw binary responses for supported file types (images, etc.) and rejects unsupported binary files with a descriptive error. Configurable via OPEN_TERMINAL_BINARY_MIME_PREFIXES env var.

## [0.2.6] - 2026-02-24

### Added

- 🔍 **File Search Endpoints**: Added a new /files/glob endpoint (alias glob_search) to search for files by name/pattern using wildcards.
- 🔄 **Alias Update**: Renamed and aliased the existing /files/search endpoint to /files/grep (alias grep_search) to establish a clear distinction between content-level search (grep) and filename-level search (glob).

## [0.2.5] - 2026-02-23

### Fixed

- 🛡️ **Graceful permission error handling** across all file endpoints (write_file, replace_file_content, upload_file). PermissionError and other OSError exceptions now return HTTP 400 with a descriptive message instead of crashing with HTTP 500.
- 🐳 **Docker volume permissions** via entrypoint.sh that automatically fixes /home/user ownership on startup when a host volume is mounted with mismatched permissions.
- 🔧 **Background process resilience** — _log_process no longer crashes if the log directory is unwritable; commands still execute and complete normally.

## [0.2.4] - 2026-02-19

### Changed

- ⚡ **Fully async I/O** across all file and upload endpoints. Replaced blocking os.* and open() calls with aiofiles and aiofiles.os so the event loop is never blocked by filesystem operations. search_files and list_files inner loops use asyncio.to_thread for os.walk/os.listdir workloads.

## [0.2.3] - 2026-02-15

### Added

- 🤖 **Optional MCP server mode** via open-terminal mcp, exposing all endpoints as MCP tools for LLM agent integration. Supports stdio and streamable-http transports. Install with pip install open-terminal[mcp].

## [0.2.2] - 2026-02-15

### Fixed

- 🛡️ **Null query parameter tolerance** via HTTP middleware that strips query parameters with the literal value "null". Prevents 422 errors when clients serialize null into query strings (e.g. ?wait=null) instead of omitting the parameter.

## [0.2.1] - 2026-02-14

### Added

- 📁 **File-backed process output** persisted to JSONL log files under 'logs/processes/', configurable via 'OPEN_TERMINAL_LOG_DIR'. Full audit trail survives process cleanup and server restarts.
- 📍 **Offset-based polling** on the status endpoint with 'offset' and 'next_offset' for stateless incremental reads. Multiple clients can independently track the same process without data loss.
- ✂️ **Tail parameter** on both execute and status endpoints to return only the last N output entries, keeping AI agent responses bounded.

### Changed

- 🗑️ **Removed in-memory output buffer** in favor of reading directly from the JSONL log file as the single source of truth.
- 📂 **Organized log directory** with process logs namespaced under 'logs/processes/' to accommodate future log types.

### Removed

- 🔄 **Bounded output buffers** and the 'OPEN_TERMINAL_MAX_OUTPUT_LINES' environment variable, no longer needed without in-memory buffering.

## [0.2.0] - 2026-02-14

### Added

- 📂 **File operations** for reading, writing, listing, and find-and-replace, with optional line-range selection for large files.
- 📤 **File upload** by URL or multipart form data.
- 📥 **Temporary download links** that work without authentication, making it easy to retrieve files from the container.
- 🔗 **Temporary upload links** with a built-in drag-and-drop page for sharing with others.
- ⌨️ **Stdin input** to send text to running processes, enabling interaction with REPLs and interactive commands.
- 📋 **Process listing** to view all tracked background processes and their current status at a glance.
- ⏳ **Synchronous mode** with an optional 'wait' parameter to block until a command finishes and get output inline.
- 🔄 **Bounded output buffers** to prevent memory issues on long-running commands, configurable via 'OPEN_TERMINAL_MAX_OUTPUT_LINES'.
- 🛠️ **Rich toolbox** pre-installed in the container, including Python data science libraries, networking utilities, editors, and build tools.
- 👤 **Non-root user** with passwordless 'sudo' available when elevated privileges are needed.
- 🚀 **CI/CD pipeline** for automated multi-arch Docker image builds and publishing via GitHub Actions.
- 💾 **Named volume** in the default 'docker run' command so your files survive container restarts.

### Changed

- 🐳 **Expanded container image** with system packages and Python libraries for a batteries-included experience.

## [0.1.0] - 2026-02-12

### Added

- 🎉 **Initial release** of Open Terminal, a lightweight API that turns any container into a remote shell for AI agents and automation workflows.
- ▶️ **Background command execution** with async process tracking, supporting shell features like pipes, chaining, and redirections.
- 🔑 **Bearer token authentication** to secure your instance using the 'OPEN_TERMINAL_API_KEY' environment variable.
- 🔐 **Zero-config setup** with an auto-generated API key printed to container logs when none is provided.
- 💚 **Health check** endpoint at '/health' for load balancer and orchestrator integration.
- 🌐 **CORS enabled by default** for seamless integration with web-based AI tools and dashboards.
