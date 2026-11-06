"""Cursor ACP launch and discovery."""

from . import acp

NAME = "Cursor"
BINARY = "agent"
COMMAND_ENV = "OPEN_TERMINAL_CURSOR_COMMAND"


def arguments(options):
    return ["acp"]


async def discover(send, response, cwd):
    return await acp.discover(send, response, cwd, auth_method="cursor_login")
