"""
MCP Server — FastMCP SSE transport, port 8090.

Environment variables:
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

mcp = FastMCP(
    name="poormans_whatsapp_mcp",
    version="0.1.0",
)


# ── permission guard ──────────────────────────────────────────────────────────

def _guard(tool_name: str, caller_role: str) -> None:
    allowed, reason = check_permission(tool_name, caller_role)
    if not allowed:
        raise PermissionError(reason)


# ── meta tool ─────────────────────────────────────────────────────────────────

@mcp.tool()
async def search_tools(query: str, caller_role: str = "user") -> list[dict]:
    """
    Search for the best tool to accomplish a task.

    Args:
        query:       Natural-language description of what needs to be done.
        caller_role: Role of the caller ('owner' | 'admin' | 'user' | 'guest').

    Returns:
        List of {name, description, score} sorted by relevance.
    """
    results = _search_tools(query=query, role=caller_role)
    if not results:
        return [{"name": None, "description": "No matching tools found for this query.", "score": 0}]
    return results


# ── user tools ────────────────────────────────────────────────────────────────

@mcp.tool()
async def add_user(
    caller_role: str,
    phone: str | None = None,
    name: str | None = None,
    role: str | None = None,
) -> dict:
    """
    Register a new user.

    All three of phone, name, and role are required.
    If any are missing the tool returns a clear message listing what is needed.

    Args:
        caller_role: Role of the invoking user.
        phone:       E.164 phone number e.g. +919876543210
        name:        Display name.
        role:        owner | admin | user | guest
    """
    _guard("add_user", caller_role)
    return await user_tools.add_user(phone=phone, name=name, role=role)


@mcp.tool()
async def deactivate_user(
    caller_role: str,
    phone: str | None = None,
    name: str | None = None,
) -> dict:
    """
    Deactivate a user (sets is_active=False).

    Identify the user by EITHER:
    - phone number (E.164, e.g. 919567288514), OR
    - display name — a partial or approximate name is fine, the system
      will fuzzy-match it. Do NOT ask the user for a phone number if
      you already have a name.

    Args:
        caller_role: Role of the invoking user.
        phone:       E.164 phone number. Optional if name is provided.
        name:        Display name. Optional if phone is provided.
    """
    _guard("deactivate_user", caller_role)
    return await user_tools.deactivate_user(phone=phone, name=name)


@mcp.tool()
async def reactivate_user(
    caller_role: str,
    phone: str | None = None,
    name: str | None = None,
) -> dict:
    """
    Reactivate a user (sets is_active=True).

    Identify the user by EITHER:
    - phone number (E.164, e.g. 919567288514), OR
    - display name — a partial or approximate name is fine, the system
      will fuzzy-match it. Do NOT ask the user for a phone number if
      you already have a name.

    Args:
        caller_role: Role of the invoking user.
        phone:       E.164 phone number. Optional if name is provided.
        name:        Display name. Optional if phone is provided.
    """
    _guard("reactivate_user", caller_role)
    return await user_tools.reactivate_user(phone=phone, name=name)


# ── entrypoint ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    host = os.getenv("MCP_HOST", "0.0.0.0")
    port = int(os.getenv("MCP_PORT", "8090"))
    log.info("Starting MCP server on %s:%s", host, port)
    mcp.run(transport="sse", host=host, port=port)