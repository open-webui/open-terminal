"""Pi's current JSONL RPC launch and model discovery."""

from uuid import uuid4

NAME = "Pi"
BINARY = "pi"


def arguments(options):
    args = ["--mode", "rpc"]
    if options.request_id == "discovery":
        return [*args, "--no-session"]
    args += ["--session-id", options.resume or uuid4().hex]
    if options.instructions:
        args += ["--append-system-prompt", options.instructions]
    return args


async def discover(send, response, cwd):
    await send({"id": "discovery", "type": "get_available_models"})
    reply = await response("id", "discovery")
    if not reply.get("success"):
        raise RuntimeError(reply.get("error", "Pi model discovery failed"))
    return [
        {"id": f"{m['provider']}/{m['id']}", "name": m.get("name") or m["id"]}
        for m in reply.get("data", {}).get("models", [])
    ]
