"""Tests for Docker socket group assignment in multi-user provisioning."""

import grp
import importlib
import os
import types

import pytest

from open_terminal import env
from open_terminal.utils import user_isolation

DOCKER_SOCK = "/var/run/docker.sock"


@pytest.fixture
def allowlist(monkeypatch):
    """Set OPEN_TERMINAL_DOCKER_ALLOWED_USERS (None = unset) and reload env."""

    def _set(value):
        if value is None:
            monkeypatch.delenv("OPEN_TERMINAL_DOCKER_ALLOWED_USERS", raising=False)
        else:
            monkeypatch.setenv("OPEN_TERMINAL_DOCKER_ALLOWED_USERS", value)
        importlib.reload(env)
        return env.DOCKER_ALLOWED_USERS

    yield _set
    monkeypatch.delenv("OPEN_TERMINAL_DOCKER_ALLOWED_USERS", raising=False)
    importlib.reload(env)


@pytest.fixture
def provision(monkeypatch):
    """Run ensure_os_user() with a fake Docker socket and record commands."""
    commands = []
    real_exists, real_stat = os.path.exists, os.stat

    def fake_getpwnam(name):
        if name == "server":
            return types.SimpleNamespace(pw_dir="/home/server", pw_gid=1000)
        raise KeyError(name)

    monkeypatch.setenv("USER", "server")
    monkeypatch.setattr(user_isolation, "_run_privileged", commands.append)
    monkeypatch.setattr(user_isolation.pwd, "getpwnam", fake_getpwnam)
    monkeypatch.setattr(os.path, "exists", lambda p: p == DOCKER_SOCK or real_exists(p))
    monkeypatch.setattr(
        os,
        "stat",
        lambda p, *a, **k: (
            types.SimpleNamespace(st_gid=999)
            if p == DOCKER_SOCK
            else real_stat(p, *a, **k)
        ),
    )
    monkeypatch.setattr(
        grp, "getgrgid", lambda gid: types.SimpleNamespace(gr_name="docker")
    )
    monkeypatch.setattr(grp, "getgrall", list)
    monkeypatch.setattr(os, "setgroups", lambda gids: None)

    def _run(username):
        commands.clear()
        user_isolation.ensure_os_user(username)
        return ["usermod", "-aG", "docker", username] in commands

    return _run


def test_allowlist_unset_grants_every_user(allowlist, provision):
    assert allowlist(None) is None
    assert provision("alice") is True
    assert provision("bob") is True


def test_authorized_user_is_added(allowlist, provision):
    allowlist("alice")
    assert provision("alice") is True


def test_unauthorized_user_is_provisioned_without_docker(allowlist, provision):
    allowlist("alice")
    assert provision("bob") is False


def test_multiple_comma_separated_users(allowlist, provision):
    assert allowlist(" alice, bob ,,carol ") == {"alice", "bob", "carol"}
    assert provision("alice") is True
    assert provision("bob") is True
    assert provision("carol") is True
    assert provision("dave") is False


def test_empty_allowlist_grants_no_user(allowlist, provision):
    assert allowlist("") == frozenset()
    assert provision("alice") is False
