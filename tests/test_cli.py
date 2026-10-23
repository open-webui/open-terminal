import sys

from click.testing import CliRunner

from open_terminal import cli


def test_run_accepts_api_key_file(monkeypatch, tmp_path):
    key_file = tmp_path / "api-key"
    key_file.write_text("dummy-test-key\n")
    monkeypatch.setattr(cli.uvicorn, "run", lambda *args, **kwargs: None)
    sys.modules.pop("open_terminal.env", None)

    result = CliRunner().invoke(
        cli.main,
        ["run", "--host", "127.0.0.1", "--port", "0"],
        env={"OPEN_TERMINAL_API_KEY_FILE": str(key_file)},
    )

    assert result.exit_code == 0, result.exception
    assert sys.modules["open_terminal.env"].API_KEY == "dummy-test-key"
    sys.modules.pop("open_terminal.env", None)
