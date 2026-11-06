"""OpenCode ACP launch and discovery."""

from . import acp

NAME = "OpenCode"
BINARY = "opencode"
COMMAND_ENV = "OPEN_TERMINAL_OPENCODE_COMMAND"


def arguments(options):
    return ["acp"]


async def discover(send, response, cwd):
    return await acp.discover(send, response, cwd, auth_method=None)
