"""Goose ACP launch and discovery."""

from . import acp

NAME = "Goose"
BINARY = "goose"
COMMAND_ENV = "OPEN_TERMINAL_GOOSE_COMMAND"


def arguments(options):
    return ["acp"]


async def discover(send, response, cwd):
    return await acp.discover(send, response, cwd, auth_method=None)
