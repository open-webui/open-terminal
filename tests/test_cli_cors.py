"""Regression tests for CORS origin resolution (open-webui/open-terminal#182).

``open-terminal run`` must resolve the allowed CORS origins with this
precedence (highest wins):

1. ``--cors-allowed-origins`` CLI flag
2. ``OPEN_TERMINAL_CORS_ALLOWED_ORIGINS`` environment variable
3. ``cors_allowed_origins`` in the user (or ``--config``) config file
4. ``cors_allowed_origins`` in the system config file
5. Built-in default ``"*"``
"""

import importlib
import os

import click
import pytest
from click.testing import CliRunner

from open_terminal import cli, config

ENV_VAR = "OPEN_TERMINAL_CORS_ALLOWED_ORIGINS"
WILDCARD_WARNING = "CORS is set to '*'"

CFG_ORIGINS = "https://config.example"
SYSTEM_ORIGINS = "https://system.example"
ENV_ORIGINS = "https://env.example"
CLI_ORIGINS = "https://cli.example"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """Isolate each test from host env vars and real config files."""
    saved_env = os.environ.copy()
    for var in (ENV_VAR, "OPEN_TERMINAL_API_KEY", "OPEN_TERMINAL_API_KEY_FILE"):
        os.environ.pop(var, None)
    os.environ["XDG_CONFIG_HOME"] = str(tmp_path / "xdg")
    monkeypatch.setattr(
        config,
        "_SYSTEM_CONFIG_PATH",
        tmp_path / "etc" / "open-terminal" / "config.toml",
    )
    yield
    # ``run`` writes the resolved value into os.environ; undo that too.
    os.environ.clear()
    os.environ.update(saved_env)
    config._config = {}


@pytest.fixture
def run_cli(monkeypatch):
    """Invoke ``open-terminal run`` without starting a server.

    Returns ``(result, cors)`` where ``cors`` is the value of
    OPEN_TERMINAL_CORS_ALLOWED_ORIGINS at the moment uvicorn would start,
    i.e. what ``open_terminal.env`` / the CORS middleware will read.
    """
    captured = {}

    def fake_uvicorn_run(app, **kwargs):
        captured["cors"] = os.environ.get(ENV_VAR)

    monkeypatch.setattr(cli.uvicorn, "run", fake_uvicorn_run)

    def _run(*args, env=None):
        captured.clear()
        result = CliRunner().invoke(
            cli.main,
            ["run", "--host", "127.0.0.1", "--api-key", "test-key", *args],
            env=env,
        )
        return result, captured.get("cors")

    return _run


