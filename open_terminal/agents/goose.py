"""Goose ACP launch and discovery."""

import os
import shutil
from pathlib import Path

from . import acp

NAME = "Goose"
BINARY = "goose"
COMMAND_ENV = "OPEN_TERMINAL_GOOSE_COMMAND"


def find_cli(path):
    for directory in path.split(os.pathsep):
        executable = shutil.which(BINARY, path=directory or os.curdir)
        # The Linux desktop package also installs a `goose` command, linked to Electron.
        if executable and not (Path(executable).resolve().parent / "resources" / "app.asar").is_file():
            return executable
    return None


def arguments(options):
    return ["acp"]


async def discover(send, response, cwd):
    return await acp.discover(send, response, cwd, auth_method=None)
