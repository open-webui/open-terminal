# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

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
