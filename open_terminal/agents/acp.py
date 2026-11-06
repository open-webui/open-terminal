"""ACP v1 model discovery using the installed agent's session configuration."""


async def discover(send, response, cwd, auth_method=None):
    sequence = 0

    async def request(method, params):
        nonlocal sequence
        sequence += 1
        await send(
            {"jsonrpc": "2.0", "id": sequence, "method": method, "params": params}
        )
        reply = await response("id", sequence)
        if "error" in reply:
            raise RuntimeError(reply["error"].get("message", "Agent request failed"))
        return reply.get("result", {})

    initialized = await request(
        "initialize",
        {
            "protocolVersion": 1,
            "clientInfo": {"name": "open_terminal", "version": "1"},
            "clientCapabilities": {
                "fs": {"readTextFile": False, "writeTextFile": False},
                "terminal": False,
            },
        },
    )
    if initialized.get("protocolVersion") != 1:
        raise RuntimeError("Agent requires an unsupported ACP version")
    if auth_method:
        await request("authenticate", {"methodId": auth_method})
    session = await request("session/new", {"cwd": cwd, "mcpServers": []})
    try:
        option = next(
            (
                c
                for c in session.get("configOptions", [])
                if c.get("category") == "model" or c.get("id") == "model"
            ),
            None,
        )
        if option:
            values = option.get("options", [])
            values = [
                v
                for group in values
                for v in (group["options"] if "group" in group else [group])
            ]
            return [
                {"id": v["value"], "name": v.get("name") or v["value"]} for v in values
            ]
        # Some current agents still advertise the native ACP models field.
        return [
            {"id": m["modelId"], "name": m.get("name") or m["modelId"]}
            for m in session.get("models", {}).get("availableModels", [])
        ]
    finally:
        capabilities = initialized.get("agentCapabilities", {}).get(
            "sessionCapabilities", {}
        )
        if "delete" in capabilities:
            await request("session/delete", {"sessionId": session["sessionId"]})
