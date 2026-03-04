"""
MCP Server — FastMCP SSE transport, port 8090.

Responsibilities:
  - Expose all tools via SSE so any MCP client (Python, Go, etc.) can connect.
  - Enforce role-based permissions before dispatching any tool.
  - Provide `search_tools` as a first-class MCP tool for the agent loop.

Environment variables expected:
  MCP_HOST  (default: 0.0.0.0)
  MCP_PORT  (default: 8090)
"""

from __future__ import annotations

import logging
import os

from fastmcp import FastMCP

from mcpserver.tools._registry import check_permission, search_tools as _search_tools
from mcpserver.tools import users as user_tools

log = logging.getLogger(__name__)

# ── server instance ───────────────────────────────────────────────────────────

mcp = FastMCP(
    name="poormans_whatsapp_mcp",
    version="0.1.0",
)


# ── permission guard ──────────────────────────────────────────────────────────

def _guard(tool_name: str, caller_role: str) -> None:
    """Raise ValueError if caller_role cannot access tool_name."""
    allowed, reason = check_permission(tool_name, caller_role)
    if not allowed:
        raise PermissionError(reason)


# ── meta tool: search_tools ───────────────────────────────────────────────────

@mcp.tool()
async def search_tools(query: str, caller_role: str = "user") -> list[dict]:
    """
    Search for the best tool to accomplish a task.

    The agent ALWAYS has this tool. It returns the top matching tool schemas
    so the agent can pick one and call it in the next iteration.

    Args:
        query:       Natural-language description of what needs to be done.
        caller_role: Role of the user who triggered the agent ('owner' | 'admin' | 'user' | 'guest').

    Returns:
        List of {name, description, score} sorted by relevance.
    """
    results = _search_tools(query=query, role=caller_role)
    if not results:
        return [{"name": None, "description": "No matching tools found for this query.", "score": 0}]
    return results


# ── users tools ───────────────────────────────────────────────────────────────

@mcp.tool()
async def get_user(
    caller_role: str,
    phone: str | None = None,
    user_id: int | None = None,
) -> dict:
    """
    Fetch a user profile by phone or ID.

    Args:
        caller_role: Role of the invoking user.
        phone:       E.164 phone number (optional).
        user_id:     Integer user ID (optional).
    """
    _guard("get_user", caller_role)
    return await user_tools.get_user(phone=phone, user_id=user_id)


@mcp.tool()
async def list_users(caller_role: str, role_filter: str | None = None) -> dict:
    """
    List all users, optionally filtered by role.

    Args:
        caller_role:  Role of the invoking user.
        role_filter:  Optional role to filter by.
    """
    _guard("list_users", caller_role)
    return await user_tools.list_users(role_filter=role_filter)


@mcp.tool()
async def add_user(caller_role: str, phone: str, name: str, role: str = "user") -> dict:
    """
    Register a new user.

    Args:
        caller_role: Role of the invoking user.
        phone:       E.164 phone number.
        name:        Display name.
        role:        New user's role (default: 'user').
    """
    _guard("add_user", caller_role)
    return await user_tools.add_user(phone=phone, name=name, role=role)


@mcp.tool()
async def update_user_role(caller_role: str, phone: str, new_role: str) -> dict:
    """
    Change a user's role.

    Args:
        caller_role: Role of the invoking user.
        phone:       Target user's phone number.
        new_role:    New role to assign.
    """
    _guard("update_user_role", caller_role)
    return await user_tools.update_user_role(phone=phone, new_role=new_role)


@mcp.tool()
async def remove_user(caller_role: str, phone: str) -> dict:
    """
    Permanently remove a user.

    Args:
        caller_role: Role of the invoking user.
        phone:       Target user's phone number.
    """
    _guard("remove_user", caller_role)
    return await user_tools.remove_user(phone=phone)


# ── entrypoint ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    host = os.getenv("MCP_HOST", "0.0.0.0")
    port = int(os.getenv("MCP_PORT", "8090"))

    log.info("Starting MCP server on %s:%s", host, port)
    mcp.run(transport="sse", host=host, port=port)
