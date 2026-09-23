"""MCP server — exposes every FastAPI endpoint as an MCP tool."""

import hmac

from fastmcp import FastMCP
from fastmcp.server.auth import AccessToken, TokenVerifier
from open_terminal.main import app
from open_terminal.env import API_KEY


class ApiKeyVerifier(TokenVerifier):
    """Accept a client bearer token only when it matches the API key."""

    async def verify_token(self, token: str) -> AccessToken | None:
        if not hmac.compare_digest(token, API_KEY):
            return None
        return AccessToken(token=token, client_id="open-terminal", scopes=[])


# HTTP transports gate on the API key; stdio is local and never hits this layer.
mcp = FastMCP.from_fastapi(
    app=app,
    name="Open Terminal",
    httpx_client_kwargs={
        "headers": {
            "Authorization": f"Bearer {API_KEY}",
        }
    },
    auth=ApiKeyVerifier(),
)
