"""Grok ACP launch and discovery."""

from . import acp

NAME = "Grok"
BINARY = "grok"
COMMAND_ENV = "OPEN_TERMINAL_GROK_COMMAND"


def arguments(options):
    return ["--no-auto-update", "agent", "stdio"]


async def discover(send, response, cwd):
    return await acp.discover(send, response, cwd, auth_method=None)
