"""Qwen Code ACP launch and discovery."""

from . import acp

NAME = "Qwen Code"
BINARY = "qwen"
COMMAND_ENV = "OPEN_TERMINAL_QWEN_COMMAND"


def arguments(options):
    return ["--acp"]


async def discover(send, response, cwd):
    return await acp.discover(send, response, cwd, auth_method=None)
