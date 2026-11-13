"""ACP v1 model discovery using the installed agent's session configuration."""


async def discover(send, response, cwd, auth_method=None):
    sequence = 0
    config_options = []

    def notification(message):
        nonlocal config_options
        if message.get("method") == "session/update":
            update = message.get("params", {}).get("update", {})
            if update.get("sessionUpdate") == "config_option_update":
                config_options = update.get("configOptions", [])

    async def request(method, params):
        nonlocal sequence, config_options
        sequence += 1
        await send(
            {"jsonrpc": "2.0", "id": sequence, "method": method, "params": params}
        )
        reply = await response("id", sequence, on_notification=notification)
        if "error" in reply:
            raise RuntimeError(reply["error"].get("message", "Agent request failed"))
        result = reply.get("result", {})
        config_options = result.get("configOptions", config_options)
        return result

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
                for c in config_options
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
            models = [
                {"id": v["value"], "name": v.get("name") or v["value"]} for v in values
            ]
        else:
            # Some current agents still advertise the native ACP models field.
            models = [
                {"id": m["modelId"], "name": m.get("name") or m["modelId"]}
                for m in session.get("models", {}).get("availableModels", [])
            ]
        current_model = (option or {}).get("currentValue") or session.get(
            "models", {}
        ).get("currentModelId")
        for model in models:
            model["supportedReasoningEfforts"] = []
            # Thinking choices can change when the model changes.
            if model["id"] != current_model:
                try:
                    if option:
                        await request(
                            "session/set_config_option",
                            {
                                "sessionId": session["sessionId"],
                                "configId": option["id"],
                                "value": model["id"],
                            },
                        )
                    else:
                        await request(
                            "session/set_model",
                            {
                                "sessionId": session["sessionId"],
                                "modelId": model["id"],
                            },
                        )
                except RuntimeError:
                    # An unavailable model's optional controls must not hide the catalog.
                    continue
                current_model = model["id"]
            thought = next(
                (
                    c
                    for c in config_options
                    if c.get("category") == "thought_level"
                    and c.get("type") == "select"
                ),
                {},
            )
            model["supportedReasoningEfforts"] = [
                {
                    "reasoningEffort": value["value"],
                    "label": value.get("name") or value["value"],
                }
                for group in thought.get("options", [])
                for value in (group["options"] if "group" in group else [group])
            ]
        return models
    finally:
        capabilities = initialized.get("agentCapabilities", {}).get(
            "sessionCapabilities", {}
        )
        if "delete" in capabilities:
            await request("session/delete", {"sessionId": session["sessionId"]})
