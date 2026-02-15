# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

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
