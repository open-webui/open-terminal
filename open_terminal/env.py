import os
import sys

from open_terminal import config


def sanitized_environ(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Exclude inherited server settings; explicit command overrides still win."""
    env = {key: value for key, value in os.environ.items() if not key.startswith("OPEN_TERMINAL_")}
    if BROWSER_NO_SANDBOX:
        # The Docker image's Chromium launcher reads this to add --no-sandbox.
        env["OPEN_TERMINAL_BROWSER_NO_SANDBOX"] = "true"
    if extra:
        env.update(extra)
    return env


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

    return value if value is not None else default


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
# Once exceeded, the oldest portion is removed on disk; output capture continues.
MAX_PROCESS_LOG_SIZE = int(
    os.environ.get(
        "OPEN_TERMINAL_MAX_LOG_SIZE",
        config.get("max_log_size", 50_000_000),  # 50 MB
    )
)

# How long (in seconds) to keep finished-process log files on disk.
# A periodic disk sweep removes expired logs independently of process records.
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

# Notebook support is optional; the base package must start without Jupyter.
if ENABLE_NOTEBOOKS:
    from importlib.util import find_spec
    ENABLE_NOTEBOOKS = all(find_spec(module) is not None for module in ("nbclient", "ipykernel"))

ENABLE_BROWSER = os.environ.get(
    "OPEN_TERMINAL_ENABLE_BROWSER",
    str(config.get("enable_browser", True)),
).lower() not in ("false", "0", "no")

BROWSER_NO_SANDBOX = os.environ.get(
    "OPEN_TERMINAL_BROWSER_NO_SANDBOX", "false"
).lower() in ("true", "1", "yes")

BROWSER_MODE = os.environ.get(
    "OPEN_TERMINAL_BROWSER_MODE", config.get("browser_mode", "managed")
).lower()
if BROWSER_MODE not in ("managed", "personal"):
    raise ValueError("OPEN_TERMINAL_BROWSER_MODE must be 'managed' or 'personal'")
BROWSER_CDP_URL = os.environ.get(
    "OPEN_TERMINAL_BROWSER_CDP_URL", config.get("browser_cdp_url", "")
)
BROWSER_USER_DATA_DIR = os.environ.get(
    "OPEN_TERMINAL_BROWSER_USER_DATA_DIR", config.get("browser_user_data_dir", "")
)

ENABLE_DESKTOP = os.environ.get(
    "OPEN_TERMINAL_ENABLE_DESKTOP",
    str(config.get("enable_desktop", True)),
).lower() not in ("false", "0", "no")

DESKTOP_MODE = os.environ.get(
    "OPEN_TERMINAL_DESKTOP_MODE",
    config.get("desktop_mode", "virtual" if sys.platform == "linux" else "host"),
).lower()
if DESKTOP_MODE not in ("virtual", "host"):
    raise ValueError("OPEN_TERMINAL_DESKTOP_MODE must be 'virtual' or 'host'")
DESKTOP_ENVIRONMENT = os.environ.get(
    "OPEN_TERMINAL_DESKTOP_ENVIRONMENT", config.get("desktop_environment", "auto")
).lower()
if DESKTOP_ENVIRONMENT not in ("auto", "gnome", "xfce"):
    raise ValueError("OPEN_TERMINAL_DESKTOP_ENVIRONMENT must be 'auto', 'gnome', or 'xfce'")

ENABLE_SYSTEM_PROMPT = os.environ.get(
    "OPEN_TERMINAL_ENABLE_SYSTEM_PROMPT",
    str(config.get("enable_system_prompt", True)),
).lower() not in ("false", "0", "no")

SYSTEM_PROMPT = os.environ.get(
    "OPEN_TERMINAL_SYSTEM_PROMPT",
    config.get("system_prompt", ""),
)

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

UVICORN_TIMEOUT_KEEP_ALIVE: float | None = None
_uvicorn_timeout_keep_alive = os.environ.get(
    "OPEN_TERMINAL_UVICORN_TIMEOUT_KEEP_ALIVE",
    config.get("uvicorn_timeout_keep_alive"),
)
if _uvicorn_timeout_keep_alive is not None:
    UVICORN_TIMEOUT_KEEP_ALIVE = float(_uvicorn_timeout_keep_alive)

OPEN_TERMINAL_INFO = os.environ.get(
    "OPEN_TERMINAL_INFO",
    config.get("info", ""),
)

FILE_BROWSER_ROOT = os.environ.get(
    "OPEN_TERMINAL_FILE_BROWSER_ROOT",
    config.get("file_browser_root", "home"),
).strip()

# Where every session, shell, and command starts unless a request names a directory.
# "~", "~/path", or a path relative to home; folders outside home are ignored.
START_DIR = os.environ.get(
    "OPEN_TERMINAL_START_DIR",
    config.get("start_dir", ""),
).strip()

# Shell script run in START_DIR each time the server starts, before it accepts requests.
SETUP_SCRIPT = os.environ.get(
    "OPEN_TERMINAL_SETUP_SCRIPT",
    config.get("setup_script", ""),
)

# How long (in seconds) to keep per-session cwd entries in memory.
# Sliding window — refreshed on every access.
SESSION_CWD_TTL: float = float(
    os.environ.get(
        "OPEN_TERMINAL_SESSION_CWD_TTL",
        config.get("session_cwd_ttl", 604_800),  # 7 days
    )
)