def _write(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def user_config(tmp_path, body):
    return _write(tmp_path / "xdg" / "open-terminal" / "config.toml", body)


def system_config(body):
    return _write(config._SYSTEM_CONFIG_PATH, body)


def cors_toml(value):
    return f'cors_allowed_origins = "{value}"\n'


# ---------------------------------------------------------------------------
# Level 5: built-in default
# ---------------------------------------------------------------------------


def test_builtin_default_is_wildcard(run_cli):
    result, cors = run_cli()
    assert result.exit_code == 0, result.output
    assert cors == "*"
    assert WILDCARD_WARNING in result.output


# ---------------------------------------------------------------------------
# Levels 3/4: config files (the bug reported in #182)
# ---------------------------------------------------------------------------


def test_explicit_config_file_is_used(run_cli, tmp_path):
    cfg = _write(tmp_path / "custom.toml", cors_toml(CFG_ORIGINS))
    result, cors = run_cli("--config", str(cfg))
    assert result.exit_code == 0, result.output
    assert cors == CFG_ORIGINS
    assert WILDCARD_WARNING not in result.output


def test_user_config_is_used(run_cli, tmp_path):
    user_config(tmp_path, cors_toml(CFG_ORIGINS))
    result, cors = run_cli()
    assert result.exit_code == 0, result.output
    assert cors == CFG_ORIGINS


def test_system_config_is_used(run_cli):
    system_config(cors_toml(SYSTEM_ORIGINS))
    result, cors = run_cli()
    assert result.exit_code == 0, result.output
    assert cors == SYSTEM_ORIGINS


def test_user_config_overrides_system_config(run_cli, tmp_path):
    system_config(cors_toml(SYSTEM_ORIGINS))
    user_config(tmp_path, cors_toml(CFG_ORIGINS))
    result, cors = run_cli()
    assert result.exit_code == 0, result.output
    assert cors == CFG_ORIGINS


def test_config_without_cors_key_falls_back_to_default(run_cli, tmp_path):
    user_config(tmp_path, "port = 8001\n")
    result, cors = run_cli()
    assert result.exit_code == 0, result.output
    assert cors == "*"


def test_config_wildcard_is_respected(run_cli, tmp_path):
    user_config(tmp_path, cors_toml("*"))
    result, cors = run_cli()
    assert result.exit_code == 0, result.output
    assert cors == "*"
    assert WILDCARD_WARNING in result.output


def test_config_multiple_origins_passed_through(run_cli, tmp_path):
    user_config(tmp_path, cors_toml("https://a.example, https://b.example"))
    result, cors = run_cli()
    assert result.exit_code == 0, result.output
    assert cors == "https://a.example, https://b.example"


@pytest.mark.parametrize("toml_value", ["123", "true", '["https://a.example"]'])
def test_non_string_config_value_is_rejected(run_cli, tmp_path, toml_value):
    user_config(tmp_path, f"cors_allowed_origins = {toml_value}\n")
    result, cors = run_cli()
    assert result.exit_code != 0
    assert cors is None  # server never started
    assert "cors_allowed_origins" in result.output


# ---------------------------------------------------------------------------
# Level 2: environment variable
# ---------------------------------------------------------------------------


def test_env_used_without_config(run_cli):
    result, cors = run_cli(env={ENV_VAR: ENV_ORIGINS})
    assert result.exit_code == 0, result.output
    assert cors == ENV_ORIGINS


def test_env_overrides_user_and_system_config(run_cli, tmp_path):
    system_config(cors_toml(SYSTEM_ORIGINS))
    user_config(tmp_path, cors_toml(CFG_ORIGINS))
    result, cors = run_cli(env={ENV_VAR: ENV_ORIGINS})
    assert result.exit_code == 0, result.output
    assert cors == ENV_ORIGINS


def test_explicit_env_wildcard_overrides_config(run_cli, tmp_path):
    user_config(tmp_path, cors_toml(CFG_ORIGINS))
    result, cors = run_cli(env={ENV_VAR: "*"})
    assert result.exit_code == 0, result.output
    assert cors == "*"
    assert WILDCARD_WARNING in result.output


def test_empty_env_var_is_treated_as_unset(run_cli, tmp_path):
    # Click ignores empty environment variables, so ``VAR=`` behaves like
    # an unset variable and the config file still applies.
    user_config(tmp_path, cors_toml(CFG_ORIGINS))
    result, cors = run_cli(env={ENV_VAR: ""})
    assert result.exit_code == 0, result.output
    assert cors == CFG_ORIGINS


# ---------------------------------------------------------------------------
# Level 1: CLI flag
# ---------------------------------------------------------------------------


def test_cli_overrides_config(run_cli, tmp_path):
    user_config(tmp_path, cors_toml(CFG_ORIGINS))
    result, cors = run_cli("--cors-allowed-origins", CLI_ORIGINS)
    assert result.exit_code == 0, result.output
    assert cors == CLI_ORIGINS


def test_cli_overrides_env_and_config(run_cli, tmp_path):
    system_config(cors_toml(SYSTEM_ORIGINS))
    user_config(tmp_path, cors_toml(CFG_ORIGINS))
    result, cors = run_cli(
        "--cors-allowed-origins", CLI_ORIGINS, env={ENV_VAR: ENV_ORIGINS}
    )
    assert result.exit_code == 0, result.output
    assert cors == CLI_ORIGINS


def test_explicit_cli_wildcard_overrides_config(run_cli, tmp_path):
    # Regression guard: an explicit ``*`` must not be mistaken for Click's
    # default and replaced by the config value.
    user_config(tmp_path, cors_toml(CFG_ORIGINS))
    result, cors = run_cli("--cors-allowed-origins", "*")
    assert result.exit_code == 0, result.output
    assert cors == "*"
    assert WILDCARD_WARNING in result.output


def test_explicit_cli_wildcard_overrides_env(run_cli):
    result, cors = run_cli("--cors-allowed-origins", "*", env={ENV_VAR: ENV_ORIGINS})
    assert result.exit_code == 0, result.output
    assert cors == "*"


def test_cli_restricts_wildcard_config(run_cli, tmp_path):
    user_config(tmp_path, cors_toml("*"))
    result, cors = run_cli("--cors-allowed-origins", CLI_ORIGINS)
    assert result.exit_code == 0, result.output
    assert cors == CLI_ORIGINS
    assert WILDCARD_WARNING not in result.output


def test_explicit_empty_cli_value_is_not_widened(run_cli, tmp_path):
    # An explicit empty value is passed through unchanged; it must never be
    # silently replaced by the config value or the permissive default.
    user_config(tmp_path, cors_toml(CFG_ORIGINS))
    result, cors = run_cli("--cors-allowed-origins", "")
    assert result.exit_code == 0, result.output
    assert cors == ""
    assert WILDCARD_WARNING not in result.output


# ---------------------------------------------------------------------------
# End-to-end: the value the app actually reads
# ---------------------------------------------------------------------------


def test_env_module_sees_config_value(run_cli, tmp_path):
    user_config(tmp_path, cors_toml(CFG_ORIGINS))
    result, _ = run_cli()
    assert result.exit_code == 0, result.output

    from open_terminal import env

    env = importlib.reload(env)
    assert env.CORS_ALLOWED_ORIGINS == CFG_ORIGINS


# ---------------------------------------------------------------------------
# Unit tests for the resolver
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("cli_value", "cfg", "expected"),
    [
        (None, {}, "*"),
        (None, {"cors_allowed_origins": CFG_ORIGINS}, CFG_ORIGINS),
        (None, {"cors_allowed_origins": "*"}, "*"),
        (CLI_ORIGINS, {"cors_allowed_origins": CFG_ORIGINS}, CLI_ORIGINS),
        ("*", {"cors_allowed_origins": CFG_ORIGINS}, "*"),
        ("", {"cors_allowed_origins": CFG_ORIGINS}, ""),
    ],
)
def test_resolve_cors_allowed_origins(cli_value, cfg, expected):
    assert cli.resolve_cors_allowed_origins(cli_value, cfg) == expected


def test_resolve_rejects_non_string_config():
    with pytest.raises(click.BadParameter):
        cli.resolve_cors_allowed_origins(None, {"cors_allowed_origins": 1})
