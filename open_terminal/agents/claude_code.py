"""Claude Code launch and model discovery."""

NAME = "Claude Code"
BINARY = "claude"


def arguments(options):
    args = [
        "--input-format",
        "stream-json",
        "--output-format",
        "stream-json",
        "--verbose",
        "--include-partial-messages",
        "--permission-prompt-tool",
        "stdio",
    ]
    if options.model:
        args += ["--model", options.model]
    if options.resume:
        args += ["--resume", options.resume]
        if options.resume_at:
            args += ["--resume-session-at", options.resume_at]
        if options.fork:
            args += ["--fork-session"]
    if options.instructions:
        args += ["--append-system-prompt", options.instructions]
    if options.approval_mode == "auto":
        # Claude's own reviewer approves routine actions; what it escalates still comes through stdio.
        args += ["--permission-mode", "auto"]
    return args


async def discover(send, response, cwd=None):
    await send(
        {
            "type": "control_request",
            "request_id": "discovery",
            "request": {"subtype": "initialize"},
        }
    )
    result = (await response("type", "control_response")).get("response", {})
    if result.get("subtype") == "error":
        raise RuntimeError("Claude initialization failed")
    return [
        {
            "id": m.get("resolvedModel") or m["value"],
            "name": m.get("displayName") or m["value"],
            "supportedReasoningEfforts": [
                {"reasoningEffort": level}
                for level in (m.get("supportedEffortLevels") or [])
            ],
        }
        for m in result.get("response", {}).get("models", [])
    ]
