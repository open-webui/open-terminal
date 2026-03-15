import os

from open_terminal import config


def _resolve_file_env(var: str, default: str = "") -> str:
    """Resolve an environment variable with Docker-secrets ``_FILE`` support.

    If ``<var>_FILE`` is set, its value is treated as a path whose contents
    supply the variable's value (trailing whitespace is stripped).  Setting
    *both* ``<var>`` and ``<var>_FILE`` is an error.

    This follows the convention established by the official PostgreSQL Docker
    image (see https://hub.docker.com/_/postgres#docker-secrets).
    """
    value = os.environ.get(var)
    file_path = os.environ.get(f"{var}_FILE")

    if value is not None and file_path is not None:
        raise ValueError(
            f"Both {var} and {var}_FILE are set, but they are mutually exclusive."
        )

    if file_path:
        with open(file_path) as f:
            return f.read().strip()

    return value or default


API_KEY = _resolve_file_env("OPEN_TERMINAL_API_KEY", config.get("api_key", ""))
CORS_ALLOWED_ORIGINS = os.environ.get(
    "OPEN_TERMINAL_CORS_ALLOWED_ORIGINS",
    config.get("cors_allowed_origins", "*"),
)
LOG_DIR = os.environ.get(
    "OPEN_TERMINAL_LOG_DIR",
    config.get(
        "log_dir",
        os.path.join(
            os.environ.get(
                "XDG_STATE_HOME",
                os.path.join(os.path.expanduser("~"), ".local", "state"),
            ),
            "open-terminal",
            "logs",
        ),
    ),
)

# Comma-separated mime type prefixes for binary files that read_file will return
# as raw binary responses (e.g. "image,audio" or "image/png,image/jpeg").
BINARY_FILE_MIME_PREFIXES = [
    p.strip()
    for p in os.environ.get(
        "OPEN_TERMINAL_BINARY_MIME_PREFIXES",
        config.get("binary_mime_prefixes", "image"),
    ).split(",")
    if p.strip()
]

MAX_TERMINAL_SESSIONS = int(
    os.environ.get(
        "OPEN_TERMINAL_MAX_SESSIONS",
        config.get("max_terminal_sessions", "16"),
    )
)

ENABLE_TERMINAL = os.environ.get(
    "OPEN_TERMINAL_ENABLE_TERMINAL",
    str(config.get("enable_terminal", True)),
).lower() not in ("false", "0", "no")

TERMINAL_TERM = os.environ.get(
    "OPEN_TERMINAL_TERM",
    config.get("term", "xterm-256color"),
)

EXECUTE_TIMEOUT: float | None = None
_execute_timeout = os.environ.get(
    "OPEN_TERMINAL_EXECUTE_TIMEOUT",
    config.get("execute_timeout"),
)
if _execute_timeout is not None:
    EXECUTE_TIMEOUT = float(_execute_timeout)

EXECUTE_DESCRIPTION = os.environ.get(
    "OPEN_TERMINAL_EXECUTE_DESCRIPTION",
    config.get("execute_description", ""),
)

# Maximum size (in bytes) for per-process JSONL log files.
# Once exceeded, logging stops for that process (the process keeps running).
MAX_PROCESS_LOG_SIZE = int(
    os.environ.get(
        "OPEN_TERMINAL_MAX_LOG_SIZE",
        config.get("max_log_size", 50_000_000),  # 50 MB
    )
)

# How long (in seconds) to keep finished-process log files on disk.
# After this period, _cleanup_expired() will delete the log file.
PROCESS_LOG_RETENTION: float = float(
    os.environ.get(
        "OPEN_TERMINAL_LOG_RETENTION",
        config.get("log_retention", 604_800),  # 7 days
    )
)

# Minimum interval (in seconds) between log flushes during command execution.
# 0 (default) = flush after every chunk (current behaviour).
# Setting this to e.g. 1.0 reduces I/O pressure on high-output commands.
LOG_FLUSH_INTERVAL: float = float(
    os.environ.get(
        "OPEN_TERMINAL_LOG_FLUSH_INTERVAL",
        config.get("log_flush_interval", 0),
    )
)

# Maximum unflushed buffer (in bytes) before a flush is forced.
# Only relevant when LOG_FLUSH_INTERVAL > 0.  0 = no buffer limit.
LOG_FLUSH_BUFFER: int = int(
    os.environ.get(
        "OPEN_TERMINAL_LOG_FLUSH_BUFFER",
        config.get("log_flush_buffer", 0),
    )
)

ENABLE_NOTEBOOKS = os.environ.get(
    "OPEN_TERMINAL_ENABLE_NOTEBOOKS",
    str(config.get("enable_notebooks", True)),
).lower() not in ("false", "0", "no")

MULTI_USER = os.environ.get(
    "OPEN_TERMINAL_MULTI_USER",
    str(config.get("multi_user", False)),
).lower() not in ("false", "0", "no", "")

USER_PREFIX = os.environ.get(
    "OPEN_TERMINAL_USER_PREFIX",
    config.get("user_prefix", ""),
)

UVICORN_LOOP = os.environ.get(
    "OPEN_TERMINAL_UVICORN_LOOP",
    config.get("uvicorn_loop", "auto"),
)

OPEN_TERMINAL_INFO = os.environ.get(
    "OPEN_TERMINAL_INFO",
    config.get("info", ""),
)


