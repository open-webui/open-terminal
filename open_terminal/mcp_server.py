"""MCP server — exposes every FastAPI endpoint as an MCP tool."""

from fastmcp import FastMCP
from open_terminal.main import app
from open_terminal.env import API_KEY

mcp = FastMCP.from_fastapi(
    app=app,
    name="Open Terminal",
    httpx_client_kwargs={
        "headers": {
            "Authorization": f"Bearer {API_KEY}",
        }
    },
)