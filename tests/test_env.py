import pytest

from open_terminal.env import _resolve_file_env


def test_resolve_file_env_reads_direct_env(monkeypatch):
    monkeypatch.setenv("TEST_SECRET", "direct-value")
    monkeypatch.delenv("TEST_SECRET_FILE", raising=False)

    assert _resolve_file_env("TEST_SECRET") == "direct-value"


def test_resolve_file_env_reads_file_value(monkeypatch, tmp_path):
    secret_file = tmp_path / "secret.txt"
    secret_file.write_text("file-secret\n")

    monkeypatch.delenv("TEST_SECRET", raising=False)
    monkeypatch.setenv("TEST_SECRET_FILE", str(secret_file))

    assert _resolve_file_env("TEST_SECRET") == "file-secret"


def test_resolve_file_env_raises_when_both_set_even_if_empty(monkeypatch, tmp_path):
    secret_file = tmp_path / "secret.txt"
    secret_file.write_text("file-secret")

    monkeypatch.setenv("TEST_SECRET", "")
    monkeypatch.setenv("TEST_SECRET_FILE", str(secret_file))

    with pytest.raises(ValueError, match="mutually exclusive"):
        _resolve_file_env("TEST_SECRET")


def test_resolve_file_env_returns_default_when_unset(monkeypatch):
    monkeypatch.delenv("TEST_SECRET", raising=False)
    monkeypatch.delenv("TEST_SECRET_FILE", raising=False)

    assert _resolve_file_env("TEST_SECRET", default="fallback") == "fallback"
