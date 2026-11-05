"""TOML configuration file support.

Settings are resolved with this precedence (highest wins):

1. CLI flags
2. Environment variables / Docker-secrets ``_FILE`` variants
3. User config — ``$XDG_CONFIG_HOME/open-terminal/config.toml``
   (defaults to ``~/.config/open-terminal/config.toml``)
4. System config — ``/etc/open-terminal/config.toml``
5. Built-in defaults
"""

import os
import sys
import tomllib
from pathlib import Path


def _default_user_config_path() -> Path:
    """Return the XDG-compliant user config file path."""
    xdg = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
        os.path.expanduser("~"), ".config"
    )
    return Path(xdg) / "open-terminal" / "config.toml"


_SYSTEM_CONFIG_PATH = Path("/etc/open-terminal/config.toml")


def load_config(explicit_path: str | None = None, *, isolated: bool = False) -> dict:
    """Load and merge TOML configuration files.

    Parameters
    ----------
    explicit_path:
        If given, this file replaces the *user-level* lookup.  The
        system-level config is still loaded underneath.

    Returns
    -------
    dict
        Merged configuration dictionary.  System values are overridden
        by user (or explicit) values.
    """
    merged: dict = {}

    # 1. System config (lowest priority of the two files)
    if not isolated and _SYSTEM_CONFIG_PATH.is_file():
        try:
            merged.update(tomllib.loads(_SYSTEM_CONFIG_PATH.read_text("utf-8")))
        except Exception as exc:
            print(
                f"Warning: failed to read {_SYSTEM_CONFIG_PATH}: {exc}",
                file=sys.stderr,
            )

    # 2. User / explicit config (overrides system)
    user_path = Path(explicit_path) if explicit_path else _default_user_config_path()
    if (explicit_path or not isolated) and user_path.is_file():
        try:
            merged.update(tomllib.loads(user_path.read_text("utf-8")))
        except Exception as exc:
            # If the user explicitly asked for this file, treat errors as fatal.
            if explicit_path:
                raise SystemExit(f"Error: failed to read {user_path}: {exc}") from exc
            print(
                f"Warning: failed to read {user_path}: {exc}",
                file=sys.stderr,
            )

    return merged


# Module-level merged config, lazily populated by ``init()``.
_config: dict = {}


def init(explicit_path: str | None = None, *, isolated: bool = False) -> dict:
    """Load config files and cache the result module-wide.

    This should be called once, early in startup (e.g. from the CLI
    entry-point), *before* ``env.py`` constants are evaluated.
    """
    global _config
    _config = load_config(explicit_path, isolated=isolated or os.environ.get('OPEN_TERMINAL_ISOLATED', '').lower() in ('true', '1'))
    return _config


def get(key: str, default=None):
    """Look up a value from the loaded config."""
    return _config.get(key, default)
