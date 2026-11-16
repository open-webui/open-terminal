"""GitHub Copilot ACP launch and discovery."""

from . import acp

NAME = "GitHub Copilot"
BINARY = "copilot"
COMMAND_ENV = "OPEN_TERMINAL_COPILOT_COMMAND"


def arguments(options):
    return ["--acp"]


async def discover(send, response, cwd):
    return await acp.discover(send, response, cwd, auth_method=None)
