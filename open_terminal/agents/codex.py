"""Codex app-server launch and model discovery."""

NAME = "Codex"
BINARY = "codex"


def arguments(options):
    return ["app-server", "--listen", "stdio://"]


async def discover(send, response, cwd=None):
    await send(
        {
            "id": 1,
            "method": "initialize",
            "params": {"clientInfo": {"name": "open_terminal", "version": "1"}},
        }
    )
    init = await response("id", 1)
    if init.get("error"):
        raise RuntimeError("Codex initialization failed")
    await send({"method": "initialized"})
    models, cursor = [], None
    while True:
        await send({"id": 2, "method": "model/list", "params": {"cursor": cursor}})
        result = (await response("id", 2)).get("result", {})
        models += [
            {
                "id": m["model"],
                "name": m.get("displayName") or m["model"],
                **{
                    key: m[key]
                    for key in (
                        "supportedReasoningEfforts",
                        "defaultReasoningEffort",
                        "serviceTiers",
                        "defaultServiceTier",
                    )
                    if key in m
                },
            }
            for m in result.get("data", [])
        ]
        cursor = result.get("nextCursor")
        if not cursor:
            return models
