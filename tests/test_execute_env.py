import os
import shlex
import subprocess

import pytest

os.environ.setdefault("OPEN_TERMINAL_API_KEY", "test-key")

from open_terminal.utils import runner

pytestmark = pytest.mark.skipif(not hasattr(runner, "PtyRunner"), reason="PTY runner is Unix-only")


class _Captured(Exception):
    pass


def spawn(monkeypatch, env, run_as_user):
    seen = {}

    def fake_popen(command, **kwargs):
        seen["command"] = command
        seen["env"] = kwargs.get("env")
        raise _Captured

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    with pytest.raises(_Captured):
        runner.PtyRunner("echo hi", "/tmp", env, run_as_user=run_as_user)
    return seen


def test_multi_user_env_is_forwarded_after_sudo(monkeypatch):
    seen = spawn(monkeypatch, {"FOO": "a b", "BAR": "1"}, "alice")
    argv = shlex.split(seen["command"])
    assert argv[:4] == ["sudo", "-u", "alice", "--"]
    assert argv[4:7] == ["env", "FOO=a b", "BAR=1"]
    assert argv[7:9] == ["bash", "-c"]
    assert argv[9] == "cd /tmp && echo hi"


def test_multi_user_launching_shell_keeps_the_server_environment(monkeypatch):
    monkeypatch.setenv("SERVER_ONLY", "server")
    seen = spawn(monkeypatch, {"FOO": "request", "PATH": "/request/bin"}, "alice")
    assert "FOO" not in seen["env"]
    assert seen["env"]["SERVER_ONLY"] == "server"
    assert seen["env"].get("PATH") != "/request/bin"


def test_multi_user_drops_names_that_are_not_variables(monkeypatch):
    seen = spawn(monkeypatch, {"-i": "x", "A=B": "y", "1X": "z", "OK": "v"}, "alice")
    argv = shlex.split(seen["command"])
    assert argv[4:6] == ["env", "OK=v"]
    assert argv[6] == "bash"


def test_multi_user_without_env_has_no_env_wrapper(monkeypatch):
    argv = shlex.split(spawn(monkeypatch, None, "alice")["command"])
    assert argv[4] == "bash"


def test_single_user_env_reaches_the_process(monkeypatch):
    seen = spawn(monkeypatch, {"FOO": "bar"}, None)
    assert seen["command"] == "echo hi"
    assert seen["env"]["FOO"] == "bar"


def test_env_value_arrives_in_the_user_command():
    command = f"{runner.forwarded_env_args({'GREETING': 'it works'})}sh -c 'printf %s \"$GREETING\"'"
    result = subprocess.run(command, shell=True, capture_output=True, text=True, env={"PATH": os.defpath}, check=False)
    assert result.stdout == "it works"
