import os
import stat
from types import SimpleNamespace

import pytest

os.environ.setdefault("OPEN_TERMINAL_API_KEY", "test-key")

from open_terminal.agents import sessions

pwd = pytest.importorskip("pwd")


def executable(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


@pytest.fixture
def user_home(tmp_path, monkeypatch):
    """A non-root server launching for another OS user whose PATH shadows sudo."""
    home = tmp_path / "home"
    executable(home / ".local" / "bin" / "sudo")
    executable(home / ".local" / "bin" / "codex")
    account = SimpleNamespace(pw_dir=str(home), pw_uid=12345, pw_gid=12345)
    monkeypatch.setattr(pwd, "getpwnam", lambda name: account)
    monkeypatch.setattr(os, "getuid", lambda: 1000)
    monkeypatch.delenv("OPEN_TERMINAL_CODEX_COMMAND", raising=False)
    return home


def test_sudo_is_resolved_from_the_system_path(user_home):
    env, kwargs, prefix = sessions.identity(SimpleNamespace(username="alice"))
    assert env["PATH"].split(os.pathsep)[0] == str(user_home / ".local" / "bin")
    assert os.path.isabs(prefix[0])
    assert not prefix[0].startswith(str(user_home))
    assert prefix[1:] == ["-H", "-n", "-u", "alice", "--"]
    assert kwargs == {}


def test_agent_binary_is_still_found_on_the_user_path(user_home):
    args, _, _ = sessions.command("codex", SimpleNamespace(username="alice"), None)
    assert args[0] == sessions.SUDO
    assert args[6] == str(user_home / ".local" / "bin" / "codex")


def test_single_user_has_no_sudo_prefix(monkeypatch):
    _, _, prefix = sessions.identity(SimpleNamespace(username=None))
    assert prefix == []
