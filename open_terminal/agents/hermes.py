"""Hermes Agent ACP launch and discovery."""

from . import acp

NAME = "Hermes"
BINARY = "hermes"
COMMAND_ENV = "OPEN_TERMINAL_HERMES_COMMAND"


def arguments(options):
    return ["acp"]


async def discover(send, response, cwd):
    return await acp.discover(send, response, cwd, auth_method=None)
