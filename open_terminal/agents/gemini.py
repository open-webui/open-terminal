"""Gemini CLI ACP launch and discovery."""

from . import acp

NAME = "Gemini CLI"
BINARY = "gemini"
COMMAND_ENV = "OPEN_TERMINAL_GEMINI_COMMAND"


def arguments(options):
    return ["--acp"]


async def discover(send, response, cwd):
    return await acp.discover(send, response, cwd, auth_method=None)
