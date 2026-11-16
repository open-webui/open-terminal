"""Grok ACP launch and discovery."""

from . import acp

NAME = "Grok"
BINARY = "grok"
COMMAND_ENV = "OPEN_TERMINAL_GROK_COMMAND"


def arguments(options):
    # Grok's own reviewer approves routine actions in Auto; it escalates the rest over ACP.
    mode = ["--permission-mode", "auto"] if options.approval_mode == "auto" else []
    return ["--no-auto-update", *mode, "agent", "stdio"]


async def discover(send, response, cwd):
    return await acp.discover(send, response, cwd, auth_method=None)
